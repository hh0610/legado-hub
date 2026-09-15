"""Plugin for 新御书屋 (qianyew.com; formerly qianyezw.com)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    id = "qianyezw_com"
    name = "新御书屋"
    contract_version = "1.0"
    last_modified = "2026-07-30"
    base_url = "https://www.qianyew.com"

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        # The site fronts every request with a browser-fingerprint challenge,
        # so pages must be rendered through the access bridge.
        return await ctx.access.browser.fetch_text(
            urljoin(self.base_url, url),
            stage="page",
            wait_ms=2500,
            timeout_ms=45000,
            **kwargs,
        )

    def _url(self, value: str) -> str:
        return urljoin(self.base_url, (value or "").strip())

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        keyword = keyword.strip()
        if page > 1 or not keyword:
            return []
        html = await self._fetch(
            ctx,
            "/search/",
            method="POST",
            data={"searchkey": keyword, "action": "login"},
            headers={"Referer": f"{self.base_url}/search/"},
        )
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[dict] = []
        seen: set[str] = set()
        for box in soup.select(".bookbox"):
            link = box.select_one(".bookname a[href]")
            if link is None:
                continue
            name = ctx.clean_text(link.get_text(" ", strip=True))
            book_url = self._url(link.get("href", ""))
            if not name or book_url in seen:
                continue
            seen.add(book_url)
            author_node = box.select_one(".author")
            author = ctx.clean_text(author_node.get_text(" ", strip=True)) if author_node else ""
            latest = box.select_one(".cat a")
            intro = box.select_one(".update")
            items.append({
                "sourceId": self.id,
                "name": name,
                "author": re.sub(r"^作者[：:]", "", author).strip(),
                "bookUrl": book_url,
                "coverUrl": "",
                "intro": re.sub(r"^简介[：:]", "", ctx.clean_text(intro.get_text(" ", strip=True)) if intro else "").strip(),
                "kind": "",
                "lastChapter": ctx.clean_text(latest.get_text(" ", strip=True)) if latest else "",
                "wordCount": "",
                "updateTime": "",
                "rank": len(items) + 1,
            })
        exact = [item for item in items if item["name"] == keyword]
        return await enrich_search_items_from_detail(self, ctx, exact or items)

    def _meta(self, ctx, html: str, name: str) -> str:
        return ctx.attr(html, f'meta[property="{name}"]', "content")

    async def detail(self, ctx, book_url: str) -> dict:
        book_url = self._url(book_url)
        html = await self._fetch(ctx, book_url)
        kind = self._meta(ctx, html, "og:novel:category")
        status = self._meta(ctx, html, "og:novel:status")
        last = (
            self._meta(ctx, html, "og:novel:latest_chapter_name")
            or self._meta(ctx, html, "og:novel:lastest_chapter_name")
        )
        return {
            "sourceId": self.id,
            "name": self._meta(ctx, html, "og:novel:book_name") or ctx.text(html, "h1.booktitle"),
            "author": self._meta(ctx, html, "og:novel:author"),
            "bookUrl": book_url,
            "coverUrl": self._url(self._meta(ctx, html, "og:image")) if self._meta(ctx, html, "og:image") else "",
            "intro": self._meta(ctx, html, "og:description") or ctx.text(html, ".bookintro"),
            "kind": " / ".join(value for value in (kind, status) if value),
            "lastChapter": last,
            "wordCount": ctx.text(html, ".booktag .blue"),
            "updateTime": self._meta(ctx, html, "og:novel:update_time"),
            "tocUrl": book_url,
            "authRequired": False,
            "extra": {"status": status},
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        toc_url = self._url(toc_url)
        html = await self._fetch(ctx, toc_url)
        soup = BeautifulSoup(html or "", "html.parser")
        links = soup.select("#list-chapterAll dd a[href]")
        if not links:
            links = soup.select(".chapterlist dd a[href]")
        chapters: list[dict] = []
        seen: set[str] = set()
        for link in links:
            chapter_url = self._url(link.get("href", ""))
            title = ctx.clean_text(link.get_text(" ", strip=True))
            if not title or chapter_url in seen:
                continue
            seen.add(chapter_url)
            chapters.append({
                "sourceId": self.id,
                "index": len(chapters) + 1,
                "title": title,
                "chapterUrl": chapter_url,
                "updateTime": "",
                "isVip": False,
                "isLocked": False,
            })
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        chapter_url = self._url(chapter_url)
        stem = self._chapter_stem(chapter_url)
        current_url = chapter_url
        seen: set[str] = set()
        parts: list[str] = []
        title = ""
        while current_url and current_url not in seen and len(seen) < 10:
            seen.add(current_url)
            html = await self._fetch(ctx, current_url)
            soup = BeautifulSoup(html or "", "html.parser")
            if not title:
                node = soup.select_one("h1.pt10") or soup.select_one("h1")
                title = ctx.clean_text(node.get_text(" ", strip=True)) if node else ""
                title = re.sub(r"[（(]\s*\d+\s*/\s*\d+\s*[）)]\s*$", "", title).strip()
            content = self._chapter_page(ctx, soup)
            if content:
                parts.append(content)
            next_link = soup.select_one("#linkNext[href]")
            next_url = self._url(next_link.get("href", "")) if next_link else ""
            if not next_url or self._chapter_stem(next_url) != stem:
                break
            current_url = next_url
        return {
            "sourceId": self.id,
            "title": title,
            "chapterUrl": chapter_url,
            "content": "\n\n".join(parts).strip(),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_page(self, ctx, soup: BeautifulSoup) -> str:
        container = soup.select_one("#rtext")
        if container is None:
            return ""
        for tag in container.select("script, style, ins, center, a"):
            tag.decompose()
        leaves = [node for node in container.find_all("p") if node.find("p") is None]
        paragraphs = [ctx.clean_text(node.get_text(" ", strip=True)) for node in leaves]
        paragraphs = [text for text in paragraphs if text]
        if paragraphs:
            return "\n\n".join(paragraphs)
        for br in container.find_all("br"):
            br.replace_with("\n")
        return "\n\n".join(
            line for line in (ctx.clean_text(raw) for raw in container.get_text("\n").splitlines()) if line
        )

    def _chapter_stem(self, url: str) -> str:
        match = re.search(r"/read/(\d+)/(\d+)(?:_\d+)?\.html", url or "")
        return f"{match.group(1)}/{match.group(2)}" if match else ""
