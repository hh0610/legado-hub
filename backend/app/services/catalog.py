"""Concurrent search across enabled plugins with batched execution, failure recording, and proxy fallback."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from app.config import HOST, PORT
from app.core.app_config import AppConfig
from app.core.proxy import ProxyConfig
from app.source_plugins.id_codec import decode_book_id, decode_chapter_id, encode_book_id, encode_chapter_id
from app.services.cache import Cache
from app.services.bookshelf import record_bookshelf_items
from app.services.search_coordinator import SearchCoordinator
from app.services.aggregate_virtual_source import (
    VIRTUAL_SOURCE_ID,
    VIRTUAL_SOURCE_NAME,
    make_library_aggregate_book_url,
    make_aggregate_chapter_url,
    primary_book_id_from_payload,
    unpack_aggregate_chapter_url,
    unpack_aggregate_book_url,
)
from app.services.library_books import library_books_service
from app.services.novel_file_cache import looks_like_garbled_text
from app.source_plugins.scheduler import PluginScheduler, get_plugin_scheduler


def _aggregate_official_debug(payload: dict[str, Any], primary_book_id: str) -> dict[str, Any]:
    try:
        from app.source_plugins.loader import PluginLoader
        plugins = PluginLoader().load_all()
    except Exception:
        plugins = {}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    official_ids = sorted({
        s.get("sourceId", "")
        for s in sources
        if isinstance(s, dict) and s.get("sourceId") and plugins.get(s.get("sourceId")) and plugins[s.get("sourceId")].metadata.is_official_source()
    })
    primary_source_id = primary_book_id.split(":", 1)[0] if ":" in primary_book_id else ""
    primary_is_official = bool(
        primary_source_id and plugins.get(primary_source_id) and plugins[primary_source_id].metadata.is_official_source()
    )
    return {
        "hasOfficialSource": bool(official_ids),
        "officialSourceIds": official_ids,
        "primarySourceIsOfficial": primary_is_official,
    }


class _NoOpHealthRepo:
    """Health persistence is being removed in Phase 1."""

    def record_attempt(self, **kwargs: Any) -> None:
        pass

    def record_success(self, source_id: str, latency_ms: int) -> None:
        pass


class Catalog:
    def __init__(
        self,
        repo: Any | None = None,
        cache: Cache | None = None,
        base_api: str | None = None,
    ):
        self.repo = repo or _NoOpHealthRepo()
        self.cache = cache or Cache()
        self.scheduler = get_plugin_scheduler(config=self._get_search_config())
        self._search_coordinator = SearchCoordinator()
        self.base_api = base_api or f"http://{HOST}:{PORT}"

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

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    async def _call_plugin(
        self,
        plugin: Any,
        operation: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
    ) -> Any:
        call = getattr(self.scheduler, "_call_plugin", None)
        if callable(call):
            return await call(plugin, operation, timeout=timeout)
        return await asyncio.wait_for(operation(), timeout=timeout)

    def _book_api_url(self, book_id: str) -> str:
        return f"{self.base_api}/api/legado/book/{book_id}"

    def _toc_api_url(self, book_id: str) -> str:
        return f"{self.base_api}/api/legado/book/{book_id}/toc"

    def _chapter_api_url(self, chapter_id: str) -> str:
        return f"{self.base_api}/api/legado/chapter/{chapter_id}"

    def _rewrite_book_response_urls(self, response: dict, book_id: str) -> dict:
        copied = dict(response)
        data = copied.get("data")
        if isinstance(data, dict):
            data = dict(data)
            data["bookUrl"] = self._book_api_url(book_id)
            data["tocUrl"] = self._toc_api_url(book_id)
            copied["data"] = data
        return copied

    def _rewrite_toc_response_urls(self, response: dict) -> dict:
        copied = dict(response)
        chapters = []
        for chapter in copied.get("chapters", []) or []:
            if not isinstance(chapter, dict):
                continue
            out = dict(chapter)
            raw_chapter_url = out.get("rawChapterUrl") or out.get("chapterUrl", "")
            chapter_url = out.get("chapterUrl", "")
            if raw_chapter_url:
                out["rawChapterUrl"] = raw_chapter_url
            if chapter_url and "/api/legado/chapter/" in chapter_url:
                chapter_id = out.get("chapterId") or chapter_url.rsplit("/", 1)[-1]
                out["chapterId"] = out.get("chapterId") or chapter_id
                out["chapterUrl"] = self._chapter_api_url(chapter_id)
            chapters.append(out)
        copied["chapters"] = chapters
        return copied

    def _record_attempt(
        self,
        source_id: str,
        stage: str,
        url: str,
        proxy_used: bool,
        success: bool,
        latency_ms: int,
        error: str = "",
        result: str = "",
    ) -> None:
        self.repo.record_attempt(
            source_id=source_id,
            stage=stage,
            url=url,
            direct_status="success" if success and not proxy_used else ("failed" if not proxy_used else "-"),
            proxy_status="success" if success and proxy_used else ("failed" if proxy_used else "-"),
            proxy_used=proxy_used,
            latency_ms=latency_ms,
            error=error,
            result=result,
        )

    async def search(
        self,
        keyword: str,
        page: int = 1,
        source_ids: list[str] | None = None,
    ) -> dict:
        """Run a catalog search.

        ``source_ids`` determines the source scope used for cache fallback.
        When omitted, the catalog operates in the default search scope
        (``__default__``), which matches the default scope used by
        ``SearchCoordinator``.
        """
        result = await self.scheduler.search(keyword, page)
        result = self._merge_search_cache_fallback(result, keyword, page, source_ids)

        # Convert plugin result dicts to SearchResultItem shape for cache consistency
        # and ensure bookId / sourceName are present
        items = result.get("items", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            source_id = item.get("sourceId", "")
            plugin = self.scheduler._plugins.get(source_id)
            if "bookId" not in item and item.get("bookUrl"):
                # extract original url from bookUrl if it's a local api url
                # otherwise use bookUrl directly
                book_url = item.get("bookUrl", "")
                item["bookId"] = encode_book_id(source_id, book_url)
            if "sourceName" not in item:
                item["sourceName"] = plugin.metadata.name if plugin else item.get("sourceId", "")

        # Ensure debug fields exist for old clients
        debug = result.get("debug", {})
        debug.setdefault("disabledCount", 0)

        response = {
            "implemented": True,
            "keyword": keyword,
            "page": page,
            "items": items,
            "debug": debug,
        }

        record_bookshelf_items(items)
        return response

    async def explore(self, source_id: str = "", group_id: str = "", page: int = 1) -> dict:
        """Reading-compatible discover/explore endpoint.

        If source_id is omitted, collect the first explore group from every
        enabled plugin that supports explore. If group_id is omitted for a
        source, use that source's first declared group.
        """
        if source_id:
            return await self._explore_one(source_id, group_id, page)

        groups_result = await self.scheduler.explore_groups()
        items: list[dict] = []
        errors: list[dict] = []
        seen_sources: set[str] = set()
        for group in groups_result.get("groups", []):
            sid = group.get("sourceId", "")
            if not sid or sid in seen_sources:
                continue
            seen_sources.add(sid)
            result = await self.scheduler.explore(sid, group.get("groupId", ""), page)
            items.extend(self._normalize_explore_items(result.get("items", [])))
            errors.extend(result.get("debug", {}).get("errors", []))

        record_bookshelf_items(items)
        return {
            "implemented": True,
            "sourceId": "",
            "groupId": group_id,
            "page": page,
            "items": items,
            "debug": {
                "sourceCount": len(seen_sources),
                "itemCount": len(items),
                "errors": errors,
            },
        }

    async def _explore_one(self, source_id: str, group_id: str = "", page: int = 1) -> dict:
        actual_group_id = group_id
        if not actual_group_id:
            groups = await self.scheduler.explore_groups(source_id)
            first = (groups.get("groups") or [{}])[0]
            actual_group_id = first.get("groupId", "")
        result = await self.scheduler.explore(source_id, actual_group_id, page)
        result["items"] = self._normalize_explore_items(result.get("items", []))
        record_bookshelf_items(result["items"])
        return result

    def _normalize_explore_items(self, items: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out = dict(item)
            source_id = out.get("sourceId", "")
            raw_book_url = out.get("bookUrl", "")
            if raw_book_url and "/api/legado/book/" not in raw_book_url:
                book_id = encode_book_id(source_id, raw_book_url)
                out["bookId"] = book_id
                out["rawBookUrl"] = raw_book_url
                out["bookUrl"] = f"{self.base_api}/api/legado/book/{book_id}"
            elif raw_book_url and "/api/legado/book/" in raw_book_url:
                book_id = raw_book_url.rsplit("/", 1)[-1]
                out["bookId"] = out.get("bookId") or book_id
                out["bookUrl"] = f"{self.base_api}/api/legado/book/{book_id}"
            normalized.append(out)
        return normalized

    async def stream_search(self, keyword: str, page: int = 1, max_sources_override: int | None = None):
        """Yield console search progress events as dictionaries."""
        config = self._get_search_config()
        max_concurrency = self._positive_int(config.get("max_concurrency"), 3)
        source_batch_size = config.get("source_batch_size", 20)
        overall_timeout = config.get("overall_search_timeout_seconds", 60.0)
        max_sources = max_sources_override if max_sources_override is not None else config.get("max_sources_per_search", 200)
        proxy_config = self._get_proxy_config()
        plugins = self.scheduler._enabled_plugins()
        if max_sources is not None:
            plugins = plugins[:max_sources]

        source_batch_size = self._positive_int(source_batch_size, 20)
        batches = [plugins[i : i + source_batch_size] for i in range(0, len(plugins), source_batch_size)]
        start_time = time.perf_counter()
        all_items: list[dict] = []
        errors: list[dict] = []
        success_count = 0
        completed_count = 0
        timeout_count = 0

        yield {
            "type": "summary",
            "keyword": keyword,
            "page": page,
            "sourceCount": len(plugins),
            "batchSize": source_batch_size,
            "batchCount": len(batches),
            "maxConcurrency": max_concurrency,
            "overallTimeoutSeconds": overall_timeout,
        }

        if not plugins:
            yield {
                "type": "done",
                "items": [],
                "debug": {
                    "sourceCount": 0,
                    "attemptedCount": 0,
                    "successCount": 0,
                    "errorCount": 0,
                    "timeoutCount": 0,
                    "elapsedMs": 0,
                    "partialSuccess": False,
                },
            }
            return

        async def _search_one(plugin) -> dict:
            sid = plugin.metadata.id
            plugin_proxy_mode = (plugin.metadata.proxy or {}).get("mode", "auto")
            proxy_used = proxy_config.enabled and plugin_proxy_mode != "never"
            if "search" not in plugin.capabilities:
                self._record_attempt(sid, "search", "", proxy_used, False, 0, "no search capability")
                return {"source": {"sourceId": sid, "bookSourceName": plugin.metadata.name}, "items": [], "error": None, "latencyMs": 0, "proxyUsed": proxy_used}
            ctx = self.scheduler._make_ctx(sid)
            source_timeout = self.scheduler.search_timeout_for_plugin(plugin)
            t0 = time.perf_counter()
            try:
                raw_items = await self._call_plugin(
                    plugin,
                    lambda: plugin.source.search(ctx, keyword, page),
                    timeout=source_timeout,
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                items = []
                for item in raw_items or []:
                    if isinstance(item, dict):
                        item.setdefault("sourceId", sid)
                        item.setdefault("sourceName", plugin.metadata.name)
                        if "bookId" not in item and item.get("bookUrl"):
                            item["bookId"] = encode_book_id(sid, item["bookUrl"])
                        items.append(item)
                result = json.dumps([{"name": i.get("name"), "author": i.get("author"), "bookUrl": i.get("bookUrl")} for i in items[:5]], ensure_ascii=False) if items else ""
                self._record_attempt(sid, "search", "", proxy_used, True, latency_ms, result=result)
                self.repo.record_success(sid, latency_ms)
                return {"source": {"sourceId": sid, "bookSourceName": plugin.metadata.name}, "items": items, "error": None, "latencyMs": latency_ms, "proxyUsed": proxy_used}
            except asyncio.TimeoutError:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                self._record_attempt(sid, "search", "", proxy_used, False, latency_ms, "timeout")
                err = {"sourceId": sid, "stage": "search", "url": "", "proxyUsed": proxy_used, "error": "timeout"}
                return {"source": {"sourceId": sid, "bookSourceName": plugin.metadata.name}, "items": [], "error": err, "latencyMs": latency_ms, "proxyUsed": proxy_used}
            except Exception as exc:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                self._record_attempt(sid, "search", "", proxy_used, False, latency_ms, str(exc))
                err = {"sourceId": sid, "stage": "search", "url": "", "proxyUsed": proxy_used, "error": str(exc)}
                return {"source": {"sourceId": sid, "bookSourceName": plugin.metadata.name}, "items": [], "error": err, "latencyMs": latency_ms, "proxyUsed": proxy_used}
            finally:
                await ctx._fetcher.close()

        for batch_index, batch in enumerate(batches, start=1):
            elapsed = time.perf_counter() - start_time
            if elapsed >= overall_timeout:
                errors.append({"sourceId": "", "stage": "search", "url": "", "error": "overall timeout"})
                yield {"type": "overall_timeout", "elapsedMs": int(elapsed * 1000)}
                break

            for plugin in batch:
                yield {
                    "type": "source_start",
                    "batchIndex": batch_index,
                    "sourceId": plugin.metadata.id,
                    "sourceName": plugin.metadata.name,
                    "proxyMode": "auto",
                }

            remaining_plugins = list(batch)
            pending: set[asyncio.Task] = set()

            def start_next_plugins() -> None:
                while remaining_plugins and len(pending) < max_concurrency:
                    pending.add(asyncio.create_task(_search_one(remaining_plugins.pop(0))))

            start_next_plugins()
            while pending:
                remaining_timeout = max(0.1, overall_timeout - (time.perf_counter() - start_time))
                done, pending = await asyncio.wait(pending, timeout=remaining_timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    timeout_count += len(pending)
                    errors.append({"sourceId": "", "stage": "search", "url": "", "error": "overall timeout"})
                    yield {"type": "overall_timeout", "elapsedMs": int((time.perf_counter() - start_time) * 1000)}
                    pending = set()
                    break

                for task in done:
                    result = task.result()
                    src = result["source"]
                    items = result["items"]
                    err = result["error"]
                    completed_count += 1
                    if err:
                        errors.append(err)
                        if "timeout" in str(err.get("error", "")).lower():
                            timeout_count += 1
                    if items:
                        success_count += 1
                        for item in items:
                            self.scheduler._score_search_item(item, keyword)
                        all_items.extend(items)

                    yield {
                        "type": "source_done",
                        "sourceId": src["sourceId"],
                        "sourceName": src.get("bookSourceName") or src["sourceId"],
                        "status": "error" if err else "success",
                        "resultCount": len(items),
                        "latencyMs": result["latencyMs"],
                        "proxyUsed": result["proxyUsed"],
                        "error": err,
                        "completedCount": completed_count,
                        "sourceCount": len(plugins),
                    }
                    for item in items:
                        yield {
                            "type": "result",
                            "item": item,
                            "sourceId": src["sourceId"],
                            "sourceName": src.get("bookSourceName") or src["sourceId"],
                        }
                start_next_plugins()

            yield {
                "type": "batch_done",
                "batchIndex": batch_index,
                "completedCount": completed_count,
                "sourceCount": len(plugins),
            }

        items = self._normalize_source_search_items(all_items)

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        response = {
            "implemented": True,
            "keyword": keyword,
            "page": page,
            "items": items,
            "debug": {
                "sourceCount": len(plugins),
                "batchSize": source_batch_size,
                "batchCount": len(batches),
                "attemptedCount": completed_count,
                "successCount": success_count,
                "errorCount": len(errors),
                "disabledCount": 0,
                "timeoutCount": timeout_count,
                "elapsedMs": elapsed_ms,
                "errors": errors,
                "partialSuccess": success_count > 0 and len(errors) > 0,
            },
        }
        record_bookshelf_items(response["items"])
        yield {
            "type": "done",
            "items": response["items"],
            "debug": response["debug"],
        }

    def _normalize_source_search_items(self, items: list[dict]) -> list[dict]:
        """Return one Reading search item per source result."""
        source_items = [dict(item) for item in items if isinstance(item, dict)]
        source_items.sort(
            key=lambda item: (
                -item.get("score", 0),
                item.get("name", ""),
                item.get("sourceName", "") or item.get("sourceId", ""),
            )
        )
        for item in source_items:
            source_id = item.get("sourceId", "")
            raw_book_url = item.get("rawBookUrl") or item.get("bookUrl", "")
            if raw_book_url and "/api/legado/book/" not in raw_book_url:
                book_id = item.get("bookId") or encode_book_id(source_id, raw_book_url)
                item["bookId"] = book_id
                item["rawBookUrl"] = raw_book_url
                item["bookUrl"] = self._book_api_url(book_id)
            elif raw_book_url and "/api/legado/book/" in raw_book_url:
                book_id = raw_book_url.rsplit("/", 1)[-1]
                item["bookId"] = item.get("bookId") or book_id
                item["bookUrl"] = self._book_api_url(book_id)
        return source_items

    async def book_detail(self, book_id: str, user_agent: str = "") -> dict:
        try:
            source_id, book_url = decode_book_id(book_id)
        except Exception:
            return {"implemented": True, "data": None, "debug": {"error": "invalid book_id format"}}

        if source_id == VIRTUAL_SOURCE_ID:
            return await self._aggregate_book_detail(book_id, book_url)

        cached = self.cache.get_book(book_id)
        result = await self.scheduler.detail(source_id, book_url)
        data = result.get("data")
        error_info = result.get("debug", {}).get("error")
        if data is None and cached is not None and self._should_fallback_to_cache(error_info):
            cached_response = self._rewrite_book_response_urls(cached, book_id)
            cached_debug = dict(cached_response.get("debug") or {})
            cached_debug["cacheHit"] = True
            cached_debug["cacheReason"] = self._cache_reason(error_info)
            cached_debug["liveError"] = error_info
            cached_response["debug"] = cached_debug
            return cached_response
        if data is None:
            return {"implemented": True, "data": None, "debug": result.get("debug", {})}

        raw_book_url = data.get("bookUrl", "") or book_url
        raw_toc_url = data.get("tocUrl", "") or raw_book_url
        data["rawBookUrl"] = raw_book_url
        data["rawTocUrl"] = raw_toc_url
        data["bookUrl"] = self._book_api_url(book_id)
        data["tocUrl"] = self._toc_api_url(book_id)

        self._ensure_book_record(book_id, data)

        response = {"implemented": True, "data": data, "debug": {}}
        self.cache.set_book(book_id, source_id, book_url, response)
        return response

    def _ensure_book_record(self, book_id: str, data: dict) -> None:
        import sqlite3
        from app.config import DB_PATH
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO book_records
                (book_id, name, author, last_chapter, last_seen_at, created_at)
                VALUES (?, ?, ?, ?, datetime('now'), COALESCE((SELECT created_at FROM book_records WHERE book_id=?), datetime('now')))
                """,
                (book_id, data.get("name", ""), data.get("author", ""), data.get("lastChapter", ""), book_id),
            )
            conn.commit()

    async def _aggregate_book_detail(self, book_id: str, book_url: str) -> dict:
        aggregate_task_book_id = book_id
        try:
            payload = unpack_aggregate_book_url(book_url)
            if payload.get("library") and payload.get("aggregateBookId"):
                aggregate_book_id = payload.get("aggregateBookId", "")
                payload = library_books_service.load_payload(aggregate_book_id)
                book_url = make_library_aggregate_book_url(aggregate_book_id)
                aggregate_task_book_id = aggregate_book_id
                primary_book_id = primary_book_id_from_payload(payload)
            else:
                primary_book_id = primary_book_id_from_payload(payload)
        except Exception as exc:
            return {"implemented": True, "data": None, "debug": {"error": str(exc), "aggregate": True}}
        if not primary_book_id:
            return {"implemented": True, "data": None, "debug": {"error": "aggregate source has no candidates", "aggregate": True}}

        from app.services.aggregate_processor import AggregateProcessor

        enqueue_result = AggregateProcessor().enqueue_book(aggregate_task_book_id, payload)
        detail = await self.book_detail(primary_book_id)
        data = dict(detail.get("data") or {})
        official_debug = _aggregate_official_debug(payload, primary_book_id)
        if not data:
            return {
                **detail,
                "debug": {
                    **detail.get("debug", {}),
                    "aggregate": True,
                    "primaryBookId": primary_book_id,
                    **official_debug,
                },
            }

        data["sourceId"] = VIRTUAL_SOURCE_ID
        data["sourceName"] = VIRTUAL_SOURCE_NAME
        data["name"] = payload.get("name") or data.get("name", "")
        data["author"] = payload.get("author") or data.get("author", "")
        data["bookUrl"] = self._book_api_url(book_id)
        data["tocUrl"] = self._toc_api_url(book_id)
        data["intro"] = (
            f"【{VIRTUAL_SOURCE_NAME}】{data.get('intro', '')}".strip()
            or f"{VIRTUAL_SOURCE_NAME} 聚合详情"
        )
        return {
            "implemented": True,
            "data": data,
            "debug": {
                **detail.get("debug", {}),
                "aggregate": True,
                "primaryBookId": primary_book_id,
                "sourceCount": len(payload.get("sources", []) or []),
                "workflow": "ai_aggregate_purify",
                "aggregateTask": enqueue_result,
                **official_debug,
            },
        }

    async def toc(self, book_id: str, user_agent: str = "") -> dict:
        try:
            source_id, book_url = decode_book_id(book_id)
        except Exception:
            return {"implemented": True, "bookId": book_id, "chapters": [], "debug": {"error": "invalid book_id format"}}

        if source_id == VIRTUAL_SOURCE_ID:
            return await self._aggregate_toc(book_id, book_url)

        cached = self.cache.get_toc(book_id)
        toc_url = book_url
        cached_book = self.cache.get_book(book_id)
        cached_book_data = cached_book.get("data") if isinstance(cached_book, dict) else {}
        if isinstance(cached_book_data, dict):
            toc_url = (
                cached_book_data.get("rawTocUrl", "")
                or cached_book_data.get("tocUrl", "")
                or toc_url
            )
        result = await self.scheduler.toc(source_id, toc_url)
        chapters = result.get("chapters", [])
        debug = result.get("debug", {})
        error_info = debug.get("error")

        if error_info and cached is not None and self._should_fallback_to_cache(error_info):
            cached_response = self._rewrite_toc_response_urls(cached)
            cached_debug = dict(cached_response.get("debug") or {})
            cached_debug["cacheHit"] = True
            cached_debug["cacheReason"] = self._cache_reason(error_info)
            cached_debug["liveError"] = error_info
            cached_response["debug"] = cached_debug
            return cached_response

        response = {
            "implemented": True,
            "bookId": book_id,
            "chapters": chapters,
            "debug": debug,
        }
        response = self._rewrite_toc_response_urls(response)
        if not debug.get("error"):
            self.cache.set_toc(book_id, response)
        return response

    async def _aggregate_toc(self, book_id: str, book_url: str) -> dict:
        aggregate_task_book_id = book_id
        try:
            payload = unpack_aggregate_book_url(book_url)
            if payload.get("library") and payload.get("aggregateBookId"):
                aggregate_task_book_id = payload.get("aggregateBookId", "")
                payload = library_books_service.load_payload(aggregate_task_book_id)
                primary_book_id = primary_book_id_from_payload(payload)
            else:
                primary_book_id = primary_book_id_from_payload(payload)
        except Exception as exc:
            return {"implemented": True, "bookId": book_id, "chapters": [], "debug": {"error": str(exc), "aggregate": True}}
        if not primary_book_id:
            return {"implemented": True, "bookId": book_id, "chapters": [], "debug": {"error": "aggregate source has no candidates", "aggregate": True}}

        toc = await self.toc(primary_book_id)
        chapters = [dict(chapter) for chapter in toc.get("chapters", []) if isinstance(chapter, dict)]
        from app.services.aggregate_processor import AggregateProcessor

        register_result = AggregateProcessor().register_toc(aggregate_task_book_id, payload, chapters)
        source_id = primary_book_id.split(":", 1)[0] if ":" in primary_book_id else ""
        for index, chapter in enumerate(chapters, start=1):
            raw_url = chapter.get("rawChapterUrl") or chapter.get("chapterUrl", "")
            source_chapter_id = chapter.get("chapterId") or (
                encode_chapter_id(source_id, raw_url) if source_id and raw_url else f"{aggregate_task_book_id}:{index}"
            )
            aggregate_chapter_url = make_aggregate_chapter_url(
                aggregate_book_id=aggregate_task_book_id,
                source_chapter_id=source_chapter_id,
                title=chapter.get("title", ""),
                index=index,
            )
            aggregate_chapter_id = encode_chapter_id(VIRTUAL_SOURCE_ID, aggregate_chapter_url)
            chapter["sourceId"] = VIRTUAL_SOURCE_ID
            chapter["sourceChapterId"] = source_chapter_id
            chapter["rawChapterUrl"] = raw_url
            chapter["chapterId"] = aggregate_chapter_id
            chapter["chapterUrl"] = self._chapter_api_url(aggregate_chapter_id)
        official_debug = _aggregate_official_debug(payload, primary_book_id)
        return {
            "implemented": True,
            "bookId": book_id,
            "chapters": chapters,
            "debug": {
                **toc.get("debug", {}),
                "aggregate": True,
                "primaryBookId": primary_book_id,
                "sourceCount": len(payload.get("sources", []) or []),
                "workflow": "ai_aggregate_purify",
                "aggregateTask": register_result,
                **official_debug,
            },
        }

    async def chapter(self, chapter_id: str, user_agent: str = "") -> dict:
        try:
            source_id, chapter_url = decode_chapter_id(chapter_id)
        except Exception:
            return {"implemented": True, "chapterId": chapter_id, "title": "", "content": "", "format": "text", "mediaUrl": "", "debug": {"error": "invalid chapter_id format"}}

        if source_id == VIRTUAL_SOURCE_ID:
            from app.services.aggregate_processor import AggregateProcessor

            return AggregateProcessor().aggregate_chapter_response(chapter_url, chapter_id=chapter_id)

        cached = self.cache.get_chapter(chapter_id)
        result = await self.scheduler.chapter(source_id, chapter_url)
        title = result.get("title", "")
        content = result.get("content", "")
        debug = result.get("debug", {})
        content_format = str(result.get("format", "") or "").strip().lower() or "text"
        media_url = str(result.get("mediaUrl", "") or "")
        is_media = content_format in {"audio", "video"} and bool(media_url)
        if not is_media and looks_like_garbled_text(content):
            content = ""
            debug = {**debug, "error": "garbled chapter content"}

        error_info = debug.get("error")
        if error_info and cached is not None and self._should_fallback_to_cache(error_info):
            cached_response = dict(cached)
            cached_debug = dict(cached_response.get("debug") or {})
            cached_debug["cacheHit"] = True
            cached_debug["cacheReason"] = self._cache_reason(error_info)
            cached_debug["liveError"] = error_info
            cached_response["debug"] = cached_debug
            return cached_response

        response = {
            "implemented": True,
            "chapterId": chapter_id,
            "sourceId": source_id,
            "title": title,
            "content": content,
            "format": "text" if not is_media else content_format,
            "mediaUrl": media_url if is_media else "",
            "mediaType": str(result.get("mediaType", "") or "") if is_media else "",
            "durationSeconds": float(result.get("durationSeconds", 0) or 0) if is_media else 0.0,
            "rawChapterUrl": chapter_url,
            "chapterUrl": chapter_url,
            "authRequired": bool(result.get("authRequired", False)),
            "isPaid": bool(result.get("isPaid", False)),
            "extra": result.get("extra", {}) if isinstance(result.get("extra", {}), dict) else {},
            "debug": debug,
        }
        # Only cache if content is non-empty and no error
        if not debug.get("error") and (
            (content and len(content.strip()) > 0) or (is_media and media_url)
        ):
            self.cache.set_chapter(chapter_id, source_id, chapter_url, response)
        return response

    async def chapter_reviews(self, chapter_id: str, user_agent: str = "") -> dict:
        from app.services.chapter_review_catalog import chapter_reviews

        return await chapter_reviews(self.scheduler, chapter_id)

    async def book_reviews(self, book_id: str, user_agent: str = "") -> dict:
        """Book-level reviews (书评) via the source plugin's book_reviews capability.

        Aggregate books dispatch to their primary source; third-party books go
        straight to the owning plugin.
        """
        try:
            source_id, book_url = decode_book_id(book_id)
        except Exception:
            return {"implemented": True, "summary": {}, "items": [], "debug": {"error": "invalid book_id format"}}

        if source_id == VIRTUAL_SOURCE_ID:
            try:
                payload = unpack_aggregate_book_url(book_url)
                if payload.get("library") and payload.get("aggregateBookId"):
                    payload = library_books_service.load_payload(payload.get("aggregateBookId", ""))
                primary_source_id = str(payload.get("primarySourceId", "") or "")
                primary_book_url = str(payload.get("primaryBookUrl", "") or "")
                if not primary_source_id or not primary_book_url:
                    return {"implemented": True, "summary": {}, "items": [], "debug": {"error": "aggregate book has no primary source"}}
                result = await self.scheduler.book_reviews(primary_source_id, primary_book_url)
                result["debug"] = {**result.get("debug", {}), "aggregate": True, "primarySourceId": primary_source_id}
                return result
            except Exception as exc:
                return {"implemented": True, "summary": {}, "items": [], "debug": {"error": str(exc)}}

        return await self.scheduler.book_reviews(source_id, book_url)

    async def page_hot_reviews(
        self,
        chapter_id: str,
        paragraph_ids: list[int],
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        from app.services.chapter_review_catalog import page_hot_reviews

        return await page_hot_reviews(
            self.scheduler,
            chapter_id,
            paragraph_ids,
            page=page,
            page_size=page_size,
        )

    async def chapter_say(self, chapter_id: str, *, page: int = 1, page_size: int = 20) -> dict:
        from app.services.chapter_review_catalog import chapter_say

        return await chapter_say(self.scheduler, chapter_id, page=page, page_size=page_size)

    async def paragraph_reviews(
        self,
        chapter_id: str,
        paragraph_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        from app.services.chapter_review_catalog import paragraph_reviews

        return await paragraph_reviews(
            self.scheduler,
            chapter_id,
            paragraph_id,
            page=page,
            page_size=page_size,
        )

    async def review_replies(
        self,
        chapter_id: str,
        root_review_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
        cursor_id: int = 0,
    ) -> dict:
        from app.services.chapter_review_catalog import review_replies

        return await review_replies(
            self.scheduler,
            chapter_id,
            root_review_id,
            page=page,
            page_size=page_size,
            cursor_id=cursor_id,
        )

    def _should_fallback_to_cache(self, error: Any) -> bool:
        if not error:
            return False
        if isinstance(error, dict):
            code = str(error.get("code", "") or "")
            message = str(error.get("message", "") or error.get("error", "") or "")
        else:
            code = ""
            message = str(error)
        message_lower = message.lower()
        if code in {"PLUGIN_TIMEOUT", "BROWSER_REQUIRED", "CLOUDFLARE_REQUIRED", "FETCH_NETWORK_ERROR", "FETCH_HTTP_5XX"}:
            return True
        if "timeout" in message_lower or "cloudflare" in message_lower or "browser" in message_lower:
            return True
        if "captcha" in message_lower or "验证码" in message or "风控" in message:
            return True
        if "network" in message_lower:
            return True
        return False

    def _cache_reason(self, error: Any) -> str:
        if isinstance(error, dict):
            code = str(error.get("code", "") or "")
        else:
            code = ""
        mapping = {
            "PLUGIN_TIMEOUT": "timeout",
            "BROWSER_REQUIRED": "browser_required",
            "CLOUDFLARE_REQUIRED": "cloudflare_required",
            "FETCH_NETWORK_ERROR": "network_error",
            "FETCH_HTTP_5XX": "server_error",
        }
        return mapping.get(code, "live_failure")

    def _merge_search_cache_fallback(
        self,
        result: dict,
        keyword: str,
        page: int,
        source_ids: list[str] | None = None,
    ) -> dict:
        # Explicit scope: if the caller did not specify source_ids, the catalog
        # is operating in the default search scope just like SearchCoordinator.
        scope = self._search_coordinator._scope_key(source_ids) if source_ids else "__default__"
        cached_items_from_cache = self._search_coordinator._query_book_cache(keyword, "mixed")
        if not cached_items_from_cache:
            return result
        debug = dict(result.get("debug") or {})
        errors = debug.get("errors") or []
        live_items = [dict(item) for item in result.get("items", []) if isinstance(item, dict)]
        cached_items = [dict(item) for item in cached_items_from_cache if isinstance(item, dict)]
        if not errors or not cached_items:
            result["items"] = live_items
            result["debug"] = debug
            return result

        fallback_source_ids: set[str] = set()
        fallback_reasons: dict[str, str] = {}
        for err in errors:
            if not isinstance(err, dict):
                continue
            source_id = err.get("sourceId", "")
            if not source_id or not self._should_fallback_to_cache(err):
                continue
            fallback_source_ids.add(source_id)
            fallback_reasons[source_id] = self._cache_reason(err)

        if not fallback_source_ids:
            result["items"] = live_items
            result["debug"] = debug
            return result

        existing_keys = {
            (item.get("sourceId", ""), item.get("rawBookUrl") or item.get("bookUrl", ""), item.get("name", ""))
            for item in live_items
        }
        merged_items = list(live_items)
        recovered = 0
        for item in cached_items:
            source_id = item.get("sourceId", "")
            if source_id not in fallback_source_ids:
                continue
            key = (source_id, item.get("rawBookUrl") or item.get("bookUrl", ""), item.get("name", ""))
            if key in existing_keys:
                continue
            item.setdefault("debug", {})
            if isinstance(item["debug"], dict):
                item["debug"]["cacheHit"] = True
                item["debug"]["cacheReason"] = fallback_reasons.get(source_id, "live_failure")
            item["cacheHit"] = True
            item["cacheReason"] = fallback_reasons.get(source_id, "live_failure")
            merged_items.append(item)
            existing_keys.add(key)
            recovered += 1

        if recovered:
            debug["cacheFallbackCount"] = recovered
            debug["cacheHit"] = True

        result["items"] = merged_items
        result["debug"] = debug
        return result

    async def test_source(
        self,
        source_id: str,
        keyword: str = "凡人修仙传",
        page: int = 1,
        stage: str = "search",
        proxy_mode_override: str | None = None,
    ) -> dict:
        """Test a single plugin without affecting active pool state."""
        plugin = self.scheduler._plugins.get(source_id)
        if not plugin:
            return {"pass": False, "error": "书源不存在", "stage": stage}

        result: dict[str, Any] = {
            "pass": False,
            "sourceId": source_id,
            "stage": stage,
            "proxyMode": proxy_mode_override or "auto",
            "keyword": keyword,
        }

        ctx = self.scheduler._make_ctx(source_id)
        t0 = time.perf_counter()
        search_timeout = self.scheduler.search_timeout_for_plugin(plugin)
        source_timeout = self.scheduler.timeout_for_plugin(plugin)
        try:
            if stage == "search":
                if "search" not in plugin.capabilities:
                    result["error"] = "插件不支持搜索"
                else:
                    items = await self._call_plugin(
                        plugin,
                        lambda: plugin.source.search(ctx, keyword, page),
                        timeout=search_timeout,
                    )
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    result["pass"] = True
                    result["itemsCount"] = len(items or [])
                    result["sample"] = [dict(i) if hasattr(i, "items") else i for i in (items or [])[:3]]
                    result["latencyMs"] = latency_ms

            elif stage == "detail":
                if "detail" not in plugin.capabilities:
                    result["error"] = "插件不支持详情"
                else:
                    book_url = plugin.metadata.base_urls[0] if plugin.metadata.base_urls else ""
                    detail = await self._call_plugin(
                        plugin,
                        lambda: plugin.source.detail(ctx, book_url),
                        timeout=source_timeout,
                    )
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    result["pass"] = True
                    result["data"] = dict(detail) if hasattr(detail, "items") else detail
                    result["latencyMs"] = latency_ms

            elif stage == "toc":
                if "toc" not in plugin.capabilities:
                    result["error"] = "插件不支持目录"
                else:
                    book_url = plugin.metadata.base_urls[0] if plugin.metadata.base_urls else ""
                    if "detail" in plugin.capabilities:
                        detail = await self._call_plugin(
                            plugin,
                            lambda: plugin.source.detail(ctx, book_url),
                            timeout=source_timeout,
                        )
                        if hasattr(detail, "get"):
                            book_url = detail.get("tocUrl", book_url)
                    chapters = await self._call_plugin(
                        plugin,
                        lambda: plugin.source.toc(ctx, book_url),
                        timeout=source_timeout,
                    )
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    result["pass"] = True
                    result["chaptersCount"] = len(chapters or [])
                    result["latencyMs"] = latency_ms

            elif stage == "content":
                if "chapter" not in plugin.capabilities:
                    result["error"] = "插件不支持正文"
                else:
                    book_url = plugin.metadata.base_urls[0] if plugin.metadata.base_urls else ""
                    if "detail" in plugin.capabilities:
                        detail = await self._call_plugin(
                            plugin,
                            lambda: plugin.source.detail(ctx, book_url),
                            timeout=source_timeout,
                        )
                        if hasattr(detail, "get"):
                            book_url = detail.get("tocUrl", book_url)
                    if "toc" in plugin.capabilities:
                        chapters = await self._call_plugin(
                            plugin,
                            lambda: plugin.source.toc(ctx, book_url),
                            timeout=source_timeout,
                        )
                        if chapters:
                            first_ch = chapters[0]
                            ch_url = first_ch.get("chapterUrl", "") if hasattr(first_ch, "get") else getattr(first_ch, "chapter_url", "")
                            content = await self._call_plugin(
                                plugin,
                                lambda: plugin.source.chapter(ctx, ch_url),
                                timeout=source_timeout,
                            )
                            latency_ms = int((time.perf_counter() - t0) * 1000)
                            result["pass"] = True
                            content_text = content.get("content", "") if hasattr(content, "get") else getattr(content, "content", "")
                            result["contentLength"] = len(content_text)
                            result["latencyMs"] = latency_ms
                        else:
                            result["error"] = "目录为空，无法测试正文"
                    else:
                        result["error"] = "插件不支持目录，无法获取章节URL"
            else:
                result["error"] = f"未知测试阶段: {stage}"

        except asyncio.TimeoutError:
            result["error"] = "timeout"
            result["latencyMs"] = int((time.perf_counter() - t0) * 1000)
        except Exception as e:
            result["error"] = str(e)
            result["latencyMs"] = int((time.perf_counter() - t0) * 1000)
        finally:
            await ctx._fetcher.close()

        self.repo.update_test_result(source_id, result)
        return result
