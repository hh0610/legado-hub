"""Tests for audiobook/video (media) source support.

Covers: metadata content.kind validation, media chapter payloads, the signed
media proxy (sign/verify, playlist rewrite, host allow-list, SSRF guard), the
media group filter in subscription payloads, and the aggregate pipeline media
branch (descriptor persistence + read paths).
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.source_plugins.models import ChapterContent, PluginMetadata
from app.services.media_proxy import (
    MediaProxyError,
    rewrite_playlist,
    sign_media_url,
    verify_media_token,
)
from app.services.aggregate_processor import AggregateProcessor
from app.services.aggregate_virtual_source import (
    VIRTUAL_SOURCE_ID,
    make_aggregate_chapter_url,
)
from app.services.library_books import LibraryBooksService
from app.services.shared_book_storage import SharedBookStorage
from app.source_plugins.id_codec import encode_chapter_id
from app.storage.db import initialize_database


# ---- metadata: content.kind ------------------------------------------------


def _metadata(content: dict) -> PluginMetadata:
    return PluginMetadata(
        contract_version="1.0",
        id="test_source",
        name="测试源",
        version="0.1.0",
        type="source",
        domains=["example.com"],
        base_urls=["https://example.com"],
        capabilities=["search", "detail", "toc", "chapter"],
        auth={"mode": "none"},
        content=content,
        tags=[],
    )


def test_content_kind_defaults_to_text():
    metadata = _metadata({"access": "free"})
    assert metadata.content_kind == "text"
    assert metadata.validate() == []


def test_content_kind_accepts_audio_and_video():
    for kind in ("audio", "video"):
        metadata = _metadata({"access": "free", "kind": kind})
        assert metadata.content_kind == kind
        assert metadata.validate() == []


def test_content_kind_rejects_unknown_kind():
    metadata = _metadata({"access": "free", "kind": "podcast"})
    errors = metadata.validate()
    assert any("content.kind" in error for error in errors)
    assert metadata.content_kind == "text"


def test_chapter_content_media_fields_in_dict():
    content = ChapterContent(
        source_id="s",
        title="第1集",
        format="audio",
        media_url="https://cdn.example.com/1.mp3",
        media_mime="audio/mpeg",
        duration_seconds=12.5,
    )
    data = content.to_dict()
    assert data["format"] == "audio"
    assert data["mediaUrl"] == "https://cdn.example.com/1.mp3"
    assert data["mediaType"] == "audio/mpeg"
    assert data["durationSeconds"] == 12.5
    assert data["content"] == ""


# ---- media proxy -----------------------------------------------------------


def test_sign_and_verify_roundtrip():
    signed = sign_media_url("https://cdn.example.com/ep1.mp3", "sample_audio_books")
    assert signed.startswith("/api/media/stream?p=")
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(signed).query)
    payload = verify_media_token(query["p"][0], query["sig"][0])
    assert payload["url"] == "https://cdn.example.com/ep1.mp3"
    assert payload["sourceId"] == "sample_audio_books"


def test_verify_rejects_tampered_signature():
    signed = sign_media_url("https://cdn.example.com/ep1.mp3", "s")
    bad = signed[:-4] + "AAAA"
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(bad).query)
    with pytest.raises(MediaProxyError):
        verify_media_token(query["p"][0], query["sig"][0])


def test_sign_is_idempotent_for_proxy_urls():
    signed = sign_media_url("https://cdn.example.com/ep1.mp3", "s")
    assert sign_media_url(signed, "s") == signed


def test_rewrite_playlist_rewrites_segments_and_keys():
    playlist = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:10",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x1',
            "#EXTINF:10,",
            "seg_1.ts",
            "https://cdn.example.com/abs/seg_2.ts",
        ]
    )
    rewritten = rewrite_playlist(playlist, "https://cdn.example.com/live/index.m3u8", "sample_video_films")
    lines = rewritten.strip().splitlines()
    assert lines[0] == "#EXTM3U"
    key_line = lines[3]
    assert 'URI="/api/media/stream?p=' in key_line
    assert "IV=0x1" in key_line
    segment = lines[5]
    assert segment.startswith("/api/media/stream?p=")
    absolute = lines[6]
    assert absolute.startswith("/api/media/stream?p=")
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(absolute).query)
    payload = verify_media_token(query["p"][0], query["sig"][0])
    assert payload["url"] == "https://cdn.example.com/abs/seg_2.ts"


class _FakeMetadata:
    def __init__(self, domains, content):
        self.domains = domains
        self.content = content


class _FakePlugin:
    def __init__(self, domains, content):
        self.metadata = _FakeMetadata(domains, content)


def test_stream_host_allowlist_accepts_declared_domains():
    from app.services.media_proxy import assert_stream_host_allowed

    plugin = _FakePlugin(["example.com"], {"streamDomains": ["cdn.media.net"]})
    assert_stream_host_allowed(plugin, "https://cdn.media.net/a/1.mp3")
    assert_stream_host_allowed(plugin, "https://nested.cdn.media.net/a/1.mp3")
    assert_stream_host_allowed(plugin, "https://www.example.com/a/1.mp3")
    with pytest.raises(MediaProxyError):
        assert_stream_host_allowed(plugin, "https://media.net/a/1.mp3")
    with pytest.raises(MediaProxyError):
        assert_stream_host_allowed(plugin, "https://elsewhere.example.net/a/1.mp3")


def test_ssrf_guard_rejects_private_and_loopback():
    from app.services.media_proxy import _assert_public_upstream

    for url in (
        "http://127.0.0.1/media.mp3",
        "http://192.168.1.10/media.mp3",
        "http://10.0.0.5/media.mp3",
        "http://localhost/media.mp3",
        "ftp://example.com/media.mp3",
        "http://example.com:8081/media.mp3",
    ):
        with pytest.raises(MediaProxyError):
            _assert_public_upstream(url)


# ---- subscription payload media filter -------------------------------------


def test_payload_from_group_keeps_media_kind_only():
    service = LibraryBooksService(db_path=":memory:")

    def fake_plugins():
        class P:
            class metadata:
                content_kind = "audio"

        return {"audio_src": P()}

    service._plugins = fake_plugins  # type: ignore[method-assign]
    group = {
        "candidateId": "c1",
        "name": "某书",
        "author": "某作者",
        "items": [
            {"sourceId": "audio_src", "sourceName": "有声源", "bookId": "b1", "rawBookUrl": "https://a.example.com/1", "score": 10},
            {"sourceId": "text_src", "sourceName": "文字源", "bookId": "b2", "rawBookUrl": "https://t.example.com/1", "score": 20},
        ],
    }
    payload = service._payload_from_group(group)
    assert payload["contentType"] == "audio"
    assert [s["sourceId"] for s in payload["sources"]] == ["audio_src"]
    assert payload["sources"][0]["contentType"] == "audio"


def test_payload_from_group_text_default():
    service = LibraryBooksService(db_path=":memory:")
    service._plugins = lambda: {}  # type: ignore[method-assign]
    group = {
        "candidateId": "c1",
        "name": "某书",
        "author": "某作者",
        "items": [
            {"sourceId": "text_src", "sourceName": "文字源", "bookId": "b2", "rawBookUrl": "https://t.example.com/1", "score": 20},
        ],
    }
    payload = service._payload_from_group(group)
    assert payload["contentType"] == "text"
    assert len(payload["sources"]) == 1


# ---- aggregate pipeline media branch ---------------------------------------


def _setup_media_book(tmp_path, *, content_kind="audio"):
    db_path = tmp_path / "media.db"
    initialize_database(db_path)
    aggregate_book_id = "book123"
    source_chapter_id = "sample_audio_books:abc"
    aggregate_chapter_url = make_aggregate_chapter_url(
        aggregate_book_id=aggregate_book_id,
        source_chapter_id=source_chapter_id,
        title="第01集",
        index=1,
    )
    chapter_id = encode_chapter_id(VIRTUAL_SOURCE_ID, aggregate_chapter_url)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO aggregate_book_tasks
            (aggregate_book_id, name, author, aggregate_payload_json, primary_source_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
            """,
            (
                aggregate_book_id,
                "有声示例·时光电台",
                "林晚",
                json.dumps({"name": "有声示例·时光电台", "author": "林晚", "contentType": content_kind, "sources": []}, ensure_ascii=False),
                "sample_audio_books",
            ),
        )
        conn.execute(
            """
            INSERT INTO aggregate_chapter_tasks
            (chapter_id, aggregate_book_id, source_chapter_id, chapter_index, title, status, created_at, updated_at)
            VALUES (?, ?, ?, 1, '第01集', 'pending', datetime('now'), datetime('now'))
            """,
            (chapter_id, aggregate_book_id, source_chapter_id),
        )
        conn.commit()
    return db_path, aggregate_book_id, source_chapter_id, chapter_id


