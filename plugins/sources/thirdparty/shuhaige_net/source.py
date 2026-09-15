"""Plugin for 书海阁小说网 (shuhaige.net) based on so-novel seed."""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.source_plugins.search_enrichment import enrich_search_items_from_detail


class Source:
    id = "shuhaige_net"
    name = "书海阁小说网"
    contract_version = "1.0"
    last_modified = "2026-06-10"
    base_url = "https://www.shuhaige.net"
    explore_url = "https://m.shuhaige.net"
    headers = {}
    explore_defs = [
        {"groupId": "allvisit", "title": "总点击榜", "url": "/allvisit/{page}.html", "kind": "rank"},
        {"groupId": "monthvisit", "title": "月点击榜", "url": "/monthvisit/{page}.html", "kind": "rank"},
        {"groupId": "weekvisit", "title": "周点击榜", "url": "/weekvisit/{page}.html", "kind": "rank"},
        {"groupId": "goodnew", "title": "新书榜单", "url": "/goodnew/{page}.html", "kind": "new"},
        {"groupId": "shuku_all", "title": "书库全部", "url": "/shuku/0_0_0_{page}.html", "kind": "category"},
    ]

    async def explore_groups(self, ctx):
        return [
            {
                "sourceId": self.id,
                "groupId": item["groupId"],
                "title": item["title"],
                "url": urljoin(self.explore_url, item["url"].replace("{page}", "1")),
                "kind": item["kind"],
                "pageable": True,
                "profile": "mobile_net",
            }
            for item in self.explore_defs
        ]

    async def explore(self, ctx, group_id: str | None = None, page: int = 1):
        group = next((item for item in self.explore_defs if item["groupId"] == group_id), self.explore_defs[0])
        url = urljoin(self.explore_url, group["url"].replace("{page}", str(page)))
        html = await ctx.access.http.fetch_text(url, headers=self.headers)
        rows = ctx.select(html, ".list li")
        items = []
        for index, row in enumerate(rows, start=1):
            links = ctx.select(row, "a")
            if len(links) < 2:
                continue
            book_link = links[1]
            name = ctx.clean_text(book_link.text_content())
            href = book_link.get("href", "")
            author = ctx.clean_text(links[2].text_content()) if len(links) > 2 else ""
            text = ctx.clean_text(row.text_content())
            latest = ctx.text(row, ".s5") or ctx.regex(text, r"最新：(.+)$", default="")
            intro = text
            if latest:
                intro = intro.replace(f"最新：{latest}", "").strip()
            if name and intro.startswith(name):
                intro = intro[len(name):].strip()
            items.append({
                "sourceId": self.id,
                "name": name,
                "author": author,
                "bookUrl": urljoin(self.explore_url, href),
                "intro": intro,
                "lastChapter": latest,
                "kind": group["title"],
                "groupId": group["groupId"],
                "groupTitle": group["title"],
                "rank": index,
            })
        return items

    async def search(self, ctx, keyword: str, page: int):
        items = []
        # Primary domain is www.shuhaige.net; keep mobile fallbacks for resilience.
        attempts = [
            # The first POST of a fresh session lands on a "错误提示" page; a
            # second POST (by then the site has issued its cookie) succeeds.
            ("https://www.shuhaige.net", "https://www.shuhaige.net/search.html", "POST", {"searchkey": keyword, "searchtype": "all"}, {"referer": "https://www.shuhaige.net/"}, "#sitembox > dl"),
            ("https://www.shuhaige.net", "https://www.shuhaige.net/search.html", "POST", {"searchkey": keyword, "searchtype": "all"}, {"referer": "https://www.shuhaige.net/"}, "#sitembox > dl"),
            ("https://www.shuhaige.net", "https://www.shuhaige.net/search.html", "GET", {"keyword": keyword}, {}, "#sitembox > dl"),
            ("https://m.shuhaige.net", "https://m.shuhaige.net/search.html", "POST", {"searchkey": keyword}, self.headers, "#sitembox > dl, .bookinfo a"),
            (self.base_url, f"{self.base_url}/search.html", "GET", {"keyword": keyword}, self.headers, ".bookinfo a, .list-item a, a[href*=\"/book/\"]"),
        ]
        for base_for_join, url, method, data, hdrs, selector in attempts:
            try:
                if method == "GET":
                    html = await ctx.access.http.fetch_text(url, params=data, headers=hdrs)
                else:
                    html = await ctx.access.http.fetch_text(url, method="POST", data=data, headers=hdrs)
                items = self._parse_search_rows(ctx, html, base_for_join, selector)
                matched = [item for item in items if keyword in item.get("name", "")]
                if matched:
                    items = matched
                    break
                items = []
            except Exception:
                continue
        if not items:
            items = await self._search_from_explore(ctx, keyword)
        return await enrich_search_items_from_detail(self, ctx, items)

    async def _search_from_explore(self, ctx, keyword: str):
        matched = []
        for group in self.explore_defs[:3]:
            for item in await self.explore(ctx, group["groupId"], 1):
                if keyword in item.get("name", ""):
                    matched.append(item)
            if matched:
                break
        return matched

    def _parse_search_rows(self, ctx, html: str, base_for_join: str, selector: str):
        rows = ctx.select(html, selector)
        items = []
        seen: set[str] = set()
        for row in rows:
            name_node = ctx.select(row, "dd > h3 > a")
            if name_node:
                name = name_node[0].text_content().strip()
                href = name_node[0].get("href", "")
                author = ""
                latest = ""
                for dd in ctx.select(row, "dd"):
                    text = dd.text_content()
                    if "作者" in text:
                        span = dd.find("span")
                        if span is not None:
                            author = span.text_content().strip()
                    if "最新" in text or "更新" in text:
                        a = dd.find("a")
                        if a is not None:
                            latest = a.text_content().strip()
            else:
                name = row.get("title", "") or row.text_content().strip()
                href = row.get("href", "")
                author = ""
                latest = ""
            book_url = urljoin(base_for_join, href)
            if not name or book_url in seen:
                continue
            seen.add(book_url)
            items.append({
                "sourceId": self.id,
                "name": name,
                "author": author,
                "bookUrl": book_url,
                "lastChapter": latest,
            })
        return items

    async def detail(self, ctx, book_url: str):
        html = await ctx.access.http.fetch_text(book_url, headers=self.headers)
        name = ctx.attr(html, 'meta[property="og:novel:book_name"]', "content") or ctx.text(html, ".name") or ctx.text(html, "#info > h1")
        author = ctx.attr(html, 'meta[property="og:novel:author"]', "content") or ctx.text(html, "#info > p:nth-child(2)").replace("作者：", "").strip()
        intro = ctx.attr(html, 'meta[property="og:description"]', "content") or ctx.text(html, ".intro") or ctx.text(html, "#intro > p:nth-child(1)")
        cover = ctx.attr(html, 'meta[property="og:image"]', "content") or ctx.attr(html, "#fmimg > img", "src")
        kind = ctx.attr(html, 'meta[property="og:novel:category"]', "content") or ""
        status = ctx.attr(html, 'meta[property="og:novel:status"]', "content") or ""
        last = ctx.attr(html, 'meta[property="og:novel:latest_chapter_name"]', "content") or ctx.text(html, "#info > p:nth-child(4) > a")
        update_time = ctx.attr(html, 'meta[property="og:novel:update_time"]', "content") or ""
        toc_url = ctx.attr(html, 'meta[property="og:novel:read_url"]', "content") or book_url
        return {
            "sourceId": self.id,
            "name": name,
            "author": author,
            "bookUrl": book_url,
            "coverUrl": urljoin(book_url, cover) if cover else "",
            "intro": intro,
            "kind": " / ".join([p for p in [kind, status] if p]),
            "lastChapter": last,
            "wordCount": "",
            "updateTime": update_time,
            "tocUrl": toc_url,
            "authRequired": False,
        }

    async def toc(self, ctx, toc_url: str):
        html = await ctx.access.http.fetch_text(toc_url, headers=self.headers)
        marker = "/" + toc_url.rstrip("/").split("/")[-1] + "/"
        # Try dl structure first (excludes "作品相关" preface chapters)
        links = ctx.select(html, "dl > dt:nth-of-type(2) ~ dd > a")
        if not links:
            # Fallback: all .html links under body, excluding #info (book detail)
            all_links = ctx.select(html, 'a[href$=".html"]')
            info_node = ctx.select(html, "#info")
            info_hrefs = set()
            if info_node:
                info_hrefs = {a.get("href", "") for a in ctx.select(info_node[0], 'a[href$=".html"]')}
            links = [
                a for a in all_links
                if a.get("href", "") not in info_hrefs
                and marker in urljoin(toc_url, a.get("href", ""))
            ]
        chapters = []
        seen: set[str] = set()
        for a in links:
            href = a.get("href", "")
            title = a.text_content().strip()
            if not href or not title or href in seen:
                continue
            seen.add(href)
            chapters.append({
                "sourceId": self.id,
                "title": title,
                "chapterUrl": urljoin(toc_url, href),
                "isVip": False,
                "isLocked": False,
            })
        if self._looks_reverse_order(chapters):
            chapters = list(reversed(chapters))
        for i, ch in enumerate(chapters, start=1):
            ch["index"] = i
        return chapters

    def _looks_reverse_order(self, chapters: list[dict]) -> bool:
        first = self._chapter_number(chapters[0].get("title", "")) if chapters else 0
        last = self._chapter_number(chapters[-1].get("title", "")) if chapters else 0
        return first > 0 and last > 0 and first > last

    def _chapter_number(self, title: str) -> int:
        import re

        match = re.search(r"第\s*(\d+)\s*章", title or "")
        if match:
            return int(match.group(1))
        match = re.search(r"第\s*([一二三四五六七八九十百千万零〇两]+)\s*章", title or "")
        return self._chinese_number(match.group(1)) if match else 0

    def _chinese_number(self, text: str) -> int:
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
        total = 0
        section = 0
        number = 0
        for char in text:
            if char in digits:
                number = digits[char]
                continue
            unit = units.get(char)
            if not unit:
                continue
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        return total + section + number

    def _chapter_stem(self, url: str) -> str:
        path = url.split("?")[0].split("#")[0]
        if "_" in path:
            return path.rsplit("_", 1)[0]
        return path.rsplit(".", 1)[0] if "." in path else path

    def _clean_chapter_content(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "iframe", "ins", "center"]):
            tag.decompose()
        for div in soup.find_all("div"):
            text = div.get_text(strip=True)
            if len(text) < 80 and any(kw in text for kw in ["广告", "声明", "本章结束", "返回目录", "加入书签", "推荐", "最新网址", "章节内容缺失", "章节不存在", "书海阁", "shuhaige"]):
                div.decompose()
        for tag in soup.find_all("br"):
            tag.replace_with("\n")
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
        parts: list[str] = []
        current_url = chapter_url
        title = ""
        original_stem = self._chapter_stem(chapter_url)
        while current_url and len(parts) < 10:
            html = await ctx.access.http.fetch_text(current_url, headers=self.headers)
            if not title:
                title = ctx.text(html, "h1") or ctx.text(html, ".bookname > h1") or ctx.text(html, ".title")
            content_html = ctx.html(html, ".content") or ctx.html(html, "#content") or ctx.html(html, "#txt")
            content = self._clean_chapter_content(content_html)
            if content:
                parts.append(content)
            next_href = ctx.attr(html, "#next_url", "href") or ctx.attr(html, "a:contains('下一章')", "href")
            if not next_href or next_href == "javascript:void(0);":
                break
            next_url = urljoin(current_url, next_href)
            if self._chapter_stem(next_url) != original_stem:
                break
            current_url = next_url
        title = re.sub(r"[（(][\d/]+[）)]", "", title or "").strip()
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
