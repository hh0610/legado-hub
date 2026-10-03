"""Bounded application attack matrix using only local test state and mocks."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.legado_source import generate_legado_source
from app.main import app, create_app
from app.services.catalog import Catalog
from app.services.aggregate_virtual_source import VIRTUAL_SOURCE_ID, make_aggregate_chapter_url
from app.services.reading_limits import (
    ReadingAccessLimiter,
    ReadingActionLimit,
    ReadingLimitError,
)
from app.services.reading_reviews import render_chapter_reviews_html
from app.services.user_auth import auth_service
from app.source_plugins.id_codec import encode_chapter_id


def _bearer_identity() -> tuple[str, dict]:
    created = auth_service.create_access_user(f"attack-{uuid.uuid4().hex[:10]}")
    response = TestClient(app).post(
        "/api/auth/access/redeem",
        json={"accessCode": created["accessCode"]},
    )
    assert response.status_code == 200
    return response.json()["token"], created


def test_authorization_variants_never_fall_back_to_cookie() -> None:
    token, _ = _bearer_identity()
    cookie_client = TestClient(app)
    login = cookie_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    assert login.status_code == 200
    path = "/api/subscribe/legado/search?keyword="

    for authorization in ("", "Basic Zm9vOmJhcg==", "Bearer invalid-session-token", "Bearer bad extra"):
        assert cookie_client.get(path, headers={"Authorization": authorization}).status_code == 401
    assert cookie_client.get(path, headers={"Authorization": "Bearer invalid,token"}).status_code == 401
    duplicate = cookie_client.get(
        path,
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", "Bearer attacker")],
    )
    assert duplicate.status_code == 401

    bearer = TestClient(app)
    assert bearer.get(path, headers={"Authorization": f"bearer {token}"}).status_code == 200
    assert bearer.get(path, headers={"Authorization": f"Bearer    {token}"}).status_code == 200


def test_session_fixation_replay_and_revocation() -> None:
    created = auth_service.create_access_user(f"fixed-{uuid.uuid4().hex[:10]}")
    victim = TestClient(app)
    victim.cookies.set("legadohub_session", "attacker-fixed-session")
    redeemed = victim.post(
        "/api/auth/access/redeem",
        json={"accessCode": created["accessCode"]},
    )
    assert redeemed.status_code == 200
    token = redeemed.json()["token"]
    assert token != "attacker-fixed-session"

    replay = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    assert replay.get("/api/auth/access/me", headers=headers).status_code == 200
    auth_service.revoke_user_sessions(created["userId"])
    assert replay.get("/api/auth/access/me", headers=headers).status_code == 401


def test_reading_rate_and_concurrency_limits_are_bounded(monkeypatch) -> None:
    # Live third-party batches would make this rate-limit probe depend on
    # upstream sites; the limits under test live on the hub side only.
    from app.api import subscribe as subscribe_api

    monkeypatch.setattr(subscribe_api, "_third_party_search_source_ids", lambda service: [])
    now = [100.0]
    limiter = ReadingAccessLimiter(
        window_seconds=60,
        limits={"chapter": ReadingActionLimit(100, max_concurrency=2)},
        clock=lambda: now[0],
    )
    with limiter.guard("user", "chapter"):
        with limiter.guard("user", "chapter"):
            with pytest.raises(ReadingLimitError) as concurrent:
                with limiter.guard("user", "chapter"):
                    pass
    assert concurrent.value.code == "reading_concurrency_limited"
    now[0] += 60
    with limiter.guard("user", "chapter"):
        pass

    token, _ = _bearer_identity()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(20):
        assert client.get("/api/subscribe/legado/search?keyword=", headers=headers).status_code == 200
    limited = client.get("/api/subscribe/legado/search?keyword=", headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert limited.json()["detail"]["code"] == "reading_rate_limited"


def test_injection_and_amplification_inputs_are_rejected_before_work(monkeypatch) -> None:
    from app.api import subscribe as subscribe_api

    monkeypatch.setattr(subscribe_api, "_third_party_search_source_ids", lambda service: [])
    token, _ = _bearer_identity()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    for keyword in ("' OR 1=1 --", "' UNION SELECT password_hash FROM users --", "\uFF07 OR 1=1"):
        response = client.get(
            "/api/subscribe/legado/search",
            params={"keyword": keyword},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["items"] == []

    assert client.get(
        "/api/subscribe/legado/search",
        params={"keyword": "x" * 201},
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/legado/search?keyword=x&page=1000000000",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/legado/search?keyword=x&page=1&page=2",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/legado/search?keyword=x&targetUrl=http://127.0.0.1",
        headers=headers,
    ).status_code == 422

    monkeypatch.setattr(
        "app.api.subscribe.subscription_search_service.create_job",
        lambda *_args, **_kwargs: pytest.fail("invalid DTO must not create a search job"),
    )
    assert client.post(
        "/api/subscribe/search",
        headers=headers,
        json={"keyword": "test", "targetUrl": "http://127.0.0.1"},
    ).status_code == 422
    assert client.post(
        "/api/subscribe/search",
        headers=headers,
        json={"keyword": {"$ne": ""}},
    ).status_code == 422
    assert client.post(
        "/api/auth/access/redeem",
        json={"accessCode": {"$gt": ""}},
    ).status_code == 422
    assert client.post(
        "/api/auth/access/redeem",
        json={"accessCode": "x" * 1_048_576},
    ).status_code == 422


def test_library_routes_reject_ambiguous_queries_and_cross_user_access(monkeypatch) -> None:
    token, created = _bearer_identity()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get(
        "/api/subscribe/library/mine",
        params={"keyword": "x" * 201},
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/library/mine?keyword=a&keyword=b",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/books/book-a/chapters?pageSize=1000000000",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/books/book-a/chapters?page=1&page=2",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/books/book-a/chapters?status=totally-invalid",
        headers=headers,
    ).status_code == 422
    assert client.get(
        "/api/subscribe/books/%2e%2e%5coutside",
        headers=headers,
    ).status_code in {404, 422}

    monkeypatch.setattr(
        "app.api.subscribe.user_subscriptions_service.get",
        lambda user_id, _book_id: {"userId": user_id} if user_id != created["userId"] else None,
    )
    monkeypatch.setattr(
        "app.api.subscribe.library_books_service.get_shared_book_detail",
        lambda _book_id: pytest.fail("cross-user request reached shared book detail"),
    )
    assert client.get(
        "/api/subscribe/books/book-a",
        headers=headers,
    ).status_code == 404


def test_direct_plugin_and_path_traversal_ids_never_reach_catalog(monkeypatch) -> None:
    token, _ = _bearer_identity()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    async def must_not_call(*_args, **_kwargs):
        pytest.fail("blocked id reached Catalog")

    monkeypatch.setattr(Catalog, "chapter", must_not_call)
    monkeypatch.setattr(Catalog, "chapter_reviews", must_not_call)
    direct_id = encode_chapter_id("qidian_com_app", "https://m.qidian.com/chapter/1/2")
    assert client.get(f"/api/legado/chapter/{direct_id}", headers=headers).status_code == 404
    assert client.get(f"/api/legado/chapter/{direct_id}/reviews", headers=headers).status_code == 404

    for attack_id in ("..", "%2e%2e", "bad\\path:value", "x:" + "A" * 9000):
        response = client.get(f"/api/legado/chapter/{attack_id}", headers=headers)
        assert response.status_code in {404, 422}

    virtual_id = encode_chapter_id(
        VIRTUAL_SOURCE_ID,
        make_aggregate_chapter_url("missing-book", "missing-chapter", index=1),
    )
    assert client.get(
        f"/api/legado/chapter/{virtual_id}/reviews/view?pageSize=1000000000",
        headers=headers,
    ).status_code == 422
    assert client.get(
        f"/api/legado/chapter/{virtual_id}/reviews/view?paragraphIds=" + "1," * 600,
        headers=headers,
    ).status_code == 422


def test_review_html_escapes_remote_text_and_rejects_untrusted_media() -> None:
    payload = {
        "authorReviews": [],
        "chapterEnd": [
            {
                "id": "1",
                "userName": '<img src=x onerror="alert(1)">',
                "content": "<script>alert(1)</script>[fn=1]",
                "mediaUrl": "javascript:alert(1)",
            }
        ],
        "hotParagraphReviews": [],
        "summary": {"totalReviews": 1, "chapterEndCount": 1},
    }
    rendered = render_chapter_reviews_html(
        chapter_title='<img src=x onerror="chapter()">',
        reviews=payload,
        review_view_url="https://books.example.test/api/reviews",
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;chapter()&quot;&gt;" in rendered
    assert "javascript:alert(1)" not in rendered
    assert 'onerror="alert(1)"' not in rendered


def test_review_html_uses_one_inline_reply_control_and_shows_paragraph_context() -> None:
    reviews = {
        "authorReviews": [],
        "chapterEnd": [],
        "hotParagraphReviews": [
            {"paragraphId": 2, "matchedText": "第一段正文", "commentCount": 2},
            {"paragraphId": 3, "matchedText": "第二段正文", "commentCount": 1},
        ],
        "summary": {"totalReviews": 3},
    }
    rendered = render_chapter_reviews_html(
        chapter_title="测试章节",
        reviews=reviews,
        review_view_url="https://books.example.test/api/reviews",
        active_tab="paragraph",
        page_hot_detail={
            "paragraphIds": [2, 3],
            "comments": [
                {
                    "id": "100",
                    "paragraphId": 2,
                    "content": "段落热评",
                    "replyCount": 2,
                    "replies": [{"userName": "回复者", "content": "首条回复"}],
                },
                {"id": "200", "paragraphId": 3, "content": "另一段热评"},
            ],
            "totalCount": 3,
        },
    )

    assert "段落热评" in rendered
    assert "另一段热评" in rendered
    assert "第一段正文" not in rendered
    assert "第二段正文" not in rendered
    assert "官方段落" not in rendered
    assert "展开2条回复" in rendered
    assert '<div class="reply-line" data-review-id="' in rendered
    assert "data-reply-url=" in rendered
    assert "loadReplyDetails" in rendered
    assert "loadedReplyUrls.has" in rendered
    assert "查看 2 条回复" not in rendered
    assert "reply-stack::before" not in rendered


def test_source_generation_rejects_js_url_injection_and_exports_no_credentials() -> None:
    with pytest.raises(RuntimeError):
        generate_legado_source('https://books.example.test/\";alert(1);//')
    source = generate_legado_source("https://books.example.test")[0]
    serialized = str(source)
    assert "LH1." not in serialized
    assert "legadohub_session" not in serialized
    assert "adminPassword" not in serialized


def test_https_reader_cors_rejection_has_no_wildcard_or_payload_leak(monkeypatch, caplog) -> None:
    import app.core.public_security as security_module

    security_module._security_last_log.clear()
    monkeypatch.delenv("LEGADOHUB_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("LEGADOHUB_REQUIRE_HTTPS", "1")
    monkeypatch.setenv("LEGADOHUB_ALLOWED_HOSTS", "books.example.test")
    monkeypatch.setenv("LEGADOHUB_ALLOWED_ORIGINS", "https://books.example.test")
    monkeypatch.setenv("LEGADOHUB_TRUSTED_PROXIES", "127.0.0.1/32")
    public_app = create_app(security_module.load_public_security_config())
    client = TestClient(public_app, base_url="https://books.example.test")
    malicious_origin = "https://secret-token.evil.invalid"

    options = client.options(
        "/api/auth/logout",
        headers={"Origin": malicious_origin, "Access-Control-Request-Method": "POST"},
    )
    assert options.status_code in {403, 405}
    assert "access-control-allow-origin" not in options.headers
    rejected = client.post("/api/auth/logout", headers={"Origin": malicious_origin})
    assert rejected.status_code == 403
    assert rejected.headers["x-request-id"]
    assert malicious_origin not in caplog.text
