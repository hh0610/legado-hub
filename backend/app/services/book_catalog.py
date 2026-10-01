"""Enhanced book catalog with reader support, fallback, and tracing."""

from __future__ import annotations

import sqlite3
from typing import Any
from app.config import DB_PATH, HOST, PORT
from app.core.app_config import AppConfig
from app.core.proxy import ProxyConfig
from app.source_plugins.id_codec import decode_chapter_id
from app.services.cache import Cache
from app.source_plugins.scheduler import PluginScheduler, get_plugin_scheduler


class _NoOpHealthRepo:
    """Health persistence is being removed in Phase 1."""

    def record_attempt(self, **kwargs: Any) -> None:
        pass

    def record_success(self, source_id: str, latency_ms: int) -> None:
        pass


class BookCatalog:
    def __init__(self, repo: Any | None = None, cache: Cache | None = None):
        self.repo = repo or _NoOpHealthRepo()
        self.cache = cache or Cache()
        self.scheduler = get_plugin_scheduler()

    def _get_proxy_config(self) -> ProxyConfig:
        cfg = AppConfig.get().proxy
        return ProxyConfig(
            enabled=cfg.enabled,
            url=cfg.url,
            allow_auto_retry=cfg.allow_auto_retry,
        )

    def _get_search_config(self) -> dict:
        cfg = AppConfig.get()
        return {
            "proxy": {
                "enabled": cfg.proxy.enabled,
                "url": cfg.proxy.url,
                "allowAutoRetry": cfg.proxy.allow_auto_retry,
            },
            "max_concurrency": cfg.search.global_source_concurrency,
            "source_timeout_seconds": cfg.search.source_timeout_seconds,
            "overall_search_timeout_seconds": cfg.search.overall_timeout_seconds,
            "source_batch_size": 20,
            "browser_source_timeout_seconds": cfg.search.browser_source_timeout_seconds,
            "browser_search_timeout_seconds": cfg.search.browser_search_timeout_seconds,
            "default_user_agent": cfg.search.default_user_agent,
        }

    async def book_detail(self, book_id: str) -> dict:
        from app.services.catalog import Catalog
        catalog = Catalog(repo=self.repo, cache=self.cache)
        return await catalog.book_detail(book_id)

    async def toc(self, book_id: str) -> dict:
        from app.services.catalog import Catalog
        catalog = Catalog(repo=self.repo, cache=self.cache)
        detail = await catalog.book_detail(book_id)
        detail_data = detail.get("data") if isinstance(detail, dict) else {}
        if isinstance(detail_data, dict) and (detail_data.get("rawTocUrl") or detail_data.get("tocUrl")):
            try:
                from app.source_plugins.id_codec import decode_book_id, encode_book_id

                source_id, _ = decode_book_id(book_id)
                raw_toc_url = str(detail_data.get("rawTocUrl") or detail_data.get("tocUrl"))
                toc_book_id = encode_book_id(source_id, raw_toc_url)
                return await catalog.toc(toc_book_id)
            except Exception:
                pass
        return await catalog.toc(book_id)

    async def chapter(self, chapter_id: str) -> dict:
        from app.services.catalog import Catalog
        catalog = Catalog(repo=self.repo, cache=self.cache)
        return await catalog.chapter(chapter_id)

    async def chapter_reviews(self, chapter_id: str) -> dict:
        from app.services.catalog import Catalog

        catalog = Catalog(repo=self.repo, cache=self.cache)
        return await catalog.chapter_reviews(chapter_id)

    async def chapter_with_fallback(
        self,
        chapter_id: str,
        fallback_source_ids: list[str] | None = None,
    ) -> dict:
        """Get chapter with fallback to alternative sources."""
        primary = await self.chapter(chapter_id)
        primary_media = primary.get("format") in {"audio", "video"} and bool(primary.get("mediaUrl"))
        if primary.get("content") or primary_media or not fallback_source_ids:
            return {**primary, "fallbackUsed": False, "fallbackTrace": []}

        # Decode primary source and chapter URL
        try:
            primary_source_id, chapter_url = decode_chapter_id(chapter_id)
        except Exception:
            return {**primary, "fallbackUsed": False, "fallbackTrace": [{"error": "invalid chapter_id"}]}

        fallback_trace = []

        for sid in fallback_source_ids:
            if sid == primary_source_id:
                continue
            plugin = self.scheduler._plugins.get(sid)
            if not plugin or "chapter" not in plugin.capabilities:
                fallback_trace.append({"sourceId": sid, "status": "skipped", "error": "plugin not found or no chapter capability"})
                continue

            try:
                content = await self.scheduler.chapter(sid, chapter_url)
                content_text = ""
                content_media_url = ""
                content_is_dict = isinstance(content, dict)
                if content_is_dict:
                    content_text = content.get("content", "")
                    content_format = str(content.get("format", "") or "")
                    content_media_url = str(content.get("mediaUrl", "") or "")
                    if content_format in {"audio", "video"} and not content_media_url:
                        content_text = ""
                elif hasattr(content, "content"):
                    content_text = content.content
                if content_text or content_media_url:
                    fallback_trace.append({"sourceId": sid, "status": "success"})
                    fallback_format = str(content.get("format", "text") or "text") if content_is_dict else "text"
                    is_media = fallback_format in {"audio", "video"}
                    return {
                        "implemented": True,
                        "chapterId": chapter_id,
                        "title": content.get("title", "") if content_is_dict else getattr(content, "title", ""),
                        "content": content_text,
                        "format": fallback_format,
                        "mediaUrl": content_media_url if is_media else "",
                        "mediaType": str(content.get("mediaType", "") or "") if is_media and content_is_dict else "",
                        "durationSeconds": float(content.get("durationSeconds", 0) or 0) if is_media and content_is_dict else 0.0,
                        "fallbackUsed": True,
                        "fallbackSourceId": sid,
                        "fallbackTrace": fallback_trace,
                        "debug": {},
                    }
                else:
                    fallback_trace.append({"sourceId": sid, "status": "failed", "error": "empty content"})
            except Exception as e:
                fallback_trace.append({"sourceId": sid, "status": "exception", "error": str(e)})

        return {
            **primary,
            "fallbackUsed": False,
            "fallbackTrace": fallback_trace,
        }

    def get_book_sources(self, book_id: str) -> list[dict]:
        """Get candidate sources for a book by name/author."""
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT name, author FROM book_records WHERE book_id = ?", (book_id,)
            ).fetchone()
        if not row:
            return []
        # Return enabled plugins as candidate sources
        plugins = self.scheduler._enabled_plugins()
        return [{"sourceId": p.metadata.id, "sourceName": p.metadata.name} for p in plugins]

    def get_chapter_navigation(self, book_id: str, chapter_id: str) -> dict:
        """Get previous and next chapter IDs."""
        toc = self.cache.get_toc(book_id)
        if not toc:
            return {"prev": None, "next": None}
        chapters = toc.get("chapters", [])
        for i, ch in enumerate(chapters):
            if ch.get("chapterId") == chapter_id:
                prev_ch = chapters[i - 1] if i > 0 else None
                next_ch = chapters[i + 1] if i < len(chapters) - 1 else None
                return {
                    "prev": prev_ch.get("chapterId") if prev_ch else None,
                    "next": next_ch.get("chapterId") if next_ch else None,
                    "prevTitle": prev_ch.get("title") if prev_ch else None,
                    "nextTitle": next_ch.get("title") if next_ch else None,
                }
        return {"prev": None, "next": None}
