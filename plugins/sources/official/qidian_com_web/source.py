"""Qidian official source plugin (Web protocol).

Capabilities:
- official-source auth hooks (cookie + phone-code login)
- mobile-site search/detail/toc/chapter/explore via pageContext SSR
- chapter reviews (mobile JSON API)
- Web-session keepalive: refreshes short-lived ywguid/ywkey/ticket from the
  long-lived ``alk`` cookie via ``ptlogin.yuewen.com/login/checkStatus``.
  Triggered lazily in ``auth_status`` and on runtime auth failures.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from html import unescape
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote, urljoin


WEB_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 26_4_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "FxiOS/121.0 Mobile/15E148 Safari/605.1.15"
)


# ---------------------------------------------------------------------------
# Private-module loader
# ---------------------------------------------------------------------------
# Private sibling modules (under ``private/``) are loaded by absolute file
# location so the plugin works without being on sys.path.  This mirrors the
# pattern already used by the APP plugin.

def _load_private_module(module_name: str) -> ModuleType:
    """Load a private sibling module by absolute file location."""
    file_path = Path(__file__).with_name("private") / f"{module_name}.py"
    cache_name = f"_qidian_com_web_private_{module_name}"
    if cache_name in sys.modules:
        return sys.modules[cache_name]
    spec = importlib.util.spec_from_file_location(cache_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_name] = mod
    spec.loader.exec_module(mod)
    return mod


class Source:
    id = "qidian_com_web"
    name = "起点中文网(Web)"
    contract_version = "1.0"
    last_modified = "2026-07-18"
    base_url = "https://www.qidian.com"
    mobile_base_url = "https://m.qidian.com"
    _toc_word_cache: dict[str, dict[str, int]] = {}

    # Substrings in an error message / response that indicate the current
    # login session has gone stale and a keepalive refresh is worth trying.
    _AUTH_FAILURE_HINTS = (
        "401", "403",
        "passport.qidian.com", "passport.yuewen.com",
        "ptlogin", "loginrequired", "needlogin", "need_login",
        "notlogin", "not_login", "unlogin",
    )

    async def auth_status(self, ctx):
        """Check Qidian login state from stored cookies.

        Strategy:
        1. Collect cookies from all qidian.com / yuewen.com subdomains.
        2. Verify presence of BOTH key login markers (ywguid AND ywkey).
        3. Probe a mobile page and look for explicit user-state fields.

        State model:
        - authenticated: True  -> confirmed logged in (nickname/username or phone)
        - authStatus: "pending" -> cookies saved but not yet confirmed active
        - authStatus: "unknown" / no cookies -> not logged in
        """
        auth_domains = ("qidian.com", "www.qidian.com", "m.qidian.com", "yuewen.com")

        def _cookie_snapshot() -> tuple[dict[str, str], list[str]]:
            all_items: dict[str, str] = {}
            domains: list[str] = []
            # Web login markers may live on qidian.com or yuewen.com domains.
            for domain in auth_domains:
                jar = ctx.cookies.get(domain)
                if isinstance(jar, dict):
                    domains.append(domain)
                    all_items.update(jar)
            return all_items, sorted(domains)

        all_cookies, cookie_domains = _cookie_snapshot()

        metadata = getattr(self, "metadata", None) or {}
        base_status = {
            "sourceId": self.id,
            "mode": (metadata.get("auth") or {}).get("mode", "optional"),
            "hasCookies": bool(all_cookies),
            "cookieDomains": cookie_domains,
        }

        if not all_cookies:
            return {
                **base_status,
                "authenticated": False,
                "authStatus": "anonymous",
                "accountName": "",
                "expiresAt": "",
                "message": "未检测到起点登录 Cookie",
                "requiredActions": ["manual_login"],
            }

        # Critical fields for Web login: ywguid + ywkey must BOTH be present.
        has_ywguid = bool(all_cookies.get("ywguid"))
        has_ywkey = bool(all_cookies.get("ywkey"))
        if not (has_ywguid and has_ywkey):
            refresh = await self.refresh_auth(
                ctx, reason="auth_status_missing_short_cookie", force=True
            )
            if not refresh.get("ok"):
                missing = []
                if not has_ywguid:
                    missing.append("ywguid")
                if not has_ywkey:
                    missing.append("ywkey")
                return {
                    **base_status,
                    "authenticated": False,
                    "authStatus": refresh.get("authStatus", "unknown"),
                    "accountName": "",
                    "expiresAt": "",
                    "message": refresh.get("message")
                    or f"Cookie 不完整，缺少关键登录态字段（{', '.join(missing)}）",
                    "requiredActions": refresh.get("requiredActions") or ["manual_login"],
                }
            all_cookies, cookie_domains = _cookie_snapshot()
            base_status = {
                **base_status,
                "hasCookies": bool(all_cookies),
                "cookieDomains": cookie_domains,
            }

        # Lazy keepalive: use the unified refresh entrypoint.  When short-lived
        # cookies are present this is a no-op unless forced; when they are
        # missing but alk exists, refresh_auth has already tried to recover.
        # Failures do NOT flip an authenticated session offline unless the
        # server explicitly says alk is dead (code=10521).
        refresh_status = await self.refresh_auth(ctx, reason="auth_status", force=False)
        web_refresh = (refresh_status.get("layers") or {}).get("web") or {}
        if web_refresh.get("reason") == "alk_expired":
            # alk is gone (>15d) — session is unrecoverable without re-login.
            # Drop the stale ywkey so the next call doesn't trust it.
            if ctx and hasattr(ctx, "cookies"):
                for domain in ("qidian.com", "yuewen.com"):
                    try:
                        jar = ctx.cookies.get(domain)
                        if isinstance(jar, dict):
                            jar.pop("ywkey", None)
                    except Exception:
                        pass
            return {
                **base_status,
                "authenticated": False,
                "authStatus": "expired",
                "accountName": "",
                "expiresAt": "",
                "message": "alk 已过期（超过15天），请重新登录",
                "requiredActions": ["relogin"],
            }
        # Refresh cookies are already applied back into ctx by _try_keepalive.

        # Probe the mobile user center page: it exposes explicit login state
        # and is more reliable than the homepage.
        try:
            html = await ctx.access.http.fetch_text(
                f"{self.mobile_base_url}/user/",
                headers=self._headers(),
            )
            page_data = self._page_data(html)
        except Exception as exc:
            return {
                **base_status,
                "authenticated": False,
                "authStatus": "pending",
                "accountName": "",
                "expiresAt": "",
                "message": f"Cookie 存在但页面探测异常：{exc}",
                "requiredActions": ["check_auth_status"],
            }

        if not page_data:
            return {
                **base_status,
                "authenticated": False,
                "authStatus": "pending",
                "accountName": "",
                "expiresAt": "",
                "message": "Cookie 存在但页面未返回有效数据，可能已失效",
                "requiredActions": ["check_auth_status"],
            }

        user_info = page_data.get("user") or page_data.get("userInfo") or {}
        if not isinstance(user_info, dict):
            user_info = {}

        is_login = bool(user_info.get("isLogin") or user_info.get("isIsLogin"))
        account_name = self._account_identity_from_web_user(user_info)

        if is_login and account_name:
            return {
                **base_status,
                "authenticated": True,
                "authStatus": "authenticated",
                "accountName": account_name,
                "expiresAt": "",
                "message": f"已登录：{account_name}",
                "requiredActions": [],
            }

        # Cookies were accepted but the user center says not logged in.
        return {
            **base_status,
            "authenticated": False,
            "authStatus": "pending",
            "accountName": "",
            "expiresAt": "",
            "message": "Cookie 已保存，但用户中心未识别登录态",
            "requiredActions": ["check_auth_status"],
        }

    async def prepare_login(self, ctx):
        return {
            "sourceId": self.id,
            "mode": "manual_browser",
            "loginUrl": "https://passport.qidian.com/",
            "instructions": "在打开的浏览器中完成起点登录（会自动重定向到阅文登录页），然后回到官方源登录页刷新状态。",
            "cookieDomains": ["qidian.com", "www.qidian.com", "m.qidian.com", "yuewen.com", "ptlogin.qidian.com", "ptlogin.yuewen.com"],
        }

    async def after_login(self, ctx):
        return await self.auth_status(ctx)

    async def refresh_auth(self, ctx, reason: str = "manual", force: bool = False) -> dict:
        """Unified Web auth refresh entrypoint.

        This is the single renewal path used by manual checks, scheduled
        probes, auth_status, and runtime auth-failure recovery.
        """
        jar = self._ctx_cookie_jar(ctx)
        flat: dict[str, str] = {}
        for cookies in jar.values():
            if isinstance(cookies, dict):
                flat.update(cookies)

        has_ywguid = bool(flat.get("ywguid"))
        has_ywkey = bool(flat.get("ywkey"))
        has_alk = bool(flat.get("alk"))

        web_layer = {
            "ok": False,
            "refreshed": False,
            "hasYwGuid": has_ywguid,
            "hasYwKey": has_ywkey,
            "hasAlk": has_alk,
        }
        base = {
            "sourceId": self.id,
            "ok": False,
            "refreshed": False,
            "authStatus": "unknown",
            "accountName": "",
            "expiresAt": "",
            "layers": {
                "web": web_layer,
                "app": {
                    "ok": False,
                    "skipped": True,
                    "reason": "web_plugin",
                },
            },
            "requiredActions": [],
            "message": "",
            "debug": {"reason": reason},
        }

        if not jar:
            return {
                **base,
                "authStatus": "anonymous",
                "message": "未检测到起点登录 Cookie",
                "requiredActions": ["manual_login"],
                "layers": {
                    **base["layers"],
                    "web": {**web_layer, "reason": "no_cookie"},
                },
            }

        if not force and has_ywguid and has_ywkey:
            return {
                **base,
                "ok": True,
                "authStatus": "pending",
                "message": "Web 登录短期 Cookie 存在，未强制续期",
                "layers": {
                    **base["layers"],
                    "web": {**web_layer, "ok": True, "reason": "already_has_short_session"},
                },
            }

        if not has_alk:
            return {
                **base,
                "authStatus": "unknown",
                "message": "缺少 alk，无法自动续期",
                "requiredActions": ["manual_login"],
                "layers": {
                    **base["layers"],
                    "web": {**web_layer, "reason": "no_alk"},
                },
            }

        result = await self._run_web_keepalive(ctx, jar)
        if result.get("ok"):
            return {
                **base,
                "ok": True,
                "refreshed": True,
                "authStatus": "pending",
                "accountName": "",
                "message": "Web 凭据续期成功，等待账户身份确认",
                "layers": {
                    **base["layers"],
                    "web": {
                        **web_layer,
                        "ok": True,
                        "refreshed": True,
                        "hasYwGuid": True,
                        "hasYwKey": True,
                        "reason": "refreshed",
                    },
                },
            }

        reason_code = result.get("reason") or "server"
        required_actions = ["relogin"] if reason_code == "alk_expired" else ["check_auth_status"]
        return {
            **base,
            "authStatus": "expired" if reason_code == "alk_expired" else "pending",
            "message": result.get("message") or f"Web 登录态续期失败：{reason_code}",
            "requiredActions": required_actions,
            "layers": {
                **base["layers"],
                "web": {**web_layer, "reason": reason_code},
            },
        }

    # ------------------------------------------------------------------
    # Keepalive: refresh short-lived Web credentials from alk
    # ------------------------------------------------------------------

    def _ctx_cookie_jar(self, ctx) -> dict[str, dict[str, str]]:
        """Collect cookies from all login-related domains into a domain-keyed jar."""
        jar: dict[str, dict[str, str]] = {}
        if not ctx or not hasattr(ctx, "cookies"):
            return jar
        for domain in ("qidian.com", "www.qidian.com", "m.qidian.com", "yuewen.com"):
            try:
                value = ctx.cookies.get(domain)
            except Exception:
                continue
            if isinstance(value, dict):
                jar[domain] = dict(value)
        return jar

    @staticmethod
    def _account_identity_from_web_user(user_info: dict) -> str:
        """Extract only explicit account identity from Web user-center payload."""
        for key in (
            "nickName",
            "userName",
            "name",
            "mobilePhone",
            "mobile",
            "phone",
            "bindPhone",
            "phoneNumber",
            "phoneMasked",
            "mobileMasked",
        ):
            value = user_info.get(key) if isinstance(user_info, dict) else None
            if value:
                return str(value).strip()
        return ""

    def _apply_refreshed_cookies(self, ctx, jar: dict[str, dict[str, str]]) -> None:
        """Write refreshed cookies back into the plugin context.

        ``ctx.cookies.set(domain, cookie_dict)`` replaces the whole domain jar,
        so we merge the refreshed login markers on top of the existing cookies
        to preserve custom fields like ``ck``.
        """
        if not ctx or not hasattr(ctx, "cookies"):
            return
        setter = getattr(ctx.cookies, "merge", None) or getattr(ctx.cookies, "set", None)
        if not callable(setter):
            return
        login_markers = ("ywguid", "ywkey", "ywopenid", "ticket", "alk")
        for domain, cookies in jar.items():
            refreshed = {k: v for k, v in cookies.items() if k in login_markers and v}
            if not refreshed:
                continue
            existing = ctx.cookies.get(domain) or {}
            if not isinstance(existing, dict):
                existing = {}
            try:
                setter(domain, {**existing, **refreshed})
            except Exception:
                pass

    async def _run_web_keepalive(self, ctx, jar: dict[str, dict[str, str]]) -> dict:
        """Run a Web-session refresh via the private keepalive module.

        Returns the raw result dict from ``web_keepalive.refresh_web_session``
        (or ``{"ok": False, "reason": "unavailable"}`` when the module or alk
        is missing).  Never raises — keepalive is best-effort.
        """
        if not ctx:
            return {"ok": False, "reason": "no_ctx"}
        if not jar:
            return {"ok": False, "reason": "no_cookies"}
        # Only refresh when we actually have an alk to refresh from.
        has_alk = any(
            (cookies or {}).get("alk")
            for cookies in jar.values()
        )
        if not has_alk:
            return {"ok": False, "reason": "no_alk"}
        try:
            keepalive_mod = _load_private_module("web_keepalive")
        except Exception as exc:
            ctx.trace("qidian.keepalive.load", message=f"error={exc}")
            return {"ok": False, "reason": "unavailable"}
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, keepalive_mod.refresh_web_session, jar
            )
        except Exception as exc:
            ctx.trace("qidian.keepalive", message=f"error={exc}")
            return {"ok": False, "reason": "error", "message": str(exc)}
        if result.get("ok"):
            self._apply_refreshed_cookies(ctx, jar)
            ctx.trace(
                "qidian.keepalive",
                message=f"refreshed ok, ywguid={result.get('account', {}).get('ywguid', '')}",
            )
        else:
            ctx.trace("qidian.keepalive", message=f"reason={result.get('reason')}")
        return result

    async def _try_keepalive(self, ctx) -> dict:
        """Compatibility wrapper for older call sites.

        New code must call ``refresh_auth`` so manual, scheduled, and runtime
        refreshes share the same result shape and retry policy.
        """
        return await self.refresh_auth(ctx, reason="legacy_try_keepalive", force=True)

    def _looks_like_auth_failure(self, exc: BaseException | str) -> bool:
        """Heuristic: does this error/response indicate a stale login session?"""
        text = str(exc).lower()
        return any(hint in text for hint in self._AUTH_FAILURE_HINTS)

    async def _refresh_after_auth_failure(self, ctx, label: str) -> dict:
        """Refresh auth after a feature request hits an auth-like failure."""
        refresh = await self.refresh_auth(ctx, reason=f"{label}_auth_failure", force=True)
        if ctx:
            web_layer = (refresh.get("layers") or {}).get("web") or {}
            ctx.trace(
                "qidian.auth.refresh_after_failure",
                message=f"label={label} ok={refresh.get('ok')} reason={web_layer.get('reason', '')}",
            )
        return refresh

    async def _fetch_text_keepalive(
        self, ctx, url: str, *, headers: dict | None = None, label: str = ""
    ) -> str:
        """fetch_text with one automatic keepalive retry on auth failure."""
        try:
            return await ctx.access.http.fetch_text(url, headers=headers or self._headers())
        except Exception as exc:
            if not self._looks_like_auth_failure(exc):
                raise
            ctx.trace(
                "qidian.fetch.auth_failure",
                url=url, message=f"label={label} error={exc}",
            )
            refresh = await self._refresh_after_auth_failure(ctx, label or "fetch_text")
            if not refresh.get("ok"):
                raise
            # Retry once with refreshed cookies.
            return await ctx.access.http.fetch_text(url, headers=headers or self._headers())

    async def _fetch_json_keepalive(
        self, ctx, url: str, *, params: dict | None = None,
        headers: dict | None = None, label: str = ""
    ) -> dict:
        """fetch_json with one automatic keepalive retry on auth failure."""
        try:
            return await ctx.access.http.fetch_json(
                url, params=params, headers=headers or self._headers()
            )
        except Exception as exc:
            if not self._looks_like_auth_failure(exc):
                raise
            ctx.trace(
                "qidian.fetch.auth_failure",
                url=url, message=f"label={label} error={exc}",
            )
            refresh = await self._refresh_after_auth_failure(ctx, label or "fetch_json")
            if not refresh.get("ok"):
                raise
            return await ctx.access.http.fetch_json(
                url, params=params, headers=headers or self._headers()
            )

    async def chapter_reviews(self, ctx, chapter_url: str) -> dict:
        """Fetch chapter reviews, preferring the private implementation."""
        try:
            reviews_mod = _load_private_module("reviews")
            reviews_impl = reviews_mod.chapter_reviews
        except Exception:
            reviews_impl = None

        if reviews_impl:
            try:
                result = await reviews_impl(ctx, chapter_url)
                if result.get("debug", {}).get("error"):
                    return await self._public_chapter_reviews(ctx, chapter_url)
                return result
            except Exception as exc:
                if ctx:
                    ctx.trace("qidian.reviews.private", url=chapter_url, message=f"error={exc}")
                return {
                    "paragraphs": {},
                    "chapterEnd": [],
                    "summary": {},
                    "debug": {"error": f"reviews private plugin error: {exc}"},
                }

        return await self._public_chapter_reviews(ctx, chapter_url)

    async def author_say(self, ctx, chapter_url: str) -> dict:
        """Return Web-side author note when present on the chapter page."""
        try:
            html = await self._fetch_text_keepalive(ctx, chapter_url, label="author_say")
            page_data = self._page_data(html)
        except Exception as exc:
            return {"content": "", "hasContent": False, "debug": {"error": str(exc)}}

        chapter_info = page_data.get("chapterInfo") or {}
        candidate_fields = (
            chapter_info.get("authorSay"),
            chapter_info.get("authorWords"),
            chapter_info.get("authorWordsContent"),
            chapter_info.get("authorRemark"),
        )
        content = ""
        for value in candidate_fields:
            text = self._text(value or "")
            if text:
                content = text
                break
        return {
            "content": content,
            "hasContent": bool(content),
            "source": "web",
            "debug": {"supported": bool(content)},
        }

    async def chapter_say(
        self,
        ctx,
        chapter_url: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Return Web chapter-end comments as a lightweight chapter-say shell."""
        book_id, chapter_id = self._parse_chapter_identifiers(chapter_url)
        if not book_id or not chapter_id:
            return {"comments": [], "hotComments": [], "normalComments": [], "source": "web", "debug": {"error": "invalid qidian chapter url"}}

        hot_comments: list[dict[str, Any]] = []
        chapter_end: list[dict[str, Any]] = []
        total_count = 0
        page = max(1, int(page))
        page_size = max(1, min(50, int(page_size)))
        try:
            reviews_mod = _load_private_module("reviews")
            csrf_token = self._csrf_token_from_cookies(ctx)
            if not csrf_token:
                csrf_token = await self._bootstrap_csrf_token(ctx, chapter_url)
            if not csrf_token:
                raise RuntimeError("missing _csrfToken cookie")
            headers = self._headers({"Referer": chapter_url or self.mobile_base_url})
            if page == 1 and hasattr(reviews_mod, "_fetch_chapter_end_comments"):
                hot_comments = await reviews_mod._fetch_chapter_end_comments(
                    ctx,
                    str(book_id),
                    str(chapter_id),
                    csrf_token,
                    headers,
                    max_pages=1,
                )
            if hasattr(reviews_mod, "_fetch_chapter_end_reviews"):
                chapter_end, total_count = await reviews_mod._fetch_chapter_end_reviews(
                    ctx,
                    str(book_id),
                    str(chapter_id),
                    csrf_token,
                    headers,
                    max_pages=page,
                )
                start = (page - 1) * page_size
                chapter_end = chapter_end[start:start + page_size]
        except Exception:
            reviews = await self.chapter_reviews(ctx, chapter_url)
            chapter_end = reviews.get("chapterEnd") or []
            hot_comments = reviews.get("chapterEndHot") or []

        merged = []
        seen_ids: set[str] = set()
        for tier, bucket in (("hot", hot_comments), ("normal", chapter_end)):
            for item in bucket:
                if not isinstance(item, dict):
                    continue
                review_id = str(item.get("id") or "")
                if review_id and review_id in seen_ids:
                    continue
                merged_item = dict(item)
                merged_item["commentTier"] = tier
                merged.append(merged_item)
                if review_id:
                    seen_ids.add(review_id)
        return {
            "comments": merged,
            "hotComments": hot_comments,
            "normalComments": chapter_end,
            "totalCount": total_count or len(merged),
            "page": page,
            "pageSize": page_size,
            "hasMore": page * page_size < total_count,
            "nextPage": page + 1 if page * page_size < total_count else None,
            "source": "web",
        }

    async def paragraph_say(
        self,
        ctx,
        chapter_url: str,
        paragraph_id: int | str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Fetch one Web paragraph directly instead of loading the whole chapter."""
        try:
            book_id, chapter_id = self._parse_chapter_identifiers(chapter_url)
            parsed_paragraph_id = int(paragraph_id)
            page = max(1, int(page))
            page_size = max(1, min(50, int(page_size)))
            if not book_id or not chapter_id:
                raise ValueError("invalid qidian chapter url")
            reviews_mod = _load_private_module("reviews")
            csrf_token = self._csrf_token_from_cookies(ctx)
            if not csrf_token:
                csrf_token = await self._bootstrap_csrf_token(ctx, chapter_url)
            if not csrf_token:
                raise RuntimeError("missing _csrfToken cookie")
            comments, total = await reviews_mod._fetch_paragraph_review_page(
                ctx,
                str(book_id),
                str(chapter_id),
                parsed_paragraph_id,
                csrf_token,
                self._headers({"Referer": chapter_url or self.mobile_base_url}),
                page=page,
                page_size=page_size,
            )
            hot_comments = sorted(
                (dict(item) for item in comments if isinstance(item, dict)),
                key=lambda item: self._safe_int(item.get("likeNum"), 0),
                reverse=True,
            )[:3]
            return {
                "paragraphId": parsed_paragraph_id,
                "comments": comments,
                "hotComments": hot_comments,
                "normalComments": comments,
                "totalCount": total,
                "page": page,
                "pageSize": page_size,
                "hasMore": bool(comments) and page * page_size < total,
                "nextPage": page + 1 if comments and page * page_size < total else None,
                "source": "web",
            }
        except Exception as exc:
            if ctx:
                ctx.trace(
                    "qidian.paragraph_say.web",
                    url=chapter_url,
                    message=f"paragraphId={paragraph_id} error={exc}",
                )
            return {
                "paragraphId": int(paragraph_id),
                "comments": [],
                "hotComments": [],
                "normalComments": [],
                "totalCount": 0,
                "source": "web",
                "debug": {"error": str(exc)},
            }

    async def paragraph_reviews(
        self, ctx, chapter_url: str, paragraph_id: int,
        *, page: int = 1, page_size: int = 20,
    ) -> dict:
        """Expose the host's paragraph-detail method without loading other paragraphs."""
        return await self.paragraph_say(
            ctx, chapter_url, paragraph_id, page=page, page_size=page_size,
        )

    async def review_replies(
        self, ctx, chapter_url: str, root_review_id: int,
        *, page: int = 1, page_size: int = 20, cursor_id: int = 0,
    ) -> dict:
        """Web reviewlist4m embeds limited replies; no verified full-reply API exists."""
        return {
            "rootReviewId": str(root_review_id), "replies": [],
            "totalCount": 0, "hasMore": False, "source": "web",
            "debug": {"error": "web_full_replies_unavailable", "supported": False},
        }

    async def vip_chapter_preview(
        self,
        ctx,
        chapter_url: str,
        *,
        without_limit_free: bool = False,
    ) -> dict:
        """Return Web VIP preview shell based on SSR chapter data."""
        result = await self.chapter(ctx, chapter_url)
        extra = result.get("extra") or {}
        words_count = extra.get("wordsCount", 0)
        actual_words = extra.get("actualWords", 0)
        book_id, chapter_id = self._parse_chapter_identifiers(chapter_url)
        if book_id and chapter_id:
            toc_words = ((self._toc_word_cache.get(str(book_id)) or {}).get(str(chapter_id))) or 0
            if toc_words:
                words_count = toc_words
                if not actual_words:
                    actual_words = toc_words
        return {
            "bookId": str(extra.get("bookId") or ""),
            "chapterId": str(extra.get("chapterId") or ""),
            "summary": result.get("content", ""),
            "wordsCount": words_count,
            "actualWords": actual_words,
            "price": extra.get("price", 0),
            "isPaid": result.get("isPaid", False),
            "previewOnly": extra.get("previewOnly", False),
            "withoutLimitFree": bool(without_limit_free),
            "source": "web",
        }

    async def vip_chapter_words(self, ctx, chapter_url: str) -> dict:
        """Return Web word-count shell for one chapter."""
        preview = await self.vip_chapter_preview(ctx, chapter_url)
        return {
            "bookId": str(preview.get("bookId") or ""),
            "chapterId": str(preview.get("chapterId") or ""),
            "wordsCount": preview.get("wordsCount", 0),
            "actualWords": preview.get("actualWords", 0),
            "source": "web",
        }

    async def vip_unbought_chapters(self, ctx, book_url: str) -> dict:
        """Return Web chapter-list shell for batch VIP metadata."""
        toc_url = self._catalog_url(self._book_id_from_url(book_url) or "")
        chapters = await self.toc(ctx, toc_url)
        normalized = []
        for item in chapters:
            if not isinstance(item, dict):
                continue
            if not item.get("isVip"):
                continue
            extra = item.get("extra") or {}
            normalized.append(
                {
                    "chapterId": str(self._parse_chapter_identifiers(item.get("chapterUrl", ""))[1] or ""),
                    "chapterName": item.get("title", ""),
                    "wordsCount": extra.get("wordCount", 0),
                    "source": "web",
                }
            )
        return {
            "bookId": str(self._book_id_from_url(book_url) or ""),
            "chapters": normalized,
            "source": "web",
            "debug": {"priceSupported": False},
        }

    async def _public_chapter_reviews(self, ctx, chapter_url: str) -> dict:
        book_id, chapter_id = self._parse_chapter_identifiers(chapter_url)
        if not book_id or not chapter_id:
            return self._empty_reviews("invalid qidian chapter url", auth_mode="public")

        csrf_token = self._csrf_token_from_cookies(ctx)
        if not csrf_token:
            csrf_token = await self._bootstrap_csrf_token(ctx, chapter_url)
        if not csrf_token:
            return self._empty_reviews("missing _csrfToken cookie", auth_mode="public")

        auth_mode = self._review_auth_mode(ctx, csrf_token)
        referer = chapter_url or self.mobile_base_url
        try:
            summary_response = await self._fetch_json_keepalive(
                ctx,
                f"{self.mobile_base_url}/webcommon/chapterreview/reviewsummary4m",
                params={
                    "bookId": book_id,
                    "chapterId": chapter_id,
                    "_csrfToken": csrf_token,
                },
                headers=self._headers({"Referer": referer}),
                label="reviewsummary4m",
            )
        except Exception as exc:
            if ctx:
                ctx.trace("qidian.reviews.summary", url=chapter_url, message=f"error={exc}")
            return self._empty_reviews(
                f"reviewsummary4m failed: {exc}",
                auth_mode=auth_mode,
                bookId=book_id,
                chapterId=chapter_id,
            )

        summary_payload = summary_response.get("data") if isinstance(summary_response, dict) else {}
        summary_list = summary_payload.get("list") if isinstance(summary_payload, dict) else []
        if not isinstance(summary_list, list):
            summary_list = []

        paragraphs_with_reviews = [
            self._safe_int(item.get("paragraphId"))
            for item in summary_list
            if isinstance(item, dict)
            and self._safe_int(item.get("paragraphId")) is not None
            and self._safe_int(item.get("paragraphId")) >= 0
            and self._safe_int(item.get("reviewNum"), 0) > 0
        ]
        chapter_end_count = 0
        for item in summary_list:
            if not isinstance(item, dict):
                continue
            if self._safe_int(item.get("paragraphId")) == -1:
                chapter_end_count = self._safe_int(item.get("reviewNum"), 0)
                break

        # Cap the number of paragraphs fetched to avoid hammering the server
        # on extreme chapters, but raise the old hard limit of 20.
        max_paragraphs = 100
        truncated = len(paragraphs_with_reviews) - max_paragraphs
        fetched_paragraphs = paragraphs_with_reviews[:max_paragraphs]

        shared = {
            "ctx": ctx, "book_id": book_id, "chapter_id": chapter_id,
            "csrf_token": csrf_token, "headers": {"Referer": referer},
        }
        # This fallback stays serial so request concurrency remains owned by
        # the host. The normal reviews path is implemented by private/reviews.py.
        paragraphs: dict[str, list[dict[str, Any]]] = {}
        for paragraph_id in fetched_paragraphs:
            comments = await self._fetch_review_list(
                paragraph_id=paragraph_id, **shared,
            )
            if comments:
                paragraphs[str(paragraph_id)] = comments
        chapter_end = await self._fetch_review_list(paragraph_id=-1, **shared)

        summary = {
            "totalParagraphs": len(paragraphs_with_reviews),
            "totalReviews": self._safe_int(summary_payload.get("total"), 0),
            "paragraphsWithReviews": paragraphs_with_reviews,
            "fetchedParagraphs": fetched_paragraphs,
            "chapterEndCount": chapter_end_count,
            "authMode": auth_mode,
        }
        debug: dict[str, Any] = {}
        if truncated > 0:
            debug["truncatedParagraphCount"] = truncated
        if chapter_end_count and not chapter_end:
            debug["chapterEndExpected"] = chapter_end_count

        result: dict[str, Any] = {
            "paragraphs": paragraphs,
            "chapterEnd": chapter_end,
            "summary": summary,
        }
        if debug:
            result["debug"] = debug
        return result

    async def _bootstrap_csrf_token(self, ctx, chapter_url: str) -> str:
        if not ctx or not hasattr(ctx, "access"):
            return ""
        bootstrap_urls = [url for url in [chapter_url, self.mobile_base_url] if url]
        for bootstrap_url in bootstrap_urls:
            try:
                await self._fetch_text_keepalive(
                    ctx, bootstrap_url, label="csrf_bootstrap",
                )
            except Exception:
                continue
            token = self._csrf_token_from_cookies(ctx)
            if token:
                return token
        return ""

    async def _fetch_review_list(
        self,
        ctx,
        book_id: int,
        chapter_id: int,
        paragraph_id: int,
        csrf_token: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not ctx:
            return []
        try:
            response = await self._fetch_json_keepalive(
                ctx,
                f"{self.mobile_base_url}/webcommon/chapterreview/reviewlist4m",
                params={
                    "bookId": book_id,
                    "chapterId": chapter_id,
                    "paragraphId": paragraph_id,
                    "page": 1,
                    "_csrfToken": csrf_token,
                },
                headers=self._headers({"Referer": headers.get("Referer", self.mobile_base_url)}),
                label=f"reviewlist4m[{paragraph_id}]",
            )
        except Exception as exc:
            ctx.trace(
                "qidian.reviews.list",
                url=headers.get("Referer", ""),
                message=f"paragraphId={paragraph_id} error={exc}",
            )
            return []

        data = response.get("data") if isinstance(response, dict) else {}
        items = data.get("list") if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []
        return [self._normalize_review_item(item, paragraph_id) for item in items if isinstance(item, dict)]

    def _normalize_review_item(self, item: dict[str, Any], paragraph_id: int) -> dict[str, Any]:
        user_info = item.get("userInfo") if isinstance(item.get("userInfo"), dict) else {}
        review_id = (
            item.get("reviewId")
            or item.get("id")
            or item.get("commentId")
            or item.get("replyId")
            or ""
        )
        like_num = self._safe_int(
            item.get("likeAmount", item.get("likeNum", item.get("praiseNum", 0))),
            0,
        )
        reply_list = item.get("replyList") if isinstance(item.get("replyList"), list) else []
        reply_count = self._safe_int(item.get("replyCount", item.get("replyAmount", len(reply_list))), len(reply_list))
        normalized = {
            "id": str(review_id),
            "content": self._text(item.get("content", "")),
            "userName": self._text(
                item.get("userName")
                or item.get("nickName")
                or user_info.get("nickName")
                or item.get("authorName")
                or ""
            ),
            "likeNum": like_num,
            "replyCount": reply_count,
            "reviewTime": self._text(
                item.get("createTimeStr")
                or item.get("ctimeDesc")
                or item.get("reviewTime")
                or item.get("timeDesc")
                or ""
            ),
            "paragraphId": paragraph_id,
            "isTop": bool(item.get("isTop") or item.get("top")),
        }
        avatar = self._cover(
            user_info.get("avatar")
            or item.get("avatar")
            or item.get("headImg")
            or ""
        )
        if avatar:
            normalized["avatar"] = avatar
        return normalized

    def _parse_chapter_identifiers(self, chapter_url: str) -> tuple[int | None, int | None]:
        match = re.search(r"/chapter/(\d+)/(\d+)/?", chapter_url or "")
        if not match:
            return None, None
        try:
            return int(match.group(1)), int(match.group(2))
        except ValueError:
            return None, None

    def _csrf_token_from_cookies(self, ctx) -> str:
        if not ctx or not hasattr(ctx, "cookies"):
            return ""
        for domain in (
            "m.qidian.com",
            "qidian.com",
            "www.qidian.com",
            "yuewen.com",
            "ptlogin.qidian.com",
            "ptlogin.yuewen.com",
        ):
            try:
                token = ctx.cookies.get(domain, "_csrfToken")
            except Exception:
                continue
            if token:
                return str(token)
        return ""

    def _review_auth_mode(self, ctx, csrf_token: str) -> str:
        if not ctx or not hasattr(ctx, "cookies"):
            return "public" if csrf_token else "none"
        for domain in ("qidian.com", "m.qidian.com", "www.qidian.com", "yuewen.com"):
            try:
                jar = ctx.cookies.get(domain)
            except Exception:
                continue
            if isinstance(jar, dict) and (jar.get("ywguid") or jar.get("ywkey")):
                return "cookie"
        return "public" if csrf_token else "none"

    def _safe_int(self, value: Any, default: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _empty_reviews(self, error: str, **debug: Any) -> dict:
        payload: dict[str, Any] = {
            "paragraphs": {},
            "chapterEnd": [],
            "summary": {
                "totalParagraphs": 0,
                "totalReviews": 0,
                "paragraphsWithReviews": [],
                "chapterEndCount": 0,
                "authMode": debug.get("auth_mode", "public"),
            },
        }
        debug_payload = {"error": error}
        debug_payload.update(debug)
        payload["debug"] = debug_payload
        return payload

    def _page_data(self, html: str) -> dict:
        match = re.search(
            r'<script id="vite-plugin-ssr_pageContext" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        if not match:
            return {}
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return {}
        page_context = payload.get("pageContext") if isinstance(payload, dict) else {}
        page_props = page_context.get("pageProps") if isinstance(page_context, dict) else {}
        page_data = page_props.get("pageData") if isinstance(page_props, dict) else {}
        return page_data if isinstance(page_data, dict) else {}

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Referer": self.mobile_base_url, "User-Agent": WEB_USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def _text(self, value: str) -> str:
        text = unescape(str(value or ""))
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        # Qidian mobile chapter content often uses bare <p> tags without closing </p>.
        # Treat each <p> as a paragraph boundary.
        text = re.sub(r"<p[^>]*>", "\n\n", text, flags=re.I)
        text = re.sub(r"</p>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(line.strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _cover(self, url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def _book_cover_url(self, book_id: int | str) -> str:
        return f"https://bookcover.yuewen.com/qdbimg/349573/{book_id}/150"

    def _book_url(self, book_id: int | str) -> str:
        return f"{self.mobile_base_url}/book/{book_id}/"

    def _catalog_url(self, book_id: int | str) -> str:
        return f"{self.mobile_base_url}/book/{book_id}/catalog/"

    def _book_id_from_url(self, book_url: str) -> str | None:
        match = re.search(r"/book/(\d+)/?", book_url or "")
        return match.group(1) if match else None

    def _chapter_url(self, book_id: int | str, chapter_id: int | str) -> str:
        return f"{self.mobile_base_url}/chapter/{book_id}/{chapter_id}/"

    def _search_items_from_page_data(self, ctx, page_data: dict) -> list[dict]:
        records = (((page_data.get("bookInfo") or {}).get("records")) or [])
        items = []
        for record in records:
            if not isinstance(record, dict):
                continue
            book_id = record.get("bid", "")
            if not book_id:
                continue
            category = self._text(record.get("cat", ""))
            sub_category = self._text(record.get("subCateName", ""))
            state = self._text(record.get("state", ""))
            kind_parts = [part for part in [category, sub_category, state] if part]
            items.append({
                "sourceId": self.id,
                "name": self._text(record.get("bName", "")),
                "author": self._text(record.get("bAuth", "")),
                "bookUrl": self._book_url(book_id),
                "coverUrl": self._cover(record.get("imgUrl", "")),
                "intro": self._text(record.get("desc", "")),
                "kind": ",".join(kind_parts),
                "lastChapter": self._text(record.get("lastChapterName", "")),
                "wordCount": self._text(record.get("cnt", "")),
                "updateTime": self._text(record.get("lastUpdateTime", "") or record.get("updateTime", "")),
                "extra": {
                    "bookId": book_id,
                    "categoryId": record.get("catId", ""),
                    "subCategoryId": record.get("subCateId", ""),
                    "isVip": bool(record.get("isVip")),
                    "signStatus": self._text(record.get("signStatus", "")),
                },
            })
        return items

    def _book_detail_from_page_data(self, ctx, book_url: str, page_data: dict) -> dict:
        book_info = page_data.get("bookInfo") or {}
        book_extra = page_data.get("bookExtra") or {}
        book_id = book_info.get("bookId") or book_info.get("bid") or page_data.get("bookId") or ""
        sub_category = self._text(book_info.get("subCateName", ""))
        category = self._text(book_info.get("chanName", ""))
        state = self._text(book_info.get("bookStatus", "") or book_info.get("actionStatus", ""))
        kind_parts = [part for part in [category, sub_category, state] if part]
        tags = []
        for tag in (book_extra.get("ugcTagInfos") or []):
            if isinstance(tag, dict):
                name = self._text(tag.get("tagName", ""))
                if name:
                    tags.append(name)
        cover = self._cover(book_info.get("coverUrl", "") or book_info.get("imgUrl", ""))
        if not cover and book_id:
            cover = self._book_cover_url(book_id)
        return {
            "sourceId": self.id,
            "name": self._text(book_info.get("bookName", "")),
            "author": self._text(book_info.get("authorName", "")),
            "bookUrl": book_url,
            "coverUrl": cover,
            "intro": self._text(book_info.get("desc", "")),
            "kind": ",".join(kind_parts),
            "lastChapter": self._text(book_info.get("updChapterName", "")),
            "wordCount": self._text(book_info.get("showWordsCnt", "") or book_info.get("wordsCnt", "")),
            "updateTime": self._text(book_info.get("updTime", "")),
            "tocUrl": self._catalog_url(book_id) if book_id else book_url,
            "authRequired": False,
            "extra": {
                "bookId": book_id,
                "categoryId": book_info.get("chanId", ""),
                "subCategoryId": book_info.get("subCateId", ""),
                "latestChapterUrl": self._cover(book_info.get("updChapterUrl", "")),
                "limitFreeType": book_extra.get("limitFreeType", page_data.get("limitFreeType", 0)),
                "tags": tags,
                "rankName": self._text((book_extra.get("tagInfo") or {}).get("rankName", "")),
                "rankNum": self._text((book_extra.get("tagInfo") or {}).get("rankNum", "")),
                "bookCirclePostCount": book_extra.get("bookCirclePostCount", 0),
            },
        }

    def _toc_from_page_data(self, page_data: dict) -> list[dict]:
        volumes = page_data.get("vs") or []
        book_id = page_data.get("bookId", "")
        chapters = []
        chapter_words: dict[str, int] = {}
        for volume in volumes:
            volume_name = self._text(volume.get("vN", "")) if isinstance(volume, dict) else ""
            for chapter in (volume.get("cs") or []):
                if not isinstance(chapter, dict):
                    continue
                chapter_id = chapter.get("id", "")
                title = self._text(chapter.get("cN", ""))
                if not chapter_id or not title:
                    continue
                # sS: 1=free, 0=vip/paid (opposite of intuitive bool)
                is_vip = not bool(chapter.get("sS", 1))
                chapters.append({
                    "sourceId": self.id,
                    "index": len(chapters) + 1,
                    "title": title,
                    "chapterUrl": self._chapter_url(book_id, chapter_id),
                    "updateTime": self._text(chapter.get("uT", "")),
                    "isVip": is_vip,
                    "isLocked": False,
                    "extra": {
                        "volumeName": volume_name,
                        "wordCount": chapter.get("cnt", 0),
                        "vipFlag": chapter.get("sS", 0),
                    },
                })
                try:
                    chapter_words[str(chapter_id)] = int(chapter.get("cnt", 0) or 0)
                except (TypeError, ValueError):
                    pass
        if book_id and chapter_words:
            self._toc_word_cache[str(book_id)] = chapter_words
        return chapters

    def _chapter_from_page_data(self, ctx, chapter_url: str, page_data: dict) -> dict:
        chapter_info = page_data.get("chapterInfo") or {}
        content = self._text(chapter_info.get("content", ""))
        is_buy = bool(chapter_info.get("isBuy", 0))
        price = int(chapter_info.get("price", 0) or 0)
        vip_status = int(chapter_info.get("vipStatus", 0) or 0)
        is_paid_chapter = bool(price > 0 or vip_status)
        preview_only = is_paid_chapter and (not is_buy) and bool(content)
        return {
            "sourceId": self.id,
            "title": self._text(chapter_info.get("chapterName", "")),
            "content": content,
            "chapterUrl": chapter_url,
            "format": "text",
            "authRequired": is_paid_chapter and not is_buy,
            "isPaid": is_paid_chapter,
            "extra": {
                "chapterId": chapter_info.get("chapterId", ""),
                "bookId": (page_data.get("bookInfo") or {}).get("bookId", ""),
                "isBuy": is_buy,
                "price": price,
                "vipStatus": vip_status,
                "freeStatus": chapter_info.get("freeStatus", 0),
                "limitFree": chapter_info.get("limitFree", 0),
                "wordsCount": chapter_info.get("wordsCount", 0),
                "actualWords": chapter_info.get("actualWords", 0),
                "previewOnly": preview_only,
            },
        }

    async def search(self, ctx, keyword: str, page: int):
        url = f"{self.mobile_base_url}/soushu/{quote(keyword)}.html"
        if page > 1:
            url += f"?pageNum={page}"
        html = await self._fetch_text_keepalive(ctx, url, label="search")
        page_data = self._page_data(html)
        if not page_data:
            ctx.trace("qidian.page_context_missing", url=url, message="label=search")
        ctx.trace("qidian.search.mobile", url=url, message=f"records={len(((page_data.get('bookInfo') or {}).get('records')) or [])}")
        return self._search_items_from_page_data(ctx, page_data)

    async def detail(self, ctx, book_url: str):
        html = await self._fetch_text_keepalive(ctx, book_url, label="detail")
        page_data = self._page_data(html)
        if not page_data:
            ctx.trace("qidian.page_context_missing", url=book_url, message="label=detail")
        ctx.trace("qidian.detail.mobile", url=book_url, message=f"bookId={(page_data.get('bookInfo') or {}).get('bookId', '')}")
        return self._book_detail_from_page_data(ctx, book_url, page_data)

    async def toc(self, ctx, toc_url: str):
        html = await self._fetch_text_keepalive(ctx, toc_url, label="toc")
        page_data = self._page_data(html)
        if not page_data:
            ctx.trace("qidian.page_context_missing", url=toc_url, message="label=toc")
        chapters = self._toc_from_page_data(page_data)
        ctx.trace("qidian.toc.mobile", url=toc_url, message=f"chapters={len(chapters)}")
        return chapters

    async def chapter(self, ctx, chapter_url: str):
        html = await self._fetch_text_keepalive(ctx, chapter_url, label="chapter")
        page_data = self._page_data(html)
        if not page_data:
            ctx.trace("qidian.page_context_missing", url=chapter_url, message="label=chapter")
        result = self._chapter_from_page_data(ctx, chapter_url, page_data)
        ctx.trace("qidian.chapter.mobile", url=chapter_url, message=f"contentLength={len(result.get('content', ''))}")
        return result

    async def explore_groups(self, ctx):
        groups = [
            {"sourceId": self.id, "groupId": "rank_yuepiao", "title": "男生月票榜", "url": f"{self.mobile_base_url}/rank/yuepiao/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_hotsales", "title": "男生畅销榜", "url": f"{self.mobile_base_url}/rank/hotsales/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_readindex", "title": "男生阅读榜", "url": f"{self.mobile_base_url}/rank/readindex/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_recom", "title": "男生推荐榜", "url": f"{self.mobile_base_url}/rank/recom/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_update", "title": "男生更新榜", "url": f"{self.mobile_base_url}/rank/update/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_sign", "title": "男生签约榜", "url": f"{self.mobile_base_url}/rank/sign/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "rank_newbook", "title": "男生新书榜", "url": f"{self.mobile_base_url}/rank/newbook/", "kind": "rank", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "cat_xuanhuan", "title": "分类·玄幻", "url": f"{self.mobile_base_url}/category/catid21/", "kind": "category", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "cat_xianxia", "title": "分类·仙侠", "url": f"{self.mobile_base_url}/category/catid22/", "kind": "category", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "cat_dushi", "title": "分类·都市", "url": f"{self.mobile_base_url}/category/catid4/", "kind": "category", "pageable": True, "extra": {}},
            {"sourceId": self.id, "groupId": "cat_kehuan", "title": "分类·科幻", "url": f"{self.mobile_base_url}/category/catid9/", "kind": "category", "pageable": True, "extra": {}},
        ]
        ctx.trace("qidian.explore_groups.mobile", message=f"groups={len(groups)}")
        return groups

    @staticmethod
    def _class_starts_with(node, prefix: str) -> bool:
        """True if any CSS class token on ``node`` starts with ``prefix``.

        Vite appends a content-hash suffix to every class (e.g.
        ``_searchBookDesc_1lmme_521``), so exact class selectors break on
        every front-end release.  The leading semantic prefix, however, is
        stable — matching by prefix survives rebuilds.
        """
        cls = node.get("class") or ""
        for token in cls.split():
            if token.startswith(prefix):
                return True
        return False

    def _explore_items_from_html(self, ctx, base_url: str, html: str) -> list[dict]:
        soup = ctx._to_lxml(html)
        items = []
        seen = set()
        for a in soup.cssselect("a[data-bid]"):
            href = a.get("href", "")
            bid = a.get("data-bid", "")
            # Skip non-book sentinels (download banners carry an empty data-bid).
            if not bid or not bid.isdigit() or bid in seen:
                continue
            seen.add(bid)

            h2_nodes = a.cssselect("h2")
            title = ctx.clean_text(" ".join(h2_nodes[0].itertext())) if h2_nodes else ""

            cover = ""
            img_nodes = a.cssselect("img")
            if img_nodes:
                cover = img_nodes[0].get("data-src") or img_nodes[0].get("src") or ""

            desc = ""
            author = ""
            kind = ""
            word_count = ""

            # Primary path: match <p> by stable semantic class prefix.
            for p in a.iter("p"):
                if not desc and self._class_starts_with(p, "_searchBookDesc"):
                    desc = ctx.clean_text(" ".join(p.itertext()))
                elif not author and self._class_starts_with(p, "_searchBookAuthor"):
                    author = ctx.clean_text(" ".join(p.itertext()))
                elif self._class_starts_with(p, "_subTitle"):
                    # Older layout: "作者·分类·字数" packed in one <p>.
                    parts = [ctx.clean_text(x) for x in p.text_content().split("·") if ctx.clean_text(x)]
                    if not author and parts:
                        author = parts[0]
                    if len(parts) >= 2 and not kind:
                        kind = parts[1]
                    if len(parts) >= 3 and not word_count:
                        word_count = parts[2]

            # Tags container: categories + word count as the last <p>.
            tag_nodes = [
                div for div in a.iter("div")
                if self._class_starts_with(div, "_tags")
            ]
            if tag_nodes:
                tags = [
                    ctx.clean_text(p.text_content())
                    for p in tag_nodes[0].iter("p")
                    if ctx.clean_text(p.text_content())
                ]
                if len(tags) >= 2:
                    # The entry containing "万字" (or ending in "字") is word count.
                    wc_idx = next(
                        (i for i, t in enumerate(tags) if "万字" in t or t.endswith("字")),
                        len(tags) - 1,
                    )
                    word_count = tags[wc_idx]
                    kind = ",".join(t for i, t in enumerate(tags) if i != wc_idx)
                elif tags:
                    kind = ",".join(tags)

            items.append({
                "sourceId": self.id,
                "sourceName": self.name,
                "name": title,
                "author": author,
                "bookUrl": self._book_url(bid),
                "coverUrl": self._cover(cover),
                "intro": desc,
                "kind": kind,
                "lastChapter": "",
                "wordCount": word_count,
                "extra": {"bookId": bid, "rawHref": urljoin(base_url, href)},
            })
        return items

    async def explore(self, ctx, group_id: str | None = None, page: int = 1):
        groups = await self.explore_groups(ctx)
        target = next((group for group in groups if group["groupId"] == (group_id or "")), groups[0] if groups else None)
        if not target:
            return []
        url = target["url"]
        if page > 1:
            url = f"{url.rstrip('/')}/page{page}/"
        html = await self._fetch_text_keepalive(ctx, url, label=f"explore[{target['groupId']}]")
        items = self._explore_items_from_html(ctx, url, html)
        ctx.trace("qidian.explore.mobile", url=url, message=f"group={target['groupId']} items={len(items)}")
        return items
