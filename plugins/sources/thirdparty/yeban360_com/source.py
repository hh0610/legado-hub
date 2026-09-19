"""Plugin for 夜伴书屋 (www.yeban360.com)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    """Adapt the site to the source contract."""

    id = "yeban360_com"
    name = "夜伴书屋"
    contract_version = "1.0"
    last_modified = "2026-07-27"
    base_url = "https://www.yeban360.com"

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        """Fetch a site page through the host-owned HTTP bridge."""
        return await ctx.access.http.fetch_text(urljoin(self.base_url, url), **kwargs)

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search the site's result table."""
        keyword = (keyword or "").strip()
        if page > 1 or not keyword:
            return []
        html = await self._fetch(ctx, "/plus/search.php", params={"q": keyword})
        items = self._parse_search(ctx, html)
        exact = [item for item in items if item.get("name") == keyword]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse one table row per book."""
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for row in soup.select("table tbody tr"):
            link = row.select_one("td a[href^='/book/']")
            if link is None:
                continue
            book_url = self._local_url(link.get("href", ""))
            if book_url in seen:
                continue
            seen.add(book_url)
            # The cell text is wrapped in 《》; the title attribute is the clean name.
            name = ctx.clean_text(link.get("title", "")) or ctx.clean_text(
                link.get_text(" ", strip=True)
            ).strip("《》")
            cells = row.find_all("td")
            author = ctx.clean_text(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else ""
            items.append(
                {
                    "sourceId": self.id,
                    "name": name,
                    "author": author,
                    "bookUrl": book_url,
                    "coverUrl": "",
                    "intro": "",
                    "kind": "",
                    "lastChapter": "",
                    "wordCount": "",
                    "rank": len(items) + 1,
                }
            )
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        """Read the book metadata from the detail page."""
        book_url = self._local_url(book_url)
        html = await self._fetch(ctx, book_url)
        name = self._meta(ctx, html, "og:novel:book_name") or ctx.text(html, "h1")
        author = self._meta(ctx, html, "og:novel:author")
        category = self._meta(ctx, html, "og:novel:category")
        status = self._meta(ctx, html, "og:novel:status")
        cover = self._meta(ctx, html, "og:image")
        intro = ctx.clean_text(self._meta(ctx, html, "og:description"))
        last = self._meta(ctx, html, "og:novel:latest_chapter_name")
        update_time = self._meta(ctx, html, "og:novel:update_time")
        return {
            "sourceId": self.id,
            "name": name,
            "author": author,
            "bookUrl": book_url,
            "coverUrl": self._local_url(cover) if cover else "",
            "intro": intro,
            "kind": " / ".join(part for part in [category, status] if part),
            "lastChapter": last,
            "wordCount": "",
            "updateTime": update_time,
            "tocUrl": book_url,
            "authRequired": False,
        }

    def _meta(self, ctx, html: str, property_name: str) -> str:
        """Return one OpenGraph property value."""
        return ctx.attr(html, f'meta[property="{property_name}"]', "content")

    def _local_url(self, url: str) -> str:
        """Normalize to the verified origin."""
        value = (url or "").strip()
        if value.startswith(("http://www.yeban360.com", "https://www.yeban360.com")):
            value = value.split("www.yeban360.com", 1)[1]
        return urljoin(self.base_url, value)

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Parse the complete catalog block on the book page.

        ``#all-chapter`` is the full reading-order catalog; the block above it is
        a reverse-ordered '最新章节' preview and must not be parsed.
        """
        toc_url = self._local_url(toc_url)
        html = await self._fetch(ctx, toc_url)
        soup = BeautifulSoup(html or "", "html.parser")
        block = soup.select_one("#all-chapter")
        chapters: list[dict] = []
        if block is None:
            return chapters
        seen: set[str] = set()
        for a in block.select("a[href]"):
            href = a.get("href", "").strip()
            if not re.search(r"/book/\d+/\d+\.html", href):
                continue
            chapter_url = self._local_url(href)
            title = ctx.clean_text(a.get("title", "")) or ctx.clean_text(a.get_text(" ", strip=True))
            if not title or chapter_url in seen:
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
        """Read one chapter body, merging the site's same-chapter pagination."""
        chapter_url = self._local_url(chapter_url)
        html = await self._fetch(ctx, chapter_url)
        title = ctx.clean_text(ctx.text(html, "h1.cont-title")) or ctx.clean_text(ctx.text(html, "h1"))
        parts = [self._page_content(ctx, html)]
        for page_url in self._page_urls(html, chapter_url):
            try:
                page_html = await self._fetch(ctx, page_url)
            except Exception as exc:
                ctx.trace("chapter_pagination_error", url=page_url, message=str(exc))
                break
            parts.append(self._page_content(ctx, page_html))
        content = "\n\n".join(part for part in parts if part).strip()
        return {
            "sourceId": self.id,
            "title": self._strip_page_marker(title),
            "chapterUrl": chapter_url,
            "content": content,
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _page_urls(self, html: str, chapter_url: str) -> list[str]:
        """Return the remaining same-chapter page URLs in order."""
        stem = re.sub(r"(_\d+)?\.html$", "", chapter_url)
        soup = BeautifulSoup(html or "", "html.parser")
        urls: list[str] = []
        for a in soup.select(".pagination a[href]"):
            page_url = self._local_url(urljoin(chapter_url, a.get("href", "")))
            if not page_url.startswith(stem + "_"):
                continue
            if page_url not in urls and page_url != chapter_url:
                urls.append(page_url)
        return urls

    def _strip_page_marker(self, title: str) -> str:
        """Drop trailing page markers such as '(第2页)'."""
        return re.sub(r"[（(]?第?\s*\d+\s*[页頁][)）]?\s*$", "", title or "").strip()

    def _page_content(self, ctx, html: str) -> str:
        """Extract clean text from one chapter page."""
        soup = BeautifulSoup(html or "", "html.parser")
        container = soup.select_one("#cont-body")
        if container is None:
            return ""
        for tag in container.find_all(["script", "style", "ins", "iframe", "div"]):
            tag.decompose()
        for br in container.find_all("br"):
            br.replace_with("\n")
        paragraphs = [ctx.clean_text(p.get_text(" ", strip=True)) for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if p and not self._is_noise(p)]
        if not paragraphs:
            text = ctx.clean_text(container.get_text("\n", strip=True))
            paragraphs = [line for line in text.splitlines() if line and not self._is_noise(line)]
        return "\n\n".join(paragraphs)

    def _is_noise(self, line: str) -> bool:
        """Drop site navigation and promo lines."""
        if len(line) > 60:
            return False
        return any(
            marker in line
            for marker in ("夜伴书屋", "最新网址", "加入书签", "回目录", "手机阅读", "TXT下载")
        )