def test_media_chapter_write_and_aggregate_read(tmp_path):
    db_path, aggregate_book_id, source_chapter_id, chapter_id = _setup_media_book(tmp_path)
    processor = AggregateProcessor(db_path=db_path)
    processor._write_media_chapter_result(
        aggregate_book_id=aggregate_book_id,
        chapter_id=chapter_id,
        title="第01集",
        chapter_index=1,
        source_chapter_id=source_chapter_id,
        source_id="sample_audio_books",
        media_kind="audio",
        media_url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
        media_mime="audio/mpeg",
        duration_seconds=301.0,
        auth_required=False,
        is_paid=False,
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, content_file_path, preview_only FROM aggregate_chapter_tasks WHERE chapter_id = ?",
            (chapter_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "processed"
    assert str(row[1]).endswith(".json")
    assert not row[2]

    response = processor.aggregate_chapter_response(make_aggregate_chapter_url(
        aggregate_book_id=aggregate_book_id,
        source_chapter_id=source_chapter_id,
        title="第01集",
        index=1,
    ), chapter_id=chapter_id)
    assert response["format"] == "audio"
    assert response["mediaUrl"] == "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
    assert response["mediaType"] == "audio/mpeg"
    assert response["durationSeconds"] == 301.0
    assert response["content"] == ""
    assert response["sourceId"] == "sample_audio_books"


def test_media_chapter_shared_read(tmp_path):
    db_path, aggregate_book_id, source_chapter_id, chapter_id = _setup_media_book(tmp_path)
    processor = AggregateProcessor(db_path=db_path)
    processor._write_media_chapter_result(
        aggregate_book_id=aggregate_book_id,
        chapter_id=chapter_id,
        title="第01集",
        chapter_index=1,
        source_chapter_id=source_chapter_id,
        source_id="sample_audio_books",
        media_kind="audio",
        media_url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
        media_mime="audio/mpeg",
        duration_seconds=262.0,
        auth_required=False,
        is_paid=False,
    )
    service = LibraryBooksService(
        db_path=db_path,
        shared_book_storage=SharedBookStorage(root=db_path.parent / "library"),
    )
    shared = service.read_shared_chapter(chapter_id, published_only=False)
    assert shared is not None
    assert shared["format"] == "audio"
    assert shared["mediaUrl"] == "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3"
    assert shared["content"] == ""
    detail = service.get_shared_book_detail(aggregate_book_id)
    assert detail["found"] is True
    assert detail["book"]["contentType"] == "audio"


def test_media_chapter_index_and_storage_consistency(tmp_path):
    db_path, aggregate_book_id, source_chapter_id, chapter_id = _setup_media_book(tmp_path)
    processor = AggregateProcessor(db_path=db_path)
    processor._write_media_chapter_result(
        aggregate_book_id=aggregate_book_id,
        chapter_id=chapter_id,
        title="第01集",
        chapter_index=1,
        source_chapter_id=source_chapter_id,
        source_id="sample_audio_books",
        media_kind="video",
        media_url="https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        media_mime="application/vnd.apple.mpegurl",
        duration_seconds=596.0,
        auth_required=False,
        is_paid=False,
    )
    storage = SharedBookStorage(root=db_path.parent / "library")
    with sqlite3.connect(db_path) as conn:
        book_name = conn.execute(
            "SELECT name FROM aggregate_book_tasks WHERE aggregate_book_id = ?", (aggregate_book_id,)
        ).fetchone()[0]
    index_payload = storage._read_json(storage.chapter_index_path(book_name=book_name, author="林晚")) or {}
    entries = index_payload.get("chapters") or []
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "readable"
    assert entry["contentType"] == "video"
    assert entry["file"].endswith(".json")
    # trace validation must tolerate media descriptor JSON files
    check = storage.check_chapter_traces(book_name=book_name, author="林晚")
    assert check["valid"] is True
    assert check["broken"] == []
