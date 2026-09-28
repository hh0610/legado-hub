"""Plugin for 小說狂人 (czbooks.net).

Traditional Chinese site behind Cloudflare: plain HTTP is answered with a
challenge, so every stage goes through ``ctx.access.stealth``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import DEFAULT_ENRICH_FIELDS, enrich_search_items_from_detail

# This site's chapter body is a flat run of <br /> separated lines. Python's
# html.parser mis-nests those tags here (every line after the first <br>
# becomes its child) and the body collapses to one paragraph, so parse with lxml.
PARSER = "lxml"


class Source:
    """Adapt the site to the source contract."""

    id = "czbooks_net"
    name = "小说狂人"
    contract_version = "1.0"
    last_modified = "2026-07-31"
    base_url = "https://czbooks.net"
    headers = {"accept-language": "zh-TW,zh;q=0.9"}

    def _s(self, ctx, value: str) -> str:
        """Normalize user-facing Traditional Chinese text."""
        return ctx.to_simplified(ctx.clean_text(value or ""))

    async def _fetch(self, ctx, url: str, **kwargs) -> str:
        """Fetch a site page through the browser bridge (Cloudflare JS challenge)."""
        return await ctx.access.browser.fetch_text(
            urljoin(self.base_url, url),
            stage="page",
            wait_ms=4000,
            timeout_ms=45000,
            **kwargs,
        )

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """Search the site's keyword path.

        The site's own script builds ``/s/<keyword>?q=<keyword>``; the bare
        ``/s?q=`` form returns 404.
        """
        keyword = (keyword or "").strip()
        if page > 1 or not keyword:
            return []
        query = ctx.to_traditional(keyword)
        html = await self._fetch(ctx, f"/s/{quote(query)}?q={quote(query)}")
        items = self._parse_search(ctx, html)
        exact = [item for item in items if item.get("name") == keyword]
        return await enrich_search_items_from_detail(
            self,
            ctx,
            exact or items,
            fields=DEFAULT_ENRICH_FIELDS + ("bookStatus", "chapterCount"),
        )

    def _parse_search(self, ctx, html: str) -> list[dict]:
        """Parse one novel card per search result."""
        soup = BeautifulSoup(html or "", PARSER)
        items: list[dict] = []
        seen: set[str] = set()
        for card in soup.select(".novel-item"):
            link = card.select_one(".novel-item-cover-wrapper a[href]")
            if link is None:
                continue
            book_url = self._local_url(link.get("href", ""))
            if not re.search(r"/n/[a-z0-9]+$", book_url) or book_url in seen:
                continue
            seen.add(book_url)
            cover_node = card.select_one(".novel-item-thumbnail img[src]")
            status = self._s(ctx, self._text(card, ".novel-item-state"))
            items.append(
                {
                    "sourceId": self.id,
                    "name": self._s(ctx, self._text(card, ".novel-item-title")),
                    "author": self._s(ctx, self._text(card, ".novel-item-author a")),
                    "bookUrl": book_url,
                    "tocUrl": book_url,
                    "coverUrl": (cover_node.get("src", "").strip() if cover_node else ""),
                    "intro": "",
                    "kind": status,
                    "bookStatus": status,
                    "lastChapter": self._s(ctx, self._text(card, ".novel-item-newest-chapter a")),
                    "wordCount": "",
                    "updateTime": self._text(card, ".novel-item-date"),
                    "chapterCount": 0,
                    "rank": len(items) + 1,
                }
            )
        return items

    def _text(self, node, selector: str) -> str:
        """Return the text of the first matching child, or an empty string."""
        found = node.select_one(selector)
        return found.get_text(" ", strip=True) if found is not None else ""

    async def detail(self, ctx, book_url: str) -> dict:
        """Read the book metadata from the detail page."""
        book_url = self._local_url(book_url)
        html = await self._fetch(ctx, book_url)
        soup = BeautifulSoup(html or "", PARSER)
        info = self._info_table(soup)
        cover_node = soup.select_one(".novel-detail .thumbnail img[src], .thumbnail img[src]")
        description = soup.select_one(".description")
        if description is not None:
            for br in description.find_all("br"):
                br.replace_with("\n")
        status = self._s(ctx, info.get("连载状态", ""))
        chapter_links = []
        seen_chapter_urls: set[str] = set()
        for link in soup.select("#chapter-list li a[href]"):
            chapter_url = self._local_url(link.get("href", ""))
            if not re.search(r"/n/[a-z0-9]+/[a-z0-9]+", chapter_url) or chapter_url in seen_chapter_urls:
                continue
            seen_chapter_urls.add(chapter_url)
            chapter_links.append(link)
        return {
            "sourceId": self.id,
            # The title is rendered as 《书名》.
            "name": self._s(ctx, self._text(soup, ".novel-info .title").strip("《》")),
            "author": self._s(ctx, self._text(soup, ".novel-info .author a")),
            "bookUrl": book_url,
            "coverUrl": (cover_node.get("src", "").strip() if cover_node else ""),
            "intro": self._s(ctx, description.get_text("\n", strip=True)) if description else "",
            "kind": " / ".join(
                part for part in [self._s(ctx, info.get("分类", "")), status] if part
            ),
            "bookStatus": status,
            "lastChapter": self._s(ctx, chapter_links[-1].get_text(" ", strip=True)) if chapter_links else "",
            "wordCount": "",
            "updateTime": info.get("更新时间", ""),
            "tocUrl": book_url,
            "chapterCount": len(chapter_links),
            "authRequired": False,
        }

    def _info_table(self, soup) -> dict[str, str]:
        """Read the 连载状态 / 更新时间 / 分类 table, keyed by simplified labels."""
        info: dict[str, str] = {}
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) != 2:
                continue
            # Labels are padded for alignment, e.g. '分  類'.
            key = re.sub(r"\s+", "", cells[0].get_text(" ", strip=True))
            key = key.replace("連載狀態", "连载状态").replace("更新時間", "更新时间").replace("分類", "分类")
            info[key] = cells[1].get_text(" ", strip=True)
        return info

    def _local_url(self, url: str) -> str:
        """Normalize protocol-relative and absolute links to the verified origin."""
        value = (url or "").strip()
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith(("http://czbooks.net", "https://czbooks.net")):
            value = value.split("czbooks.net", 1)[1]
        return urljoin(self.base_url, value)

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """Parse the single-page complete catalog on the book page."""
        toc_url = self._local_url(toc_url)
        html = await self._fetch(ctx, toc_url)
        soup = BeautifulSoup(html or "", PARSER)
        block = soup.select_one("#chapter-list")
        chapters: list[dict] = []
        if block is None:
            return chapters
        seen: set[str] = set()
        for a in block.select("li a[href]"):
            chapter_url = self._local_url(a.get("href", ""))
            if not re.search(r"/n/[a-z0-9]+/[a-z0-9]+", chapter_url) or chapter_url in seen:
                continue
            title = self._s(ctx, a.get_text(" ", strip=True))
            if not title:
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
        soup = BeautifulSoup(html or "", PARSER)
        # The heading is rendered as 《书名》第1章 标题.
        heading = self._text(soup, ".chapter-detail .name")
        title = self._s(ctx, re.sub(r"^《[^》]*》", "", heading).strip())
        return {
            "sourceId": self.id,
            "title": title,
            "chapterUrl": chapter_url,
            "content": self._chapter_content(ctx, soup, title),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }

    def _chapter_content(self, ctx, soup, title: str) -> str:
        """Convert the <br>-separated body into clean paragraphs."""
        container = soup.select_one(".chapter-detail .content")
        if container is None:
            return ""
        for tag in container.find_all(["script", "style", "ins", "iframe"]):
            tag.decompose()
        for br in container.find_all("br"):
            br.replace_with("\n")
        lines = [self._s(ctx, line) for line in container.get_text("\n").splitlines()]
        paragraphs = [line for line in lines if line and not self._is_noise(line)]
        # The site repeats the chapter title as the first line.
        if paragraphs and title and paragraphs[0] == title:
            paragraphs.pop(0)
        return "\n\n".join(paragraphs).strip()

    def _is_noise(self, line: str) -> bool:
        """Drop end-of-chapter markers and site navigation."""
        if len(line) > 40:
            return False
        return any(
            marker in line
            for marker in ("(本章完)", "（本章完）", "小说狂人", "小說狂人", "上一章", "下一章", "回報錯誤")
        )
