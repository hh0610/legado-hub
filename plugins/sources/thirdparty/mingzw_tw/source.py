"""Plugin for 明智屋 (tw.mingzw.net)."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    """Adapt the Traditional Chinese HTML site to the source contract."""

    id = "mingzw_tw"
    name = "明智屋"
    contract_version = "1.0"
    last_modified = "2026-07-27"
    base_url = "https://tw.mingzw.net"
    headers = {"accept-language": "zh-TW,zh;q=0.9"}

    async def _fetch(self, ctx, url: str) -> str:
        """Fetch a site page through the host-owned HTTP bridge."""
        return await ctx.access.http.fetch_text(
            urljoin(self.base_url, url),
            headers=self.headers,
        )

    def _s(self, ctx, value: str) -> str:
        """Convert user-facing Traditional Chinese text to Simplified Chinese."""
        return ctx.to_simplified(ctx.clean_text(value or ""))

    def _body(self, ctx, value: str) -> str:
        """Convert chapter content without collapsing its paragraph boundaries."""
        return ctx.to_simplified(value or "").strip()

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search the title index and enrich the first sparse results."""
        if page > 1:
            return []
        keyword = keyword.strip()
        search_keyword = ctx.to_traditional(keyword)
        # The site's title index misses longer keywords at times; fall back to
        # progressively shorter prefixes and filter the results locally.
        keywords = [search_keyword]
        for size in (3, 2):
            if len(search_keyword) > size and search_keyword[:size] not in keywords:
                keywords.append(search_keyword[:size])
        items: list[dict] = []
        for kw in keywords:
            url = f"{self.base_url}/mzwlist/{quote(kw, safe='')}.html"
            html = await self._fetch(ctx, url)
            items = self._parse_search(ctx, html)
            if items:
                break
        exact = [item for item in items if keyword and keyword in item.get("name", "")]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse one horizontal result card per book."""
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for card in soup.select("#list .bd .figure-horizontal"):
            title_link = card.select_one('h3 a[href*="/mzwbook/"]')
            if title_link is None:
                continue
            href = title_link.get("href", "")
            book_url = urljoin(self.base_url, href)
            if not href or book_url in seen:
                continue
            seen.add(book_url)
            name = title_link.get("title", "") or title_link.get_text(" ", strip=True)
            author_node = card.select_one(".cont dl:nth-of-type(1) dd")
            cover_node = card.select_one(".pic img[src]")
            intro_node = card.select_one(".cont > p")
            status_node = card.select_one(".pic .info")
            latest_node = card.select_one(".cont a.update")
            intro = intro_node.get_text(" ", strip=True) if intro_node else ""
            intro = re.sub(r"查看詳[細情].*$", "", intro).strip()
            items.append(
                {
                    "sourceId": self.id,
                    "name": self._s(ctx, name),
                    "author": self._s(ctx, author_node.get_text(" ", strip=True) if author_node else ""),
                    "bookUrl": book_url,
                    "coverUrl": urljoin(self.base_url, cover_node.get("src", "")) if cover_node else "",
                    "intro": self._s(ctx, intro),
                    "kind": self._s(ctx, status_node.get_text(" ", strip=True) if status_node else ""),
                    "lastChapter": self._s(ctx, latest_node.get_text(" ", strip=True) if latest_node else ""),
                    "wordCount": "",
                    "rank": len(items) + 1,
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        """Read the book page and locate its complete catalog URL."""
        book_url = urljoin(self.base_url, book_url)
        html = await self._fetch(ctx, book_url)
        name = ctx.text(html, ".header .novel-name").strip("《》 ")
        author = ctx.text(html, ".picinfo dd:nth-of-type(1) a") or ctx.text(html, ".picinfo dd:nth-of-type(1)")
        status = ctx.text(html, ".header .status")
        tags = [self._s(ctx, node.text_content()) for node in ctx.select(html, "#Lab_Keywords a")]
        kind = " / ".join(part for part in [status, *tags] if part)
        intro = ctx.text(html, ".cont .desc .content")
        cover = ctx.attr(html, ".pic .piclink img", "src")
        latest = ctx.text(html, ".otherinfo .newsection a")
        toc_href = ctx.attr(html, '.view-btn a.view-all-btn[href*="mzwchapter"]', "href")
        if not toc_href:
            book_id = self._book_id(book_url)
            toc_href = f"/mzwchapter/{book_id}.html" if book_id else book_url
        return {
            "sourceId": self.id,
            "name": self._s(ctx, name),
            "author": self._s(ctx, author),
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, cover) if cover else "",
            "intro": self._s(ctx, intro),
            "kind": self._s(ctx, kind),
            "lastChapter": self._s(ctx, latest),
            "wordCount": "",
            "updateTime": self._update_time(ctx.text(html, ".picinfo")),
            "tocUrl": urljoin(self.base_url, toc_href),
            "authRequired": False,
        }

    def _book_id(self, book_url: str) -> str:
        """Extract the numeric book identifier from a detail URL."""
        match = re.search(r"/mzwbook/(\d+)\.html", book_url or "", re.IGNORECASE)
        return match.group(1) if match else ""

    def _update_time(self, text: str) -> str:
        """Extract the stable date or timestamp shown in book metadata."""
        match = re.search(r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?)", text or "")
        return match.group(1) if match else ""

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Parse and reorder the multi-column catalog by increasing chapter ID."""
        toc_url = urljoin(self.base_url, toc_url)
        html = await self._fetch(ctx, toc_url)
        raw: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for link in ctx.select(html, '#section-free li a[href*="/mzwread/"]'):
            href = link.get("href", "")
            chapter_url = urljoin(self.base_url, href)
            title = self._s(ctx, link.text_content())
            if not href or not title or chapter_url in seen:
                continue
            seen.add(chapter_url)
            raw.append((self._chapter_id(chapter_url), title, chapter_url))
        raw.sort(key=lambda item: item[0])
        return [
            {
                "sourceId": self.id,
                "index": index,
                "title": title,
                "chapterUrl": chapter_url,
                "updateTime": "",
                "isVip": False,
                "isLocked": False,
            }
            for index, (_, title, chapter_url) in enumerate(raw, start=1)
        ]

    def _chapter_id(self, chapter_url: str) -> int:
        """Return the monotonically increasing chapter ID used by this site."""
        match = re.search(r"_(\d+)\.html", chapter_url or "", re.IGNORECASE)
        return int(match.group(1)) if match else 2**63 - 1

    async def chapter(self, ctx, chapter_url: str) -> dict:
        """Read one chapter and stop before the recommendation block."""
        chapter_url = urljoin(self.base_url, chapter_url)
        html = await self._fetch(ctx, chapter_url)
        title_raw = self._chapter_title(ctx, html)
        content = self._chapter_content(ctx, ctx.html(html, ".contents"), title_raw)
        return {
            "sourceId": self.id,
            "title": self._s(ctx, title_raw),
            "chapterUrl": chapter_url,
            "content": self._body(ctx, content),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_title(self, ctx, html: str) -> str:
        """Extract the chapter title from the site's SEO document title."""
        page_title = ctx.text(html, "title")
        match = re.search(r"最新章[節节]，(.+?)，.+?的最新章[節节]", page_title or "")
        if match:
            return ctx.clean_text(match.group(1))
        content_text = ctx.text(html, ".contents")
        first_line = next((line.strip() for line in content_text.splitlines() if line.strip()), "")
        return first_line.split("_", 1)[0]

    def _chapter_content(self, ctx, content_html: str, title: str) -> str:
        """Convert the chapter body to paragraphs and drop page chrome."""
        soup = BeautifulSoup(content_html or "", "html.parser")
        for node in soup.select("script, style, iframe"):
            node.decompose()
        for separator in soup.select("p, br"):
            separator.replace_with("\n")
        lines = [ctx.clean_text(line) for line in soup.get_text("\n").splitlines()]
        result: list[str] = []
        compact_title = re.sub(r"[\s，,。！？!?]", "", title or "")
        for line in lines:
            if not line:
                continue
            if "新書推薦" in line or "新书推荐" in line or line.startswith("請：m."):
                break
            compact_line = re.sub(r"[\s，,。！？!?]", "", line)
            if not result and ("_" in line or (compact_title and compact_line.startswith(compact_title))):
                continue
            result.append(line)
        return "\n\n".join(result).strip()
