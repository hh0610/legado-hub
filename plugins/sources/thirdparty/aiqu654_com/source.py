"""Plugin for 爱去小说网 (aiqu226.com; rotated from aiqu654.com)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, quote_from_bytes, urlencode, urldefrag, urljoin

from bs4 import BeautifulSoup


CHAPTER_RE = re.compile(r"^第\s*[0-9零〇一二三四五六七八九十百千两]+\s*章")
SPECIAL_CHAPTER_RE = re.compile(r"^(?:番外|楔子|序章|序言|前言|后记|尾声|终章)")
SOFT_ID_RE = re.compile(r"(?:txt-|softid=)(\d+)")


class Source:
    """Adapt the site's public TXT downloads with byte-range chapter reads."""

    id = "aiqu654_com"
    name = "爱去小说网"
    contract_version = "1.0"
    last_modified = "2026-09-15"
    base_url = "https://www.aiqu226.com"
    download_page_base = "https://www.aiqu226.com"

    async def _fetch_page(self, ctx, url: str) -> str:
        return await ctx.access.http.fetch_text(url)

    async def _fetch_txt(self, ctx, url: str, **kwargs) -> bytes:
        # Whole-book TXT is ~1.4 MB and the file host can be slow; don't let
        # the host's short default fetch timeout cut the download short.
        kwargs.setdefault("timeout", 30.0)
        return await ctx.access.stealth.fetch_bytes(url, **kwargs)

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        keyword = (keyword or "").strip()
        if page > 1 or not keyword:
            return []
        encoded = quote_from_bytes(keyword.encode("gbk", errors="replace"))
        html = await self._fetch_page(ctx, f"{self.base_url}/search.asp?word={encoded}")
        items = self._parse_search(ctx, html)
        exact = [item for item in items if keyword.casefold() in item.get("name", "").casefold()]
        completed: list[dict] = []
        for item in (exact or items)[:1]:
            txt_url = await self._resolve_txt_url(ctx, item["bookUrl"])
            raw = await self._fetch_txt(ctx, txt_url)
            snapshot = self._snapshot(ctx, raw, item.get("bookStatus", ""))
            if not snapshot["chapters"]:
                continue
            item.update(self._subscription_fields(snapshot, txt_url))
            item["rank"] = len(completed) + 1
            completed.append(item)
        return completed

    def _parse_search(self, ctx, html: str) -> list[dict]:
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for card in soup.select(".search-card"):
            link = card.select_one("a.searchtitle[href]")
            if link is None:
                continue
            book_url = self._book_url(link.get("href", ""))
            if not self._soft_id(book_url) or book_url in seen:
                continue
            seen.add(book_url)
            author_node = card.select_one("a.search-card-author")
            category = self._text(card, ".search-card-category")
            body = ctx.clean_text(card.get_text(" ", strip=True))
            status = self._status(body)
            items.append(
                {
                    "sourceId": self.id,
                    "name": ctx.clean_text(link.get("title", "")).strip("《》"),
                    "author": ctx.clean_text(author_node.get_text(" ", strip=True)).replace("作者：", "") if author_node else "",
                    "bookUrl": book_url,
                    "tocUrl": "",
                    "coverUrl": "",
                    "intro": ctx.clean_text(self._text(card, ".search-card-content")),
                    "kind": " / ".join(part for part in (category, status) if part),
                    "bookStatus": status,
                    "lastChapter": "",
                    "wordCount": "",
                    "updateTime": self._text(card, ".search-card-date"),
                    "chapterCount": 0,
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        book_url = self._book_url(book_url)
        html = await self._fetch_page(ctx, book_url)
        soup = BeautifulSoup(html or "", "html.parser")
        txt_url = await self._resolve_txt_url(ctx, book_url)
        raw = await self._fetch_txt(ctx, txt_url)
        snapshot = self._snapshot(ctx, raw, ctx.clean_text(soup.get_text(" ", strip=True)))
        fields = self._subscription_fields(snapshot, txt_url)
        return {
            "sourceId": self.id,
            "name": snapshot["name"],
            "author": snapshot["author"],
            "bookUrl": book_url,
            "tocUrl": txt_url,
            "coverUrl": "",
            "intro": snapshot["intro"],
            "kind": " / ".join(part for part in (snapshot["tags"], fields["bookStatus"]) if part),
            "bookStatus": fields["bookStatus"],
            "lastChapter": fields["lastChapter"],
            "wordCount": f"{len(raw.decode('utf-8-sig', errors='replace'))} 字",
            "updateTime": "",
            "chapterCount": fields["chapterCount"],
            "authRequired": False,
        }

    async def _resolve_txt_url(self, ctx, book_url: str) -> str:
        soft_id = self._soft_id(book_url)
        page_url = f"{self.download_page_base}/txt-xx/softdownfree.asp?softid={soft_id}&ckm=mianfei"
        html = await self._fetch_page(ctx, page_url)
        soup = BeautifulSoup(html or "", "html.parser")
        links = soup.select("a[href]")
        preferred = next((a for a in links if "第一下载地址" in a.get_text(" ", strip=True)), None)
        preferred = preferred or next((a for a in links if "在线阅读" in a.get_text(" ", strip=True)), None)
        return quote(preferred.get("href", ""), safe=":/?=&%") if preferred else ""

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        raw = await self._fetch_txt(ctx, toc_url)
        snapshot = self._snapshot(ctx, raw, "")
        chapters: list[dict] = []
        for index, chapter in enumerate(snapshot["chapters"], 1):
            fragment = urlencode(
                {
                    "chapter": index,
                    "start": chapter["start"],
                    "end": chapter["end"],
                    "title": chapter["title"],
                }
            )
            chapters.append(
                {
                    "sourceId": self.id,
                    "index": index,
                    "title": chapter["title"],
                    "chapterUrl": f"{toc_url}#{fragment}",
                    "updateTime": "",
                    "isVip": False,
                    "isLocked": False,
                }
            )
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        txt_url, fragment = urldefrag(chapter_url)
        args = parse_qs(fragment)
        start = int((args.get("start") or ["0"])[0])
        end = int((args.get("end") or ["0"])[0])
        title = (args.get("title") or [""])[0]
        if end <= start:
            content = ""
        else:
            raw = await self._fetch_txt(ctx, txt_url, headers={"Range": f"bytes={start}-{end - 1}"})
            expected = end - start
            if len(raw) != expected:
                raw = raw[start:end] if len(raw) >= end else raw[:expected]
            content = self._chapter_text(ctx, raw)
        return {
            "sourceId": self.id,
            "title": title,
            "chapterUrl": chapter_url,
            "content": content,
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _snapshot(self, ctx, raw: bytes, status_hint: str) -> dict:
        chapters = self._chapter_records(ctx, raw)
        lines = self._decoded_lines(ctx, raw)
        header = next((line for line in lines[:80] if "《" in line and "》作者" in line), "")
        name_match = re.search(r"《([^》]+)》", header)
        author_match = re.search(r"作者[：:]\s*(.+)$", header)
        tags = self._prefixed_value(lines[:100], "内容标签")
        return {
            "name": name_match.group(1).strip() if name_match else "未知书名",
            "author": author_match.group(1).strip() if author_match else "佚名",
            "status": self._status(status_hint) or "状态未知",
            "tags": tags,
            "intro": self._intro(lines, chapters[0]["lineIndex"] if chapters else len(lines)),
            "chapters": chapters,
        }

    def _chapter_records(self, ctx, raw: bytes) -> list[dict]:
        markers: list[dict] = []
        offset = 0
        line_index = 0
        for line in raw.splitlines(keepends=True):
            text = line.decode("utf-8-sig" if offset == 0 else "utf-8", errors="replace")
            title = ctx.clean_text(text)
            if CHAPTER_RE.match(title) or SPECIAL_CHAPTER_RE.match(title):
                markers.append(
                    {
                        "title": title,
                        "heading": offset,
                        "start": offset + len(line),
                        "lineIndex": line_index,
                    }
                )
            offset += len(line)
            if title:
                line_index += 1
        for i, marker in enumerate(markers):
            marker["end"] = markers[i + 1]["heading"] if i + 1 < len(markers) else len(raw)
        return markers

    def _decoded_lines(self, ctx, raw: bytes) -> list[str]:
        text = raw.decode("utf-8-sig", errors="replace")
        return [cleaned for line in text.splitlines() if (cleaned := ctx.clean_text(line))]

    def _intro(self, lines: list[str], end: int) -> str:
        try:
            start = next(i for i, line in enumerate(lines[:end]) if line in {"文案：", "文案:"}) + 1
        except StopIteration:
            return ""
        parts: list[str] = []
        for line in lines[start:end]:
            if line.startswith(("内容标签：", "内容标签:", "主角：", "主角:")):
                break
            if not self._is_noise(line):
                parts.append(line)
        return "\n".join(parts).strip()

    def _chapter_text(self, ctx, raw: bytes) -> str:
        lines = self._decoded_lines(ctx, raw)
        return "\n\n".join(line for line in lines if not self._is_noise(line)).strip()

    def _subscription_fields(self, snapshot: dict, txt_url: str) -> dict:
        chapters = snapshot["chapters"]
        return {
            "name": snapshot["name"],
            "author": snapshot["author"],
            "tocUrl": txt_url,
            "intro": snapshot["intro"],
            "kind": " / ".join(part for part in (snapshot["tags"], snapshot["status"]) if part),
            "bookStatus": snapshot["status"],
            "lastChapter": chapters[-1]["title"] if chapters else "",
            "chapterCount": len(chapters),
        }

    def _prefixed_value(self, lines: list[str], label: str) -> str:
        for line in lines:
            match = re.match(rf"^{re.escape(label)}[：:]\s*(.*)$", line)
            if match:
                return match.group(1).strip()
        return ""

    def _status(self, text: str) -> str:
        if re.search(r"完结|完本|全本|番外完", text or ""):
            return "已完结"
        if re.search(r"连载|未完|更新至", text or ""):
            return "连载中"
        return ""

    def _is_noise(self, line: str) -> bool:
        if len(line) > 100:
            return False
        return any(marker in line for marker in ("小说下载必备网址", "每天更新，喜欢的去看看", "www.599txt.com"))

    def _text(self, node, selector: str) -> str:
        found = node.select_one(selector)
        return found.get_text(" ", strip=True) if found else ""

    def _soft_id(self, value: str) -> str:
        match = SOFT_ID_RE.search(value or "")
        return match.group(1) if match else ""

    def _book_url(self, value: str) -> str:
        url = urljoin(self.base_url, (value or "").strip())
        return re.sub(r"^https?://www\.aiqu(?:654|127|226)\.com", self.base_url, url)
