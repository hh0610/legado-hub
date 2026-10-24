"""七猫小说书源（qimao_novel）。

依据七猫 App 逆向成果实现（逆向项目目录见 README，此处不展开路径）：
- 游客登录（xiaoshuo.wtzw.com/api/v1/login/tourist）获取 JWT，设备指纹本地生成；
- 所有请求带 9 字段 header 签名（sign = md5(固定序串 + SECRET)），
  GET query 另带排序串接的 query sign；
- 搜索 tab=3 → 章节目录（api-ks chapter-list）→ 正文（api-ks chapter/content）；
- 正文为 base64(IV + AES-128-CBC)，reader_type=3（epub）解密后是 zip 包，
  解出 xhtml 去标签。

bookUrl / chapterUrl 使用 api-ks.wtzw.com 域下的伪路径承载状态：
- 书：  https://api-ks.wtzw.com/qm/book/{book_id}/{reader_type}
- 章：  https://api-ks.wtzw.com/qm/chapter/{book_id}/{chapter_id}/{reader_type}
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime
from urllib.parse import quote

QM_SECRET = "d3dGiJc651gSQ8w1"
QM_AES_KEY = "242ccb8230d709e1"
QM_PARAM_MAP = "PXMUlErYWbdJ9saI0oy_HGitgNA8Fk3hfRqC4pmBOuc6Kx5T-2zSZ1VvjQ7DwnLe"
QM_CONTENT_MAP = "5NhEilDnz01JC67qSmT89-FkGKor4stuHvBwxOILcdMeAfgPQRbyU2p3VjWX_YZa"
QM_B64 = "+/0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
QM_APP_ID = "com.kmxs.reader"
QM_CHANNEL = "qm-guanfang_lf"
QM_APP_VERSION = "80800"

_URL_BASE = "https://api-ks.wtzw.com"

_PARAM_TRANS = str.maketrans({c: QM_PARAM_MAP[QM_B64.index(c)] for c in QM_B64})
_CONTENT_TRANS = str.maketrans({c: QM_B64[QM_CONTENT_MAP.index(c)] for c in QM_CONTENT_MAP})


def _md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _qm_sign(value: str) -> str:
    return _md5_hex(value + QM_SECRET)


def _url_sign(data: dict) -> str:
    text = "".join(f"{k}={data[k]}" for k in sorted(data))
    return _qm_sign(text)


def _param_encode(value: str) -> str:
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    return b64.translate(_PARAM_TRANS)


def _content_layer_decode(value: str) -> str:
    b64 = str(value).translate(_CONTENT_TRANS)
    b64 += "=" * (-len(b64) % 4)
    return base64.b64decode(b64).decode("utf-8", "replace")


def _parse_payload(text: str):
    text = (text or "").lstrip("\ufeff").strip()
    value = json.loads(text)
    if isinstance(value, dict) and value.get("v"):
        plain = _content_layer_decode(value["v"])
        if plain:
            value = json.loads(plain)
    return value


def _random_hex(length: int) -> str:
    value = uuid.uuid4().hex
    while len(value) < length:
        value += uuid.uuid4().hex
    return value[:length]


def _date_stamp() -> str:
    now = datetime.now()
    return f"{now.year}{now.month:02d}{now.day:02d}{now.hour:02d}{now.minute:02d}{now.second:02d}"


def _epub_spine_docs(archive) -> list:
    """容器→OPF→spine 顺序的 XHTML 文档列表；解析失败回退文件名排序。"""
    import xml.etree.ElementTree as ET
    from urllib.parse import unquote

    try:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf_path = next(
            el.get("full-path", "")
            for el in container.iter()
            if el.tag.endswith("rootfile")
        )
        opf = ET.fromstring(archive.read(opf_path))
        opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        manifest = {}
        for el in opf.iter():
            if el.tag.endswith("item") and el.get("id") and el.get("href"):
                manifest[el.get("id")] = el.get("href")
        ordered = []
        for el in opf.iter():
            if el.tag.endswith("itemref"):
                href = manifest.get(el.get("idref"), "")
                if href:
                    name = unquote(opf_dir + href)
                    if name in archive.namelist():
                        ordered.append(name)
        if ordered:
            return ordered
    except Exception:
        pass
    return sorted(
        n for n in archive.namelist()
        if n.lower().endswith((".xhtml", ".html", ".htm"))
    )


def _decrypt_content(content_b64: str, reader_type: str) -> str:
    from Crypto.Cipher import AES

    raw = base64.b64decode(content_b64)
    if len(raw) <= 16:
        raise ValueError("invalid chapter ciphertext")
    iv, enc = raw[:16], raw[16:]
    plain = AES.new(QM_AES_KEY.encode("utf-8"), AES.MODE_CBC, iv).decrypt(enc)
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    if reader_type == "3" and plain[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(plain))
        # 按 OPF spine 顺序提取（namelist 顺序会打乱段落）；无 OPF 时回退文件名排序。
        doc_names = _epub_spine_docs(archive)
        parts = []
        for name in doc_names:
            html = archive.read(name).decode("utf-8", "replace")
            html = re.sub(r"(?is)<(script|style).*?</" + BS + "1>", "", html)
            html = re.sub(r"(?s)<[^>]+>", " " + NL, html)
            lines = [ln.strip() for ln in html.splitlines()]
            parts.append(" ".join(ln for ln in lines if ln))
        text = ("" + NL + NL).join(p for p in parts if p)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip().lstrip("" + BS + "ufeff")
    return plain.decode("utf-8", "replace").lstrip("\ufeff")


class Source:
    id = "qimao_novel"
    name = "七猫小说"
    contract_version = "1.0"
    last_modified = "2026-10-01"

    def __init__(self):
        # chapter_id -> (content_md5, {md5(line)[:8] -> (段落序号, 段落文本)})；段评锚定用。
        # 有界：最多保留 32 章，超出丢弃最旧（OrderedDict 语义用 dict 有序性）。
        self._paragraph_anchors: dict[str, tuple[str, dict[str, tuple[int, str]]]] = {}
        self._device = {
            "uuid": str(uuid.uuid4()),
            "device_id": _date_stamp() + _random_hex(48),
            "source_uid": _random_hex(16),
            "mac": "02:" + ":".join(_random_hex(10)[i:i + 2] for i in range(0, 10, 2)),
            "gender": "0", "static_score": "0.4", "sys_ver": "12",
            "phone_level": "M", "app_version": QM_APP_VERSION,
            "brand": "Redmi", "make": "Redmi", "model": "23117RK66C",
        }
        self._token = ""
        self._token_uid = ""
        self._token_exp = 0.0

    # ---- 七猫签名层 -----------------------------------------------------

    def _qm_params(self) -> str:
        d = self._device
        fields = [
            ["gender", d["gender"]], ["static_score", d["static_score"]], ["uuid", d["uuid"]],
            ["device-id", d["device_id"]], ["mac", d["mac"]], ["sourceuid", d["source_uid"]],
            ["sys-ver", d["sys_ver"]], ["phone-level", d["phone_level"]], ["app_ver", d["app_version"]],
            ["imei", ""], ["model", d["model"]], ["wlb-imei", ""], ["wlb-uid", d["source_uid"]],
            ["client-id", d["source_uid"]], ["brand", d["brand"]], ["make", d["make"]], ["net_env", "1"],
        ]
        parts = [json.dumps(k) + ":" + json.dumps(str(v)) for k, v in fields]
        return _param_encode("{" + ",".join(parts) + "}")

    def _headers(self) -> dict:
        qm_params = self._qm_params()
        now = int(time.time())
        values = {
            "AUTHORIZATION": self._token, "app-version": QM_APP_VERSION,
            "application-id": QM_APP_ID, "channel": QM_CHANNEL, "is-white": "0",
            "net-env": "1", "platform": "android", "qm-params": qm_params, "reg": "",
        }
        order = ["AUTHORIZATION", "app-version", "application-id", "channel",
                 "is-white", "net-env", "platform", "qm-params", "reg"]
        sign = _qm_sign("".join(f"{k}={values[k]}" for k in order))
        day = datetime.now()
        uaf = f"{day.year}{day.month:02d}{day.day:02d}-{self._token_uid}"
        return {
            "authorization": self._token, "app-version": QM_APP_VERSION,
            "application-id": QM_APP_ID, "channel": QM_CHANNEL, "is-white": "0",
            "net-env": "1", "platform": "android", "qm-params": qm_params, "reg": "",
            "sign": sign,
            "qm-uaf": uaf, "qm-it": str(now), "qm-ii": str(now % 10000000000),
            "no-permiss": "3", "user-agent": "webviewversion/0",
            "qm-request-id": _qm_sign(str(int(time.time() * 1000)) + _random_hex(8)),
        }

    async def _login(self, ctx, force: bool = False) -> None:
        if not force and self._token and time.time() < self._token_exp:
            return
        form = {"cancell_check": "1", "gender": "0"}
        form["sign"] = _url_sign(form)
        body = "&".join(f"{quote(k)}={quote(str(form[k]))}" for k in sorted(form))
        headers = self._headers()
        headers["content-type"] = "application/x-www-form-urlencoded"
        text = await ctx.access.http.fetch_text(
            "https://xiaoshuo.wtzw.com/api/v1/login/tourist",
            method="POST", data=body, headers=headers,
        )
        payload = _parse_payload(text)
        token = str(((payload.get("data") or {}).get("token")) or "")
        if not token:
            raise RuntimeError(f"qimao tourist login failed: {str(payload)[:200]}")
        self._token = token
        self._token_exp = time.time() + 3000
        try:
            jwt_payload = json.loads(base64.b64decode(token.split(".")[1] + "==").decode("utf-8", "replace"))
            self._token_uid = str((jwt_payload.get("user") or {}).get("uid") or "")
        except Exception:
            self._token_uid = ""

    def _review_identity(self, chapter_url: str) -> tuple[str, str, str]:
        """(book_id, chapter_id, content_md5) — 小说章 URL 不含 md5，段评按需跳过。"""
        text = str(chapter_url or "")
        marker = "/qm/chapter/"
        if marker in text:
            tail = text.split(marker, 1)[1].split("?", 1)[0]
            parts = tail.split("/")
            book_id = parts[0] if parts else ""
            chapter_id = parts[1] if len(parts) > 1 else ""
            return book_id, chapter_id, ""
        return "", "", ""

    def _book_identity_for_reviews(self, book_url: str) -> str:
        return self._book_identity(book_url)[0]

    # ---- 源设置（控制台「源设置」声明，动作仅管理员触发） -----------------

    async def ui_guest_status(self, ctx, payload: dict) -> dict:
        remaining = max(0, int(self._token_exp - time.time())) if self._token else 0
        state = "已登录" if self._token else "未登录"
        return {
            "ok": True,
            "message": (
                f"{state} uid={self._token_uid or '-'} token剩余≈{remaining}s "
                f"设备指纹={self._device['device_id'][:20]}…"
            ),
        }

    async def ui_test_login(self, ctx, payload: dict) -> dict:
        await self._login(ctx, force=True)
        return {"ok": True, "message": f"直连登录成功 uid={self._token_uid}"}

    async def ui_reset_identity(self, ctx, payload: dict) -> dict:
        self._reset_identity()
        return {"ok": True, "message": "游客身份已重置（新设备指纹，下次请求自动重新登录）"}

    def _reset_identity(self) -> None:
        self._device = {
            "uuid": str(uuid.uuid4()),
            "device_id": _date_stamp() + _random_hex(48),
            "source_uid": _random_hex(16),
            "mac": "02:" + ":".join(_random_hex(10)[i:i + 2] for i in range(0, 10, 2)),
            "gender": "0", "static_score": "0.4", "sys_ver": "12",
            "phone_level": "M", "app_version": QM_APP_VERSION,
            "brand": "Redmi", "make": "Redmi", "model": "23117RK66C",
        }
        self._token = ""
        self._token_uid = ""
        self._token_exp = 0.0

    # ---- 章评 / 段评 / 书评（api-cmnt.wtzw.com，逆向已实测） ----------------

    async def chapter_reviews(self, ctx, chapter_url: str) -> dict:
        """章评列表 + 段评（气泡接口，md5 锚定）映射为阅读C/Max 气泡数据。"""
        book_id, chapter_id, content_md5 = self._review_identity(chapter_url)
        if not book_id or not chapter_id:
            raise ValueError(f"invalid qimao review url: {chapter_url}")
        # 目录/正文阶段缓存的锚点：chapter_id -> (content_md5, {md5(line)[:8]: 段落序号})
        cached_md5, paragraph_anchors = self._paragraph_anchors.get(chapter_id, ("", {}))
        if not content_md5:
            content_md5 = cached_md5
        first = await self._get(ctx, "https://api-cmnt.wtzw.com/api/v1/chapter-comment/first", {
            "book_id": book_id, "chapter_id": chapter_id, "sort": "", "extra": "",
        })
        data = first.get("data") or {}
        try:
            chapter_count = int(data.get("comment_count") or 0)
        except (TypeError, ValueError):
            chapter_count = 0
        chapter_end = []
        for item in data.get("comment_list") or []:
            if not isinstance(item, dict):
                continue
            user = item.get("user_info") or {}
            chapter_end.append({
                "id": str(item.get("comment_id") or ""),
                "userName": str(user.get("nickname") or item.get("nickname") or "书友"),
                "avatar": str(user.get("avatar") or item.get("avatar") or ""),
                "content": str(item.get("content") or ""),
                "likeCount": item.get("like_count") or 0,
                "time": str(item.get("comment_time") or ""),
                "replyCount": item.get("reply_count") or 0,
            })
        hot_paragraph_reviews = []
        if content_md5:
            # 气泡接口（POST form，无需签名；逆向实测）：每章返回约 80-120 个
            # {p: md5(段落原始行)[:8], c: 评论数, icon_type}。c>0 的才是有评论的段落。
            try:
                bubble_text = await ctx.access.http.fetch_text(
                    "https://api-cmnt.wtzw.com/api/v1/paragraph/bubbles",
                    method="POST",
                    data={"book_id": book_id, "chapters": json.dumps([
                        {"chapter_id": chapter_id, "chapter_md5": content_md5}
                    ], ensure_ascii=False)},
                    headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
                )
                bubble_payload = _parse_payload(bubble_text)
                chapters_data = (bubble_payload.get("data") or {}).get("chapters") or []
                bubbles = chapters_data[0].get("bubbles") or [] if chapters_data else []
            except Exception:
                bubbles = []
            for bubble in bubbles:
                if not isinstance(bubble, dict):
                    continue
                try:
                    comment_count = int(bubble.get("c") or 0)
                except (TypeError, ValueError):
                    continue
                if comment_count <= 0:
                    continue
                anchor = paragraph_anchors.get(str(bubble.get("p") or ""))
                if anchor is None:
                    continue
                paragraph_index, paragraph_text = anchor
                hot_paragraph_reviews.append({
                    "paragraphId": paragraph_index,
                    "matchedParagraphIndex": paragraph_index,
                    "matchedParagraphCount": 1,
                    "matchedText": paragraph_text,
                    "paragraphText": paragraph_text,
                    "commentCount": comment_count,
                    "hotCommentCount": comment_count,
                })
        if content_md5 and not hot_paragraph_reviews:
            # 气泡接口无数据时回退 p-list（部分书目的段评走该接口）。
            plist = await self._get(ctx, "https://api-cmnt.wtzw.com/api/v1/paragraph/inset/p-list", {
                "book_id": book_id, "chapter_id": chapter_id,
                "last_paragraph_offset": "0", "next_id": "",
                "chapter_md5": content_md5, "click_paragraph_id": "",
            })
            pdata = plist.get("data") or {}
            for item in pdata.get("paragraph_comment_list") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    paragraph_id = int(item.get("paragraph_id") or item.get("paragraphId") or -1)
                except (TypeError, ValueError):
                    continue
                if paragraph_id < 0:
                    continue
                hot_paragraph_reviews.append({
                    "paragraphId": paragraph_id,
                    "matchedParagraphIndex": max(0, paragraph_id - 1),
                    "matchedParagraphCount": 1,
                    "matchedText": "",
                    "commentCount": item.get("comment_count") or len(item.get("comment_list") or []) or 0,
                    "hotCommentCount": item.get("hot_count") or 0,
                })
        book_url = f"https://api-ks.wtzw.com/qm/book/{book_id}/0" if book_id else ""
        return {
            "paragraphs": {},
            "bookUrl": book_url,
            "chapterEnd": chapter_end,
            "chapterEndHot": [],
            "authorReviews": [],
            "hotParagraphReviews": hot_paragraph_reviews,
            "summary": {"chapterEndCount": chapter_count},
            "debug": {},
        }

    async def book_reviews(self, ctx, book_url: str) -> dict:
        """书评聚合（评分/人数/好评率）+ 书评列表。"""
        book_id = self._book_identity_for_reviews(book_url)
        if not book_id:
            raise ValueError(f"invalid qimao book url: {book_url}")
        payload = await self._get(ctx, "https://api-cmnt.wtzw.com/api/v1/comment/book-evaluates", {
            "book_id": book_id, "tag_id": "", "sort": "", "next_id": "", "source": "", "audio_type": "",
        })
        data = payload.get("data") or {}
        book_block = data.get("book") or {}
        items = []
        for item in data.get("comment_list") or []:
            if not isinstance(item, dict):
                continue
            items.append({
                "id": str(item.get("comment_id") or ""),
                "userName": str(item.get("nickname") or "书友"),
                "avatar": str(item.get("avatar") or ""),
                "content": str(item.get("content") or ""),
                "likeCount": item.get("like_count") or 0,
                "time": str(item.get("comment_time") or ""),
                "replyCount": item.get("reply_count") or 0,
                "rating": str(item.get("eval_rating") or ""),
            })
        return {
            "summary": {
                "score": str(book_block.get("score") or ""),
                "peopleCount": str(data.get("eval_people_count") or ""),
                "positivePercent": str(data.get("positive_percent") or ""),
            },
            "items": items,
            "debug": {},
        }

    # ---- 评论扩展（章评分页 / 段评列表 / 页热评 / 回复；逆向 v2 接口） ----

    async def _post_form(self, ctx, url: str, form: dict) -> dict:
        await self._login(ctx)
        text = await ctx.access.http.fetch_text(
            url, method="POST", data=form, headers=self._headers(),
        )
        return _parse_payload(text)

    @staticmethod
    def _comment_item(item: dict) -> dict:
        user = item.get("user_info") or {}
        return {
            "id": str(item.get("comment_id") or item.get("cmt_id") or ""),
            "userName": str(user.get("nickname") or item.get("nickname") or "书友"),
            "avatar": str(user.get("avatar") or item.get("avatar") or ""),
            "content": str(item.get("content") or ""),
            "likeCount": item.get("like_count") or item.get("likeCount") or 0,
            "time": str(item.get("comment_time") or item.get("ctime") or ""),
            "replyCount": item.get("reply_count") or 0,
        }

    @staticmethod
    def _comment_zones(data: dict) -> tuple[list[dict], list[dict], int]:
        hot = [x for x in (data.get("hot_zone") or {}).get("comment_list") or [] if isinstance(x, dict)]
        time_zone = [x for x in (data.get("time_zone") or {}).get("comment_list") or [] if isinstance(x, dict)]
        try:
            total = int(data.get("time_zone", {}).get("comment_count") or data.get("hot_zone", {}).get("comment_count") or len(hot) + len(time_zone))
        except (TypeError, ValueError):
            total = len(hot) + len(time_zone)
        return hot, time_zone, total

    @staticmethod
    def _para_md5_for_index(paragraph_id: int, anchors: dict[str, tuple[int, str]]) -> str:
        for md5_prefix, (index, _text) in anchors.items():
            if index == paragraph_id:
                return md5_prefix
        return ""

    async def chapter_say(self, ctx, chapter_url: str, *, page: int = 1, page_size: int = 20) -> dict:
        """章评分页：first(POST) 取热+时间两区，more(GET next_id) 翻页。"""
        book_id, chapter_id, _md5 = self._review_identity(chapter_url)
        if not book_id or not chapter_id:
            return {"comments": [], "totalCount": 0, "hasMore": False, "debug": {"error": "invalid chapter"}}
        comments: list[dict] = []
        next_id = ""
        if page == 1:
            data = (await self._post_form(ctx, "https://api-cmnt.wtzw.com/api/v2/chapter-comment/first", {
                "book_id": book_id, "chapter_id": chapter_id,
                "hot": "0", "exposed_ids": "", "need_common": "0",
            })).get("data") or {}
            hot, timed, _total = self._comment_zones(data)
            comments = [self._comment_item(x) for x in hot + timed]
            next_id = str(data.get("next_id") or "")
        else:
            # 光标式翻页：逐页推进到目标页（qimao v2 用 next_id 而非偏移）。
            data = (await self._post_form(ctx, "https://api-cmnt.wtzw.com/api/v2/chapter-comment/first", {
                "book_id": book_id, "chapter_id": chapter_id,
                "hot": "0", "exposed_ids": "", "need_common": "0",
            })).get("data") or {}
            next_id = str(data.get("next_id") or "")
            for _ in range(page - 1):
                if not next_id:
                    break
                more = await self._get(ctx, "https://api-cmnt.wtzw.com/api/v2/chapter-comment/more", {
                    "book_id": book_id, "chapter_id": chapter_id, "next_id": next_id,
                })
                mdata = more.get("data") or {}
                batch = [self._comment_item(x) for x in (mdata.get("comment_list") or []) if isinstance(x, dict)]
                comments.extend(batch)
                next_id = str(mdata.get("next_id") or "")
        start = (page - 1) * page_size
        window = comments[start:start + page_size] if page == 1 else comments
        return {
            "comments": window,
            "totalCount": len(comments) if page > 1 else len(comments),
            "hasMore": bool(next_id),
            "debug": {"nextId": next_id[:32]},
        }

    async def paragraph_say(self, ctx, chapter_url: str, paragraph_id: int, *,
                            page: int = 1, page_size: int = 20, cursor_id: int = 0) -> dict:
        """单段段评列表：v2 paragraph/first + more，paragraph_id 传段落 md5 前缀。"""
        book_id, chapter_id, _md5 = self._review_identity(chapter_url)
        anchors = self._paragraph_anchors.get(chapter_id, ("", {}))[1]
        para_md5 = self._para_md5_for_index(int(paragraph_id), anchors)
        if not book_id or not chapter_id or not para_md5:
            return {"comments": [], "totalCount": 0, "hasMore": False,
                    "debug": {"error": "no paragraph anchor (open the chapter first)"}}
        comments: list[dict] = []
        next_id = ""
        if page == 1:
            data = (await self._post_form(ctx, "https://api-cmnt.wtzw.com/api/v2/paragraph/first", {
                "book_id": book_id, "chapter_id": chapter_id,
                "paragraph_id": para_md5, "hot": "0", "check_cmt_id": "",
                "exposed_ids": "", "need_common": "0",
            })).get("data") or {}
            hot, timed, _total = self._comment_zones(data)
            comments = [self._comment_item(x) for x in hot + timed]
            next_id = str(data.get("next_id") or "")
        else:
            data = (await self._post_form(ctx, "https://api-cmnt.wtzw.com/api/v2/paragraph/first", {
                "book_id": book_id, "chapter_id": chapter_id,
                "paragraph_id": para_md5, "hot": "0", "check_cmt_id": "",
                "exposed_ids": "", "need_common": "0",
            })).get("data") or {}
            next_id = str(data.get("next_id") or "")
            for _ in range(page - 1):
                if not next_id:
                    break
                more = await self._get(ctx, "https://api-cmnt.wtzw.com/api/v2/paragraph/more", {
                    "book_id": book_id, "chapter_id": chapter_id,
                    "paragraph_id": para_md5, "hot": "0", "next_id": next_id,
                })
                mdata = more.get("data") or {}
                comments.extend(self._comment_item(x) for x in (mdata.get("comment_list") or []) if isinstance(x, dict))
                next_id = str(mdata.get("next_id") or "")
        return {
            "comments": comments,
            "totalCount": len(comments),
            "hasMore": bool(next_id),
            "paragraphId": int(paragraph_id),
            "debug": {"paraMd5": para_md5[:16], "nextId": next_id[:32]},
        }

    async def page_hot_reviews(self, ctx, chapter_url: str, paragraph_ids: list, *,
                               page: int = 1, page_size: int = 20) -> dict:
        """页热评：合并当前页各段的段评（每段取前 page_size 条）。"""
        comments: list[dict] = []
        total = 0
        for pid in list(paragraph_ids)[:50]:
            try:
                detail = await self.paragraph_say(ctx, chapter_url, int(pid), page=1, page_size=page_size)
                comments.extend(detail.get("comments") or [])
                total += len(detail.get("comments") or [])
            except Exception:
                continue
        return {"comments": comments, "totalCount": total, "hasMore": False}

    async def review_replies(self, ctx, chapter_url: str, root_review_id: str, *,
                             page: int = 1, page_size: int = 20, cursor_id: int = 0) -> dict:
        """评论回复：段落评论走 v1 paragraph/detail，章评走 v1 comment/detail。"""
        book_id, chapter_id, _md5 = self._review_identity(chapter_url)
        if not book_id or not chapter_id:
            return {"comments": [], "totalCount": 0, "hasMore": False, "debug": {"error": "invalid chapter"}}
        anchors = self._paragraph_anchors.get(chapter_id, ("", {}))[1]
        is_paragraph = int(root_review_id or 0) in {idx for idx, _t in anchors.values()} if anchors else False
        if is_paragraph:
            para_md5 = self._para_md5_for_index(int(root_review_id), anchors)
            data = (await self._post_form(ctx, "https://api-cmnt.wtzw.com/api/v1/paragraph/detail", {
                "book_id": book_id, "comment_id": str(root_review_id),
                "para_md5": para_md5,
            })).get("data") or {}
        else:
            data = (await self._get(ctx, "https://api-cmnt.wtzw.com/api/v1/comment/detail", {
                "book_id": book_id, "chapter_id": chapter_id, "comment_id": str(root_review_id),
            })).get("data") or {}
        replies = [self._comment_item(x) for x in (data.get("reply_list") or data.get("comment_list") or []) if isinstance(x, dict)]
        return {
            "comments": replies,
            "replies": replies,
            "totalCount": len(replies),
            "hasMore": False,
            "rootReviewId": str(root_review_id),
        }

    async def _get(self, ctx, url: str, params: dict) -> dict:
        await self._login(ctx)
        query = {k: str(v) for k, v in params.items()}
        parts = [f"{quote(k)}={quote(query[k])}" for k in sorted(query)]
        parts.append(f"sign={_url_sign(query)}")
        text = await ctx.access.http.fetch_text(
            f"{url}?{'&'.join(parts)}", headers=self._headers(),
        )
        return _parse_payload(text)

    # ---- 伪 URL 编解码 ---------------------------------------------------

    @staticmethod
    def _book_identity(book_url: str) -> tuple[str, str]:
        text = str(book_url or "")
        marker = "/qm/book/"
        if marker in text:
            tail = text.split(marker, 1)[1].split("?", 1)[0]
            parts = tail.split("/")
            book_id = parts[0] if parts else ""
            reader_type = parts[1] if len(parts) > 1 else "0"
            return book_id, reader_type or "0"
        return "", "0"

    @staticmethod
    def _chapter_identity(chapter_url: str) -> tuple[str, str, str]:
        text = str(chapter_url or "")
        marker = "/qm/chapter/"
        if marker in text:
            tail = text.split(marker, 1)[1].split("?", 1)[0]
            parts = tail.split("/")
            book_id = parts[0] if parts else ""
            chapter_id = parts[1] if len(parts) > 1 else ""
            reader_type = parts[2] if len(parts) > 2 else "0"
            return book_id, chapter_id, reader_type or "0"
        return "", "", "0"

    # ---- 生命周期 ---------------------------------------------------------

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        payload = await self._get(ctx, "https://api-bc.wtzw.com/search/v1/words", {
            "extend": "", "tab": "3", "gender": "0", "refresh_state": "8",
            "track_id": self._device["source_uid"] + str(int(time.time() * 1000)),
            "page": str(page), "book_id": "", "book_privacy": "1",
            "wd": keyword, "read_preference": "4", "is_short_story_user": "0",
        })
        items = []
        for raw in (payload.get("data") or {}).get("books") or []:
            book_id = str(raw.get("id") or raw.get("book_id") or "")
            title = str(raw.get("title") or raw.get("original_title") or "")
            if not book_id or not title:
                continue
            reader_type = str(raw.get("reader_type") or "0")
            items.append({
                "sourceId": self.id,
                "name": title,
                "author": str(raw.get("author") or raw.get("original_author") or ""),
                "bookUrl": f"{_URL_BASE}/qm/book/{book_id}/{reader_type}",
                "coverUrl": str(raw.get("image_link") or ""),
                "intro": str(raw.get("intro") or ""),
                "kind": str(raw.get("sub_title") or ""),
                "lastChapter": str(raw.get("latest_chapter_title") or ""),
                "wordCount": str(raw.get("words_num") or ""),
                "score": 0,
                "extra": {"readerType": reader_type},
            })
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        book_id, reader_type = self._book_identity(book_url)
        if not book_id:
            raise ValueError(f"invalid qimao book url: {book_url}")
        payload = await self._get(ctx, "https://api-bc.wtzw.com/api/v1/reader/detail", {
            "ab_type": "2", "id": book_id,
        })
        data = payload.get("data") or {}
        reader_type = str(data.get("reader_type") or reader_type or "0")
        tags = "/".join(
            str(tag.get("title") or "") for tag in (data.get("book_tag_list") or []) if isinstance(tag, dict)
        )
        return {
            "sourceId": self.id,
            "name": str(data.get("title") or data.get("original_title") or ""),
            "author": str(data.get("author") or ""),
            "bookUrl": f"{_URL_BASE}/qm/book/{book_id}/{reader_type}",
            "coverUrl": str(data.get("big_image_link") or data.get("image_link") or ""),
            "intro": str(data.get("intro") or ""),
            "kind": tags or str(data.get("category1_name") or ""),
            "lastChapter": str(data.get("latest_chapter_title") or data.get("chapter_list_desc") or ""),
            "wordCount": str(data.get("category_over_words") or ""),
            "tocUrl": f"{_URL_BASE}/qm/book/{book_id}/{reader_type}",
            "bookStatus": "completed" if str(data.get("is_over") or "") == "1" else "ongoing",
            "authRequired": False,
            "extra": {"readerType": reader_type},
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        book_id, reader_type = self._book_identity(toc_url)
        if not book_id:
            raise ValueError(f"invalid qimao toc url: {toc_url}")
        payload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/chapter/chapter-list", {
            "id": book_id, "chapter_ver": "0", "reader_type": reader_type,
        })
        data = payload.get("data") or {}
        chapters = []
        for index, raw in enumerate(data.get("chapter_lists") or [], start=1):
            chapter_id = str(raw.get("id") or "")
            title = str(raw.get("title") or "")
            if not chapter_id:
                continue
            chapters.append({
                "sourceId": self.id,
                "index": int(raw.get("index") or index),
                "title": title,
                "chapterUrl": f"{_URL_BASE}/qm/chapter/{book_id}/{chapter_id}/{reader_type}",
                "updateTime": "",
                "isVip": False,
                "isLocked": False,
                "extra": {},
            })
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        book_id, chapter_id, reader_type = self._chapter_identity(chapter_url)
        if not book_id or not chapter_id:
            raise ValueError(f"invalid qimao chapter url: {chapter_url}")
        params = {"id": book_id, "chapterId": chapter_id}
        if reader_type:
            params["reader_type"] = reader_type
        payload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/chapter/content", params)
        data = payload.get("data") or {}
        content_b64 = str(data.get("content") or "")
        if not content_b64:
            raise RuntimeError(f"qimao chapter content empty: {payload.get('code')} {str(payload)[:120]}")
        text = _decrypt_content(content_b64, reader_type)
        paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n") if p.strip()]
        content_md5 = str(data.get("content_md5") or "")
        if content_md5:
            # 七猫段评气泡按 md5(段落原始行文本)[:8] 锚定（与 App 一致）。
            anchors: dict[str, int] = {}
            stripped_index = 0
            for line in text.replace("\r\n", "\n").split("\n"):
                if line.strip():
                    anchors[hashlib.md5(line.encode("utf-8")).hexdigest()[:8]] = (stripped_index, line.strip())
                    stripped_index += 1
            self._paragraph_anchors[chapter_id] = (content_md5, anchors)
            if len(self._paragraph_anchors) > 32:
                oldest = next(iter(self._paragraph_anchors))
                self._paragraph_anchors.pop(oldest, None)
        return {
            "sourceId": self.id,
            "title": "",
            "chapterUrl": chapter_url,
            "content": "\n\n".join(paragraphs),
            "format": "text",
            "authRequired": False,
            "isPaid": False,
            "extra": {},
        }
