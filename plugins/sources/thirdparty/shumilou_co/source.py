"""Plugin for 书迷楼 (shumilou.co)."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    """Adapt the independent shumilou.co HTML site to the source contract."""

    id = "shumilou_co"
    name = "书迷楼"
    contract_version = "1.0"
    last_modified = "2026-07-25"
    base_url = "https://www.shumilou.co"
    headers = {"accept-language": "zh-CN,zh;q=0.9"}

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        """Fetch a site page through the browser bridge (Cloudflare JS challenge)."""
        return await ctx.access.browser.fetch_text(
            urljoin(self.base_url, url),
            stage="page",
            wait_ms=2500,
            timeout_ms=45000,
            **kwargs,
        )

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search the verified single-page result endpoint."""
        if page > 1 or not keyword.strip():
            return []
        home = await self._fetch(ctx, "/")
        search_path = ctx.attr(home, 'form[name="search"][action]', "action")
        if not search_path:
            return []
        html = await self._fetch(ctx, f"{search_path}?searchkey={quote(keyword.strip())}")
        items = self._parse_search(ctx, html)
        exact = [item for item in items if item.get("name") == keyword.strip()]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse one result card per book."""
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for card in soup.select(".item"):
            title_node = card.select_one("dt a[href]") or card.select_one(".image a[href]")
            if title_node is None:
                continue
            href = title_node.get("href", "")
            book_url = urljoin(self.base_url, href)
            name = ctx.clean_text(title_node.get("title", "") or title_node.get_text(" ", strip=True))
            if not href or not name or book_url in seen:
                continue
            seen.add(book_url)
            author_node = card.select_one('.btm a[href^="/author/"]')
            intro_node = card.select_one("dd")
            cover_node = card.select_one(".image img")
            cover = cover_node.get("data-original", "") or cover_node.get("src", "") if cover_node else ""
            items.append(
                {
                    "sourceId": self.id,
                    "name": name,
                    "author": ctx.clean_text(author_node.get_text(" ", strip=True) if author_node else ""),
                    "bookUrl": book_url,
                    "coverUrl": urljoin(self.base_url, cover) if cover else "",
                    "intro": ctx.clean_text(intro_node.get_text(" ", strip=True) if intro_node else ""),
                    "kind": "",
                    "lastChapter": "",
                    "wordCount": "",
                    "rank": len(items) + 1,
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        """Read OpenGraph metadata and derive the paginated catalog URL."""
        book_url = urljoin(self.base_url, book_url)
        html = await self._fetch(ctx, book_url)
        name = self._meta(ctx, html, "og:novel:book_name") or ctx.text(html, "#info h1")
        category = self._meta(ctx, html, "og:novel:category")
        status = self._meta(ctx, html, "og:novel:status")
        cover = self._meta(ctx, html, "og:image") or ctx.attr(html, "#fmimg img", "data-original") or ctx.attr(html, "#fmimg img", "src")
        info_text = ctx.text(html, "#info")
        book_id = self._book_id(book_url)
        return {
            "sourceId": self.id,
            "name": ctx.clean_text(name),
            "author": ctx.clean_text(self._meta(ctx, html, "og:novel:author")),
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, cover) if cover else "",
            "intro": ctx.clean_text(ctx.text(html, "#intro")),
            "kind": " / ".join(part for part in [category, status] if part),
            "lastChapter": ctx.clean_text(self._meta(ctx, html, "og:novel:latest_chapter_name")),
            "wordCount": ctx.regex(info_text, r"字数[：:]\s*([^|\s]+(?:\s*万字)?)", default=""),
            "updateTime": self._meta(ctx, html, "og:novel:update_time"),
            "tocUrl": f"{self.base_url}/indexlist/{book_id}/" if book_id else book_url,
            "authRequired": False,
        }

    def _meta(self, ctx, html: str, property_name: str) -> str:
        """Return one OpenGraph property value."""
        return ctx.attr(html, f'meta[property="{property_name}"]', "content")

    def _book_id(self, url: str) -> str:
        """Extract the numeric book identifier."""
        match = re.search(r"/(\d+)/?$", url or "")
        return match.group(1) if match else ""

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Follow the catalog's explicit next-page links in reading order."""
        chapters: list[dict] = []
        seen_chapters: set[str] = set()
        seen_pages: set[str] = set()
        page_url = urljoin(self.base_url, toc_url)
        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            html = await self._fetch(ctx, page_url)
            for link in ctx.select(html, 'a[rel="chapter"]'):
                href = link.get("href", "")
                chapter_url = urljoin(page_url, href)
                title = ctx.clean_text(link.text_content())
                if not href or not title or chapter_url in seen_chapters:
                    continue
                seen_chapters.add(chapter_url)
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
            next_href = self._next_href(ctx, html, "下一页")
            page_url = urljoin(page_url, next_href) if next_href else ""
        return chapters

    def _next_href(self, ctx, html: str, label: str) -> str:
        """Return the first usable navigation link matching its visible label."""
        for link in ctx.select(html, "a[href]"):
            href = link.get("href", "")
            if ctx.clean_text(link.text_content()) == label and href and not href.lower().startswith("javascript:"):
                return href
        return ""

    async def chapter(self, ctx, chapter_url: str) -> dict:
        """Combine all pages belonging to one logical chapter."""
        chapter_url = urljoin(self.base_url, chapter_url)
        chapter_id = self._chapter_id(chapter_url)
        page_url = chapter_url
        seen_pages: set[str] = set()
        parts: list[str] = []
        title = ""
        while page_url and page_url not in seen_pages and len(seen_pages) < 10:
            seen_pages.add(page_url)
            html = await self._fetch(ctx, page_url)
            if not title:
                title = ctx.text(html, "h1.bookname")
            content = self._chapter_content(ctx, html)
            if content:
                parts.append(content)
            next_href = ctx.attr(html, 'a[rel="next"]', "href")
            next_url = urljoin(page_url, next_href) if next_href else ""
            if self._chapter_id(next_url) != chapter_id:
                break
            page_url = next_url
        return {
            "sourceId": self.id,
            "title": re.sub(r"[（(]\d+/\d+(?:页)?[）)]", "", title).strip(),
            "chapterUrl": chapter_url,
            "content": "\n\n".join(parts).strip(),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_id(self, url: str) -> str:
        """Return the stable chapter ID shared by all of its page URLs."""
        match = re.search(r"/(\d+)(?:_\d+)?\.html$", url or "")
        return match.group(1) if match else ""

    def _chapter_content(self, ctx, html: str) -> str:
        """Extract paragraph text and drop the page continuation notice."""
        soup = BeautifulSoup(ctx.html(html, "#content #booktxt") or "", "html.parser")
        lines = [ctx.clean_text(node.get_text(" ", strip=True)) for node in soup.select("p")]
        blocked = {"本章未完，点击下一页继续阅读。"}
        return "\n\n".join(line for line in lines if line and line not in blocked)
