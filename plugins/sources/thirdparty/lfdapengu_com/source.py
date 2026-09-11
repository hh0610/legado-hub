"""猫眼看书 source plugin.

纯 JSON API 源（猫眼看书安卓 App 接口）。业务 API 主机在多个域名间轮换，
章节列表中的 ``path`` 为 AES/CBC/PKCS5Padding 加密的 Base64 字符串，解密后
是指向内容 CDN（api.jxgtzxc.com）的绝对 URL。

接口概览：
- 搜索: ``GET /search?keyword={key}&page={page}``
- 详情: ``GET /novel/{novelId}?isSearch=1``
- 目录: ``GET /novel/{novelId}/chapters``
- 正文: 解密后的绝对 URL，返回 ``{"content": "..."}``
"""

from __future__ import annotations

import base64
import re
import unicodedata
from urllib.parse import urlsplit

from app.source_plugins.errors import ParseEmpty, ParseError
from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    id = "lfdapengu_com"
    name = "猫眼看书"
    contract_version = "1.0"
    last_modified = "2026-09-11"
    base_url = "http://api.lfdapengu.com"

    # 书源公开携带的 App 级请求头；JWT 为 App 匿名令牌（exp 2028），非用户凭证。
    # 业务主机轮换时这些头仍然适用（api.myweipin.com / api.jmlldsc.com /
    # api.lemiyigou.com 为书源注释中记录的备用主机）。
    headers = {
        "User-Agent": "okhttp/4.9.2",
        "client-device": "0cdeb38dd0f2a381b06c0a02926ee317",
        "client-brand": "vivo",
        "client-version": "2.3.0",
        "client-name": "app.maoyankanshu.novel",
        "client-source": "android",
        "Authorization": "bearereyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJodHRwOlwvXC9hcGkuanhndHp4Yy5jb21cL2F1dGhcL2xvZ2luYnltb2JpbGUiLCJpYXQiOjE3MzU2MTQ3NzEsImV4cCI6MTgyODkyNjc3MSwibmJmIjoxNzM1NjE0NzcxLCJqdGkiOiI1VEdjdXpoOHNSNVk5WlNjIiwic3ViIjo4MTEzMzQsInBydiI6ImExY2IwMzcxODAyOTZjNmExOTM4ZWYzMGI0Mzc5NDY3MmRkMDE2YzUifQ.-dT55vUMI-JJyfl3a9__Ii-DjxbyvnlOMoXWdG1c8JA",
    }

    # AES/CBC/PKCS5Padding（对 AES 而言 PKCS5Padding 即 PKCS7）。
    _AES_KEY = b"f041c49714d39908"
    _AES_IV = b"0123456789abcdef"
    _NOISE_RE = re.compile(r"一秒记住.*精彩阅读。|7017k")

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        keyword = (keyword or "").strip()
        if not keyword or page < 1:
            return []
        payload = await ctx.access.http.fetch_json(
            f"{self.base_url}/search",
            params={"keyword": keyword, "page": page},
            headers=self.headers,
        )
        rows = (payload or {}).get("data")
        if not isinstance(rows, list):
            return []
        items: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            novel_id = str(row.get("novelId") or "").strip()
            name = self._clean(ctx, row.get("novelName"))
            if not novel_id or not name:
                continue
            items.append(
                {
                    "sourceId": self.id,
                    "name": name,
                    "author": self._clean(ctx, row.get("authorName")),
                    "bookUrl": f"{self.base_url}/novel/{novel_id}?isSearch=1",
                    "tocUrl": f"{self.base_url}/novel/{novel_id}/chapters",
                    "coverUrl": str(row.get("cover") or "").strip(),
                    "intro": self._clean(ctx, row.get("summary")),
                    "kind": self._categories(row),
                    "bookStatus": self._status(row.get("isComplete")),
                    "lastChapter": "",
                    "wordCount": self._clean(ctx, row.get("wordNum")),
                    "updateTime": self._clean(ctx, row.get("createdAt")),
                    "chapterCount": 0,
                    "rank": len(items) + 1,
                }
            )
        # 搜索列表没有 lastChapter，仅对首条候选补一次详情：该接口响应偏慢
        # （书源记录 respondTime 约 25s），而搜索 fast 阶段单源预算只有 5s，
        # 串行补全多条极易触发 PLUGIN_TIMEOUT。其余字段列表已自带。
        return await enrich_search_items_from_detail(self, ctx, items, limit=1)

    async def detail(self, ctx, book_url: str) -> dict:
        url = self._absolute_url(book_url)
        payload = await ctx.access.http.fetch_json(url, headers=self.headers)
        data = (payload or {}).get("data")
        if not isinstance(data, dict) or not data:
            raise ParseEmpty(f"猫眼看书详情为空: {url}")
        novel_id = str(data.get("novelId") or self._novel_id(url))
        origin = self._origin(url)
        last = data.get("lastChapter")
        last_name = self._clean(ctx, last.get("chapterName")) if isinstance(last, dict) else ""
        updated = self._clean(ctx, data.get("lastUpdatedAt"))
        last_chapter = "•".join(part for part in (last_name, updated) if part)
        return {
            "sourceId": self.id,
            "name": self._clean(ctx, data.get("novelName")),
            "author": self._clean(ctx, data.get("authorName")),
            "bookUrl": url,
            "tocUrl": f"{origin}/novel/{novel_id}/chapters",
            "coverUrl": str(data.get("cover") or "").strip(),
            "intro": self._clean(ctx, data.get("summary")),
            "kind": self._categories(data),
            "bookStatus": self._status(data.get("isComplete")),
            "lastChapter": last_chapter,
            "wordCount": self._clean(ctx, data.get("wordNum")),
            "updateTime": updated,
            "chapterCount": int(data.get("chapterNum") or 0),
            "authRequired": False,
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        toc_url = self._absolute_url(toc_url)
        payload = await ctx.access.http.fetch_json(toc_url, headers=self.headers)
        data = (payload or {}).get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ParseEmpty(f"猫眼看书目录为空: {toc_url}")
        chapters: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = self._clean(ctx, row.get("chapterName"))
            encrypted = str(row.get("path") or "").strip()
            if not title or not encrypted:
                continue
            try:
                chapter_url = self._decrypt_path(encrypted)
            except ParseError as exc:
                ctx.trace("toc_decrypt_error", url=toc_url, message=str(exc))
                continue
            if not chapter_url or chapter_url in seen:
                continue
            seen.add(chapter_url)
            word_num = self._clean(ctx, row.get("wordNum"))
            updated = self._clean(ctx, row.get("updatedAt"))
            chapters.append(
                {
                    "sourceId": self.id,
                    "index": len(chapters) + 1,
                    "title": title,
                    "chapterUrl": chapter_url,
                    "updateTime": f"{updated} | {word_num}字" if updated and word_num else updated,
                    "isVip": False,
                    "isLocked": False,
                }
            )
        if not chapters:
            raise ParseEmpty(f"猫眼看书目录解密后无有效章节: {toc_url}")
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        chapter_url = self._absolute_url(chapter_url)
        payload = await ctx.access.http.fetch_json(chapter_url, headers=self.headers)
        content = (payload or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ParseEmpty(f"猫眼看书正文为空: {chapter_url}")
        return {
            "sourceId": self.id,
            # 正文 JSON 仅含 content，标题由目录侧信息补齐。
            "title": "",
            "chapterUrl": chapter_url,
            "content": self._content(ctx, content),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _decrypt_path(self, encrypted: str) -> str:
        """解密目录项 path；加密库在调用路径内延迟导入。"""
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad

            raw = base64.b64decode(encrypted, validate=True)
            cipher = AES.new(self._AES_KEY, AES.MODE_CBC, self._AES_IV)
            plain = unpad(cipher.decrypt(raw), AES.block_size)
            return plain.decode("utf-8").strip()
        except Exception as exc:
            raise ParseError(f"猫眼看书章节路径 AES 解密失败: {exc}") from exc

    def _content(self, ctx, content: str) -> str:
        lines: list[str] = []
        for line in content.splitlines():
            line = self._clean(ctx, self._NOISE_RE.sub("", line))
            if line:
                lines.append(line)
        return "\n\n".join(lines)

    def _categories(self, row: dict) -> str:
        names: list[str] = []
        for item in row.get("categoryNames") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("className") or "").strip()
            if name and name not in names:
                names.append(name)
        return " / ".join(names)

    def _status(self, value: object) -> str:
        return "已完结" if value in (1, True, "1") else "连载中"

    def _clean(self, ctx, value: object) -> str:
        # 接口数据中夹杂退格、取代等控制字符（Cc）以及不可见格式字符（Cf）。
        visible = "".join(
            char
            for char in str(value or "")
            if unicodedata.category(char) not in ("Cc", "Cf")
        )
        return ctx.clean_text(visible)

    def _absolute_url(self, value: str) -> str:
        url = str(value or "").strip()
        if url.startswith(("http://", "https://")):
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return self.base_url

    def _novel_id(self, url: str) -> str:
        match = re.search(r"/novel/([^/?#]+)", urlsplit(url).path)
        return match.group(1) if match else ""
