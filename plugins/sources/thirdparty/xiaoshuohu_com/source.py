"""Xiaoshuohu (小说虎) source plugin.

Site search is unstable; search uses provider fallback.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    id = "xiaoshuohu_com"
    name = "小说虎"
    contract_version = "1.0"
    last_modified = "2026-06-10"
    base_url = "https://www.xiaoshuohu.com"

    async def search(self, ctx, keyword: str, page: int):
        if page > 1:
            return []
        hits = await ctx.access.search_provider(
            keyword,
            target_domain="www.xiaoshuohu.com",
            url_patterns=[r"/\d+/\d+/?$"],
            provider_order=["bing_html", "google_html"],
            query_site_path="/",
            timeout=15,
        )
        items = []
        seen_urls: set[str] = set()
        for hit in hits:
            book_url = hit.url
            if not book_url or book_url in seen_urls:
                continue
            seen_urls.add(book_url)
            items.append({
                "sourceId": self.id,
                "name": re.split(r"[-_|,，:：]", hit.title or "", maxsplit=1)[0].strip() or keyword,
                "author": "",
                "bookUrl": book_url,
                "coverUrl": "",
                "intro": (hit.snippet or "").strip(),
                "kind": "",
                "lastChapter": "",
                "extra": {
                    "searchProvider": "source_access_bridge",
                    "provider": hit.provider,
                    "matchedPattern": hit.matched_pattern,
                    "searchUrl": hit.url,
                },
            })
        if not items:
            html = await ctx.access.http.fetch_text(self.base_url + "/")
            for anchor in ctx.select(html, "a[href]"):
                name = ctx.clean_text(anchor.text_content())
                href = anchor.get("href", "")
                book_url = urljoin(self.base_url, href)
                if name != keyword or not re.search(r"/\d+/\d+/?$", book_url):
                    continue
                items.append({
                    "sourceId": self.id,
                    "name": name,
                    "author": "",
                    "bookUrl": book_url,
                    "coverUrl": "",
                    "intro": "",
                    "kind": "",
                    "lastChapter": "",
                    "extra": {"searchProvider": "homepage"},
                })
                break
        return await enrich_search_items_from_detail(self, ctx, items)

    async def detail(self, ctx, book_url: str):
        html = await ctx.access.http.fetch_text(book_url)
        name = ctx.text(html, ".bookname > h1") or ctx.text(html, "h1") or ""
        author = ctx.text(html, "#info > p:nth-child(1)")
        if "作者：" in author:
            author = author.split("作者：", 1)[1].strip()
        intro = ctx.text(html, "#intro > p") or ""
        cover = ctx.attr(html, "#fmimg > img", "src")
        return {
            "sourceId": self.id,
            "name": name,
            "author": author,
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, cover) if cover else "",
            "intro": intro,
            "tocUrl": book_url,
            "authRequired": False,
            "extra": {},
        }

    async def toc(self, ctx, toc_url: str):
        chapters = []
        seen = set()
        html = await ctx.access.http.fetch_text(toc_url)
        links = ctx.select(html, "#list > dl > dd > a")
        for a in links:
            href = a.get("href", "")
            title = ctx.clean_text(a.text_content())
            if not href or not title or href in seen:
                continue
            seen.add(href)
            chapters.append({
                "sourceId": self.id,
                "index": len(chapters) + 1,
                "title": title,
                "chapterUrl": urljoin(toc_url, href),
                "isVip": False,
                "isLocked": False,
            })
        return chapters

    def _clean_chapter_content(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "iframe", "ins", "center"]):
            tag.decompose()
        for div in soup.find_all("div"):
            text = div.get_text(strip=True)
            if len(text) < 120 and any(kw in text for kw in [
                "广告", "声明", "本章结束", "返回目录", "加入书签", "推荐", "最新网址",
                "章节内容缺失", "章节不存在", "小说虎", "xshbook", "完本神站",
                "一秒记住", "记不住网址", "手机直接访问", "支持", "书友群"
            ]):
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
        html = await ctx.access.http.fetch_text(chapter_url)
        title = ctx.text(html, ".bookname > h1") or ctx.text(html, "h1") or ""
        content_html = ctx.html(html, "#content")
        content = self._clean_chapter_content(content_html)
        return {
            "sourceId": self.id,
            "title": title,
            "content": content,
            "chapterUrl": chapter_url,
            "format": "text",
            "authRequired": False,
            "isPaid": False,
        }
