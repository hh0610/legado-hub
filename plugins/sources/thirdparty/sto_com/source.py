"""Plugin for 思兔阅读 (sto9.com)."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    """Adapt the verified sto9 HTML endpoints to the source contract."""

    id = "sto_com"
    name = "思兔阅读"
    contract_version = "1.0"
    last_modified = "2026-07-27"
    base_url = "https://sto9.com"
    headers = {"accept-language": "zh-TW,zh;q=0.9"}

    def _s(self, ctx, value: str) -> str:
        """Normalize user-facing Traditional Chinese text."""
        return ctx.to_simplified(ctx.clean_text(value or ""))

    def _body(self, ctx, value: str) -> str:
        """Convert chapter content without collapsing its paragraph boundaries."""
        return ctx.to_simplified(value or "").strip()

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        """Fetch a site page through the stealth HTTP bridge (Cloudflare)."""
        return await ctx.access.stealth.fetch_text(
            urljoin(self.base_url, url),
            headers=self.headers,
            **kwargs,
        )

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search titles through the site's paginated search path."""
        keyword = keyword.strip()
        if not keyword:
            return []
        html = await self._fetch(
            ctx,
            f"/search/{quote(keyword, safe='')}/{max(1, page)}.html",
        )
        items = self._parse_search(ctx, html)
        exact = [item for item in items if item.get("name") == keyword.strip()]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse one list item per search result."""
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for cover_link in soup.select('a.imgbox[href*="/book/"]'):
            card = cover_link.find_parent("li")
            if card is None:
                continue
            href = cover_link.get("href", "")
            book_url = urljoin(self.base_url, href)
            title_node = card.select_one('h3 a[href*="/book/"]')
            name = self._s(ctx, title_node.get_text(" ", strip=True) if title_node else "")
            if not href or not name or book_url in seen:
                continue
            seen.add(book_url)
            labels = [self._s(ctx, node.get_text(" ", strip=True)) for node in card.select(".labelbox label")]
            cover_node = card.select_one("img[data-src], img[src]")
            cover = cover_node.get("data-src", "") or cover_node.get("src", "") if cover_node else ""
            latest_node = card.select_one(".zxzj a")
            intro_node = card.select_one(".ellipsis_2")
            items.append(
                {
                    "sourceId": self.id,
                    "name": name,
                    "author": labels[0] if labels else "",
                    "bookUrl": book_url,
                    "coverUrl": urljoin(self.base_url, cover) if cover else "",
                    "intro": self._s(ctx, intro_node.get_text(" ", strip=True) if intro_node else ""),
                    "kind": " / ".join(labels[1:]),
                    "lastChapter": self._s(ctx, latest_node.get_text(" ", strip=True) if latest_node else ""),
                    "wordCount": "",
                    "rank": len(items) + 1,
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        """Read stable OpenGraph novel metadata."""
        book_url = urljoin(self.base_url, book_url)
        html = await self._fetch(ctx, book_url)
        name = self._meta(ctx, html, "og:novel:book_name") or self._meta(ctx, html, "og:title")
        category = self._meta(ctx, html, "og:novel:category")
        status = self._meta(ctx, html, "og:novel:status")
        book_id = self._book_id(book_url)
        return {
            "sourceId": self.id,
            "name": self._s(ctx, name),
            "author": self._s(ctx, self._meta(ctx, html, "og:novel:author")),
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, self._meta(ctx, html, "og:image")),
            "intro": self._s(ctx, ctx.attr(html, 'meta[name="description"]', "content")),
            "kind": self._s(ctx, " / ".join(part for part in [category, status] if part)),
            "lastChapter": self._s(ctx, self._meta(ctx, html, "og:novel:latest_chapter_name")),
            "wordCount": "",
            "updateTime": self._meta(ctx, html, "og:novel:update_time"),
            "tocUrl": f"{self.base_url}/ajax_novels/chapterlist/{book_id}.html" if book_id else book_url,
            "authRequired": False,
        }

    def _meta(self, ctx, html: str, property_name: str) -> str:
        """Return one OpenGraph property value."""
        return ctx.attr(html, f'meta[property="{property_name}"]', "content")

    def _book_id(self, url: str) -> str:
        """Extract the numeric book identifier from a detail URL."""
        match = re.search(r"/book/(\d+)(?:\.html|/|$)", url or "", re.IGNORECASE)
        return match.group(1) if match else ""

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Parse the site's complete AJAX catalog."""
        toc_url = urljoin(self.base_url, toc_url)
        html = await self._fetch(ctx, toc_url)
        chapters: list[dict] = []
        seen: set[str] = set()
        for link in ctx.select(html, 'a[href*="/txt/"]'):
            href = link.get("href", "")
            chapter_url = urljoin(self.base_url, href)
            title = self._s(ctx, link.text_content())
            if not href or not title or chapter_url in seen:
                continue
            seen.add(chapter_url)
            chapters.append(
                {
                    "sourceId": self.id,
                    "index": len(chapters) + 1,
                    "title": title,
                    "chapterUrl": chapter_url,
                    "updateTime": "",
                    "isVip": False,
                    "isLocked": False,
                }
            )
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        """Read one chapter and remove ads and navigation."""
        chapter_url = urljoin(self.base_url, chapter_url)
        html = await self._fetch(ctx, chapter_url)
        title = ctx.text(html, ".txtnav h1")
        content = self._chapter_content(ctx, html, title)
        return {
            "sourceId": self.id,
            "title": self._s(ctx, title),
            "chapterUrl": chapter_url,
            "content": self._body(ctx, content),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_content(self, ctx, html: str, title: str) -> str:
        """Convert the bare-text and BR layout into clean paragraphs."""
        soup = BeautifulSoup(html or "", "html.parser")
        container = soup.select_one(".txtnav")
        if container is None:
            return ""
        for node in container.select("h1, script, style, iframe, a, .txtright, .txtad, .txtcenter"):
            node.decompose()
        for separator in container.select("br"):
            separator.replace_with("\n")
        lines = [ctx.clean_text(line) for line in container.get_text("\n").splitlines()]
        lines = [line for line in lines if line]
        if lines and ctx.clean_text(lines[0]) == ctx.clean_text(title):
            lines.pop(0)
        noise = ("获取最新章节更新，请访问", "獲取最新章節更新，請訪問", "(本章完)", "（还有更新耶）", "（還有更新耶）")
        return "\n\n".join(line for line in lines if not any(marker in line for marker in noise)).strip()
