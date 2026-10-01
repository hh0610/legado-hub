"""示例有声书源（sample_audio_books）。

参考实现：演示 ``content.kind: audio`` 媒体书源的完整生命周期。
search/detail/toc/chapter 的调用方式与文字书源完全一致，只有 chapter
返回的是可播放的音频地址（``format: "audio"`` + ``mediaUrl``）而不是正文。

目录数据优先从站点 JSON API 拉取（``api.sample-audio-books.test``），
拉取失败时回退到内置目录，因此本插件离线也能完整工作。媒体文件指向
公开的 SoundHelix 示例 mp3（soundhelix.com），播放走本站媒体代理。
"""

from __future__ import annotations

API_BASE = "https://api.sample-audio-books.test"

# 公开示例音频（soundhelix.com 提供的免版权示例 mp3）。
_SAMPLE_TRACKS = [
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3", 301),
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3", 262),
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3", 278),
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3", 255),
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-5.mp3", 291),
    ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-6.mp3", 269),
]

_CATALOG = [
    {
        "id": "shengxu-shidai",
        "name": "有声示例·时光电台",
        "author": "林晚",
        "coverUrl": "",
        "intro": "示例有声书：一档关于城市夜晚的电台节目，共 6 集。",
        "bookStatus": "completed",
        "category": "电台",
    },
    {
        "id": "shengxu-xingqiu",
        "name": "有声示例·火星漫游指南",
        "author": "陆远",
        "coverUrl": "",
        "intro": "示例有声书：一部轻松的科幻广播剧，共 6 集。",
        "bookStatus": "completed",
        "category": "广播剧",
    },
]


def _book_url(book_id: str) -> str:
    return f"{API_BASE}/book/{book_id}"


def _episode_url(book_id: str, index: int) -> str:
    return f"{API_BASE}/book/{book_id}/ep/{index}"


class Source:
    id = "sample_audio_books"
    name = "示例有声书源"
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
        track_index = (max(1, episode) - 1) % len(_SAMPLE_TRACKS)
        media_url, duration = _SAMPLE_TRACKS[track_index]
        return {
            "sourceId": self.id,
            "title": f"第{episode:02d}集",
            "chapterUrl": chapter_url,
            "content": "",
            "format": "audio",
            "mediaUrl": media_url,
            "mediaType": "audio/mpeg",
            "durationSeconds": float(duration),
            "authRequired": False,
            "isPaid": False,
            "extra": {"streamHost": "soundhelix.com"},
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
    return len(_SAMPLE_TRACKS)
