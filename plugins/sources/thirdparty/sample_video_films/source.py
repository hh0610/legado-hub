"""示例视频源（sample_video_films）。

参考实现：演示 ``content.kind: video`` 媒体书源的完整生命周期。
chapter 返回 ``format: "video"`` + ``mediaUrl``，可以是直链 mp4，也可以是
m3u8 播放列表（由本站媒体代理自动重写，前端用 hls.js 播放）。

目录数据优先从站点 JSON API 拉取（``api.sample-video-films.test``），
失败时回退内置目录，因此本插件离线也能完整工作。媒体文件使用公开的
Big Buck Bunny 测试流（test-streams.mux.dev 的 HLS 与 test-videos.co.uk
的 mp4），用于验证视频播放与 HLS 代理链路。
"""

from __future__ import annotations

API_BASE = "https://api.sample-video-films.test"

# 公开测试视频流（验证直链 mp4 与 HLS 两条播放路径）。
_TEST_STREAMS = [
    ("https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", "application/vnd.apple.mpegurl", 596),
    ("https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4", "video/mp4", 10),
]

_CATALOG = [
    {
        "id": "shipin-tuyuan",
        "name": "视频示例·兔原野",
        "author": "Blender 基金会",
        "coverUrl": "",
        "intro": "示例视频：Big Buck Bunny 公开测试影片拆分的分集演示，共 2 集。",
        "bookStatus": "completed",
        "category": "动画",
    },
    {
        "id": "shipin-duanjutai",
        "name": "视频示例·短剧台",
        "author": "示例工作组",
        "coverUrl": "",
        "intro": "示例视频：用于验证 HLS 与 mp4 播放的演示短剧，共 2 集。",
        "bookStatus": "completed",
        "category": "短剧",
    },
]


def _book_url(book_id: str) -> str:
    return f"{API_BASE}/book/{book_id}"


def _episode_url(book_id: str, index: int) -> str:
    return f"{API_BASE}/book/{book_id}/ep/{index}"


class Source:
    id = "sample_video_films"
    name = "示例视频源"
    contract_version = "1.0"
    last_modified = "2026-10-01"

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        catalog = await self._load_catalog(ctx)
        keyword = (keyword or "").strip()
        if page > 1:
            return []
        items = []
        for book in catalog:
            if keyword and keyword not in book["name"] and keyword not in book["author"]:
                continue
            items.append(
                {
                    "sourceId": self.id,
                    "name": book["name"],
                    "author": book["author"],
                    "bookUrl": _book_url(book["id"]),
                    "coverUrl": book["coverUrl"],
                    "intro": book["intro"],
                    "kind": book["category"],
                    "lastChapter": f"第{_episode_count(book['id'])}集",
                    "wordCount": "",
                    "score": 0,
                    "extra": {},
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        book_id = self._book_id_from_url(book_url)
        catalog = await self._load_catalog(ctx)
        book = self._find_book(catalog, book_id)
        return {
            "sourceId": self.id,
            "name": book["name"],
            "author": book["author"],
            "bookUrl": _book_url(book_id),
            "coverUrl": book["coverUrl"],
            "intro": book["intro"],
            "kind": book["category"],
            "lastChapter": f"第{_episode_count(book_id)}集",
            "wordCount": "",
            "tocUrl": _book_url(book_id),
            "bookStatus": book["bookStatus"],
            "authRequired": False,
            "extra": {},
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        book_id = self._book_id_from_url(toc_url)
        chapters = []
        total = _episode_count(book_id)
        for index in range(1, total + 1):
            chapters.append(
                {
                    "sourceId": self.id,
                    "index": index,
                    "title": f"第{index:02d}集",
                    "chapterUrl": _episode_url(book_id, index),
                    "updateTime": "",
                    "isVip": False,
                    "isLocked": False,
                    "extra": {},
                }
            )
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        book_id, episode = self._episode_from_url(chapter_url)
        stream_index = (max(1, episode) - 1) % len(_TEST_STREAMS)
        media_url, media_mime, duration = _TEST_STREAMS[stream_index]
        return {
            "sourceId": self.id,
            "title": f"第{episode:02d}集",
            "chapterUrl": chapter_url,
            "content": "",
            "format": "video",
            "mediaUrl": media_url,
            "mediaType": media_mime,
            "durationSeconds": float(duration),
            "authRequired": False,
            "isPaid": False,
            "extra": {"streamHost": media_url.split("/", 3)[2]},
        }

    # ---- helpers -------------------------------------------------------

    async def _load_catalog(self, ctx) -> list[dict]:
        """站点 JSON API 优先；本插件离线可运行，失败即回退内置目录。"""
        try:
            payload = await ctx.access.http.fetch_json(f"{API_BASE}/api/catalog.json")
            books = payload.get("books") if isinstance(payload, dict) else None
            if isinstance(books, list) and books:
                return books
        except Exception:
            ctx.trace("catalog_api_unavailable", {"fallback": "builtin"})
        return _CATALOG

    @staticmethod
    def _find_book(catalog: list[dict], book_id: str) -> dict:
        for book in catalog:
            if book.get("id") == book_id:
                return book
        return catalog[0]

    @staticmethod
    def _book_id_from_url(url: str) -> str:
        text = str(url or "")
        marker = "/book/"
        if marker in text:
            tail = text.split(marker, 1)[1]
            return tail.split("/", 1)[0].split("?", 1)[0] or _CATALOG[0]["id"]
        return _CATALOG[0]["id"]

    @staticmethod
    def _episode_from_url(url: str) -> tuple[str, int]:
        text = str(url or "")
        marker = "/book/"
        book_id = _CATALOG[0]["id"]
        episode = 1
        if marker in text:
            tail = text.split(marker, 1)[1]
            book_id = tail.split("/", 1)[0].split("?", 1)[0] or book_id
            rest = tail.split("/", 1)[1] if "/" in tail else ""
            digits = "".join(ch for ch in rest.split("?", 1)[0] if ch.isdigit())
            if digits:
                episode = int(digits)
        return book_id, episode


def _episode_count(book_id: str) -> int:
    return len(_TEST_STREAMS)
