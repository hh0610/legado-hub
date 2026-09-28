"""Plugin for 宙斯小说网 (tw.zhswx.com)."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    """Adapt the Traditional Chinese site to the source contract."""

    id = "zhswx_tw"
    name = "宙斯小说网"
    contract_version = "1.0"
    last_modified = "2026-07-30"
    base_url = "https://tw.zhswx.com"

    def _s(self, ctx, value: str) -> str:
        """Normalize user-facing Traditional Chinese text."""
        return ctx.to_simplified(ctx.clean_text(value or ""))

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        """Fetch a site page through the browser bridge (fingerprint-gated HTML)."""
        return await ctx.access.browser.fetch_text(
            urljoin(self.base_url, url),
            stage="page",
            wait_ms=4000,
            timeout_ms=45000,
            **kwargs,
        )

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search the site's list endpoint."""
        if page > 1 or not keyword.strip():
            return []
        html = await self._fetch(ctx, f"/list/{quote(keyword.strip())}.html")
        items = self._parse_search(ctx, html)
        exact = [item for item in items if item.get("name") == keyword.strip()]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse search result links."""
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            title = (a.get("title") or a.get_text(" ", strip=True)).strip()
            if not title or not re.search(r"/book/\d+\.html", href):
                continue
            book_url = self._local_url(href)
            if book_url in seen:
                continue
            seen.add(book_url)
            # Title may be "书名 作者" from title attribute
            name = title
            author = ""
            if " " in title:
                name, author = title.rsplit(" ", 1)
            items.append(
                {
                    "sourceId": self.id,
                    "name": self._s(ctx, name),
                    "author": self._s(ctx, author),
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
        name = self._s(ctx, ctx.text(html, "h1"))
        author = self._s(ctx, self._meta(ctx, html, "og:novel:author"))
        cover = self._meta(ctx, html, "og:image") or ctx.attr(html, "#fmimg img", "src")
        intro = self._s(ctx, self._meta(ctx, html, "og:description"))
        kind = self._s(ctx, self._meta(ctx, html, "og:novel:category"))
        status = self._s(ctx, self._meta(ctx, html, "og:novel:status"))
        last = self._s(ctx, self._meta(ctx, html, "og:novel:latest_chapter_name"))
        update_time = self._meta(ctx, html, "og:novel:update_time")
        # Fallback author from meta keywords when og tag is missing
        if not author:
            author = self._s(ctx, self._extract_author_from_keywords(ctx, html, name))
        m = re.search(r"/book/(\d+)\.html", book_url)
        toc_url = f"/chapter/{m.group(1)}.html" if m else book_url
        return {
            "sourceId": self.id,
            "name": name,
            "author": author,
            "bookUrl": book_url,
            "coverUrl": self._local_url(cover) if cover else "",
            "intro": intro,
            "kind": kind or status,
            "lastChapter": last,
            "wordCount": "",
            "updateTime": update_time,
            "tocUrl": self._local_url(toc_url),
            "authRequired": False,
        }

    def _meta(self, ctx, html: str, property_name: str) -> str:
        """Return one OpenGraph property value."""
        return ctx.attr(html, f'meta[property="{property_name}"]', "content")

    def _extract_author_from_keywords(self, ctx, html: str, book_name: str) -> str:
        """Fallback: parse author from meta keywords or page title."""
        keywords = ctx.attr(html, 'meta[name="Keywords"]', "content") or ctx.attr(html, 'meta[name="keywords"]', "content")
        if keywords and book_name:
            prefix = keywords.split("|")[0]
            # Compare simplified forms so Traditional names match
            simple_prefix = ctx.to_simplified(prefix)
            simple_name = ctx.to_simplified(book_name)
            if simple_name in simple_prefix:
                rest = simple_prefix.split(simple_name, 1)[1].lstrip("- ")
                return rest.split(" ")[0].split("|")[0]
        # Fallback: page title "作者 书名txt下载,..."
        title = ctx.text(html, "title") or ""
        simple_title = ctx.to_simplified(title)
        simple_name = ctx.to_simplified(book_name)
        if simple_name and simple_name in simple_title:
            before = simple_title.split(simple_name, 1)[0].strip()
            return before.rstrip(" ").split(" ")[-1] if before else ""
        return ""

    def _local_url(self, url: str) -> str:
        """Normalize to the verified origin."""
        value = (url or "").strip()
        if value.startswith(("http://tw.zhswx.com", "https://tw.zhswx.com")):
            value = value.split("tw.zhswx.com", 1)[1]
        return urljoin(self.base_url, value)

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Parse the single-page complete catalog."""
        toc_url = self._local_url(toc_url)
        html = await self._fetch(ctx, toc_url)
        soup = BeautifulSoup(html or "", "html.parser")
        chapters: list[dict] = []
        seen: set[str] = set()
        book_id_match = re.search(r"/chapter/(\d+)\.html", toc_url)
        book_id = book_id_match.group(1) if book_id_match else ""
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not re.search(rf"/read/{book_id}_\d+\.html", href):
                continue
            title = self._s(ctx, a.get_text(" ", strip=True))
            chapter_url = self._local_url(href)
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
        """Read one chapter body."""
        chapter_url = self._local_url(chapter_url)
        html = await self._fetch(ctx, chapter_url)
        title = self._s(ctx, ctx.text(html, "h1"))
        content = self._chapter_content(ctx, html, title)
        return {
            "sourceId": self.id,
            "title": title,
            "chapterUrl": chapter_url,
            "content": content,
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_content(self, ctx, html: str, title: str) -> str:
        """Convert the paragraph layout into clean plain text."""
        soup = BeautifulSoup(html or "", "html.parser")
        # The chapter body is the first large text div after the title/center info.
        container = None
        for div in soup.find_all("div"):
            style = div.get("style", "")
            if "font-size" in style and "line-height" in style and div.find("br"):
                container = div
                break
        if container is None:
            return ""
        for tag in container.find_all(["script", "style", "ins", "center"]):
            tag.decompose()
        for br in container.find_all("br"):
            br.replace_with("\n")
        paragraphs = [self._s(ctx, p.get_text(" ", strip=True)) for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            paragraphs = [
                self._s(ctx, line)
                for line in container.get_text("\n", strip=True).splitlines()
                if ctx.clean_text(line)
            ]
        # Drop title line if it appears at the start
        if paragraphs and title and title in paragraphs[0]:
            paragraphs.pop(0)
        return "\n\n".join(paragraphs).strip()
