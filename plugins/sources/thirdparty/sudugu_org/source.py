"""Sudugu (速读谷) source plugin.

Proxy required, rate-limited, concurrency=1 recommended.
"""

import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup
from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    id = "sudugu_org"
    name = "速读谷"
    contract_version = "1.0"
    last_modified = "2026-07-31"
    base_url = "https://www.sudugu.org"
    backup_url = "https://www.sudugu.co"

    async def search(self, ctx, keyword: str, page: int):
        items = []
        search_error = None
        try:
            html = await ctx.access.http.fetch_text(
                f"{self.base_url}/i/sor.aspx",
                params={"key": keyword},
            )
            results = ctx.select(html, ".container > div")
            for div in results:
                name_a = ctx.select(div, "h3 > a")
                if not name_a:
                    continue
                name = ctx.clean_text(name_a[0].text_content())
                href = name_a[0].get("href", "")
                author = ctx.clean_text(ctx.text(div, "p:nth-child(3)"))
                author = re.sub(r"^作者[：:]\s*", "", author)
                latest = ctx.clean_text(ctx.text(div, "ul > li:nth-child(1) > a"))
                update_time = ctx.clean_text(ctx.text(div, "ul > li:nth-child(1) > i"))
                items.append({
                    "sourceId": self.id,
                    "name": name,
                    "author": author,
                    "bookUrl": urljoin(self.base_url, href),
                    "lastChapter": latest,
                    "updateTime": update_time,
                })
        except Exception as exc:
            search_error = exc
            ctx.trace("search_error", url=f"{self.base_url}/i/sor.aspx", message=str(exc))
        if not items:
            items = await self._search_backup(ctx, keyword, page)
        if not items:
            items = await self._search_from_explore(ctx, keyword)
        if items:
            return await enrich_search_items_from_detail(self, ctx, items)
        if search_error is not None:
            raise search_error
        return []

    async def _search_backup(self, ctx, keyword: str, page: int) -> list[dict]:
        if page > 1:
            return []
        try:
            html = await ctx.access.http.fetch_text(
                f"{self.backup_url}/modules/article/search.php",
                params={"searchkey": keyword},
            )
        except Exception as exc:
            ctx.trace("search_backup_error", url=self.backup_url, message=str(exc))
            return []

        items = []
        for box in ctx.select(html, ".bookbox"):
            anchors = ctx.select(box, ".bookname > a[href]")
            if not anchors:
                continue
            anchor = anchors[0]
            author = re.sub(r"^作者[：:]\s*", "", ctx.text(box, ".author"))
            intro = re.sub(r"^简介[：:]\s*", "", ctx.text(box, ".update"))
            items.append({
                "sourceId": self.id,
                "name": ctx.text(box, ".bookname > a"),
                "author": author,
                "bookUrl": urldefrag(urljoin(self.backup_url, anchor.get("href", "")))[0],
                "intro": intro,
                "lastChapter": ctx.text(box, ".cat > a"),
            })
        if items:
            return items

        anchors = ctx.select(html, ".item h1 > a[href]")
        if not anchors:
            return []
        anchor = anchors[0]
        author = ctx.text(html, ".itemtxt > p:nth-of-type(2) > a")
        return [{
            "sourceId": self.id,
            "name": ctx.text(html, ".item h1 > a"),
            "author": re.sub(r"^作者[：:]\s*", "", author),
            "bookUrl": urldefrag(urljoin(self.backup_url, anchor.get("href", "")))[0],
            "lastChapter": ctx.text(html, ".itemtxt > ul > li:first-child > a"),
        }]

    async def _search_from_explore(self, ctx, keyword: str) -> list[dict]:
        items = []
        try:
            html = await ctx.access.http.fetch_text(f"{self.base_url}/topallvisit/1.html")
            links = ctx.select(html, ".container > div h3 > a")
            seen = set()
            for a in links:
                href = a.get("href", "")
                name = ctx.clean_text(a.text_content())
                if not href or not name or name in seen:
                    continue
                if keyword.lower() in name.lower():
                    seen.add(name)
                    items.append({
                        "sourceId": self.id,
                        "name": name,
                        "bookUrl": urljoin(self.base_url, href),
                    })
        except Exception as exc:
            ctx.trace("search_explore_fallback_error", url=f"{self.base_url}/topallvisit/1.html", message=str(exc))
        return items

    async def detail(self, ctx, book_url: str):
        html = await ctx.access.http.fetch_text(book_url)
        name = ctx.text(html, ".item > div > h1 > a") or ctx.text(html, "h1") or ""
        author = (
            ctx.text(html, ".item > div > p:nth-of-type(3) > a")
            or ctx.text(html, ".item > div > p:nth-of-type(2) > a")
        )
        author = re.sub(r"^作者[：:]\s*", "", author)
        intro = ctx.text(html, ".des") or ""
        cover = ctx.attr(html, ".item > a > img", "src")
        latest = ctx.text(html, ".item > div > ul > li:nth-child(1) > a")
        return {
            "sourceId": self.id,
            "name": name,
            "author": author,
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, cover) if cover else "",
            "intro": intro,
            "lastChapter": latest,
            "tocUrl": book_url,
            "authRequired": False,
            "extra": {},
        }

    async def toc(self, ctx, toc_url: str):
        chapters = []
        seen = set()
        seen_pages = set()
        last_title = ""
        page_url = toc_url
        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            html = await ctx.access.http.fetch_text(page_url)
            links = ctx.select(html, "#list > ul > li > a")
            new_count = 0
            for a in links:
                href = a.get("href", "")
                title = ctx.clean_text(a.text_content())
                if not href or not title or href in seen:
                    continue
                if ".sudugu.co/" in page_url and title == last_title:
                    continue
                seen.add(href)
                last_title = title
                chapters.append({
                    "sourceId": self.id,
                    "index": len(chapters) + 1,
                    "title": title,
                    "chapterUrl": urljoin(page_url, href),
                    "isVip": False,
                    "isLocked": False,
                })
                new_count += 1
            next_href = ctx.attr(html, "#pages > .gr:last-child", "href")
            if not next_href or new_count == 0:
                break
            next_url = urljoin(page_url, next_href)
            if next_url == page_url:
                break
            page_url = next_url
        return chapters

    def _chapter_stem(self, url: str) -> str:
        path = url.split("?")[0].split("#")[0]
        stem = path.rsplit(".", 1)[0] if "." in path else path
        return re.sub(r"[-_]\d+$", "", stem)

    def _bump_chapter_id(self, url: str) -> str:
        """Return the page with the chapter id incremented by one."""
        m = re.search(r"(\d+)(\.html)", url.split("?")[0].split("#")[0])
        if not m:
            return ""
        return url[: m.start(1)] + str(int(m.group(1)) + 1) + url[m.end(1):]

    def _clean_chapter_content(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "iframe", "ins", "center"]):
            tag.decompose()
        for div in soup.find_all("div"):
            text = div.get_text(strip=True)
            if len(text) < 80 and any(kw in text for kw in ["广告", "声明", "本章结束", "返回目录", "加入书签", "推荐", "最新网址", "章节内容缺失", "章节不存在", "速读谷", "sudugu"]):
                div.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)
        if paragraphs:
            return "\n\n".join(paragraphs)
        text = soup.get_text("\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n\n".join(lines)

    async def chapter(self, ctx, chapter_url: str):
        parts = []
        current_url = chapter_url
        visited_urls = set()
        title = ""
        original_stem = self._chapter_stem(chapter_url)
        while current_url and len(parts) < 10:
            visited_urls.add(current_url)
            html = await ctx.access.http.fetch_text(current_url)
            if not title:
                title = ctx.text(html, ".submenu > h1") or ctx.text(html, "h1")
            content_html = ctx.html(html, ".con") or ctx.html(html, "#content")
            content = self._clean_chapter_content(content_html)
            if not content:
                # Some chapter ids render as empty interstitials; the real body
                # sits on the page with the id bumped by one.
                bumped_url = self._bump_chapter_id(current_url)
                if bumped_url and bumped_url not in visited_urls:
                    visited_urls.add(bumped_url)
                    html = await ctx.access.http.fetch_text(bumped_url)
                    content_html = ctx.html(html, ".con") or ctx.html(html, "#content")
                    content = self._clean_chapter_content(content_html)
            if content:
                parts.append(content)
            next_url = ""
            for anchor in ctx.select(html, ".prenext a"):
                href = str(anchor.get("href", "") or "").strip()
                candidate_url = urljoin(current_url, href)
                if (
                    not href
                    or href == "javascript:void(0);"
                    or candidate_url in visited_urls
                    or self._chapter_stem(candidate_url) != original_stem
                ):
                    continue
                next_url = candidate_url
                break
            current_url = next_url
        title = (title or "").rsplit(">", 1)[-1].strip()
        title = re.sub(r"[（(][\d/]+[）)]", "", title).strip()
        full_content = "\n\n".join(parts)
        return {
            "sourceId": self.id,
            "title": title,
            "content": full_content,
            "chapterUrl": chapter_url,
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }
