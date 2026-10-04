"""七猫听书书源（qimao_audio）。

content.kind: audio 媒体书源。七猫"听书"分两条路线：
1. **真人专辑**（有 pre-recorded 专辑）：album/chapter-list 非空时按专辑章走，
   单集音频 = `album/info?album_id&chapter_id` → `voice_list[0].voice_url`。
2. **AI TTS 在线合成**（大多数书）：专辑章节列表为 null，音频按小说章合成，
   = `listen/preload-chapter-list?book_id&chapter_id&content_md5&voice_id`，
   voice_id 来自 `album/info` 的随书音色（voice_type 5/9），返回
   cdn-audio.qimao.com 的直链 mp3（带 expire 参数）+ 字幕 txt。

伪 URL 编码：
- 书：  https://api-ks.wtzw.com/qm/album/{album_id}
- 专辑章：https://api-ks.wtzw.com/qm/album-ep/{album_id}/{chapter_id}
- TTS 章：https://api-ks.wtzw.com/qm/tts/{book_id}/{chapter_id}/{content_md5}
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import datetime
from urllib.parse import quote

QM_SECRET = "d3dGiJc651gSQ8w1"
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


class Source:
    id = "qimao_audio"
    name = "七猫听书"
    contract_version = "1.0"
    last_modified = "2026-10-01"

    def __init__(self):
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
        self._voice_cache: dict[str, str] = {}
        self._album_mode_cache: dict[str, str] = {}

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
        """章评列表 + 段评（TTS 路由自带 content_md5）。"""
        book_id, chapter_id, content_md5 = self._review_identity(chapter_url)
        if not book_id or not chapter_id:
            raise ValueError(f"invalid qimao review url: {chapter_url}")
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
                "content": str(item.get("content") or ""),
                "likeCount": item.get("like_count") or 0,
                "time": str(item.get("comment_time") or ""),
                "replyCount": item.get("reply_count") or 0,
            })
        hot_paragraph_reviews = []
        if content_md5:
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
        return {
            "paragraphs": {},
            "chapterEnd": chapter_end,
            "chapterEndHot": [],
            "authorReviews": [],
            "hotParagraphReviews": hot_paragraph_reviews,
            "summary": {"chapterEndCount": chapter_count},
            "debug": {},
        }

    async def book_reviews(self, ctx, book_url: str) -> dict:
        """书评聚合（评分/人数/好评率）+ 书评列表。"""
        book_id = self._album_id(book_url)
        if not book_id:
            raise ValueError(f"invalid qimao album url: {book_url}")
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

    def _review_identity(self, chapter_url: str) -> tuple[str, str, str]:
        """(book_id, chapter_id, content_md5) — TTS 路由携带 md5，专辑章无。"""
        route, payload = self._chapter_identity(chapter_url)
        if route == "album-ep":
            album_id, _, tail = payload.partition("/")
            chapter_id = tail.split("?", 1)[0].partition("/")[0]
            return album_id, chapter_id, ""
        if route == "tts":
            book_id, _, tail = payload.partition("/")
            rest = tail.split("?", 1)[0]
            chapter_id, _, content_md5 = rest.partition("/")
            return book_id, chapter_id, content_md5
        return "", "", ""

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
    def _album_id(book_url: str) -> str:
        text = str(book_url or "")
        marker = "/qm/album/"
        if marker in text:
            return text.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]
        return ""

    @staticmethod
    def _chapter_identity(chapter_url: str) -> tuple[str, str]:
        """Returns (route, payload) where route is "album-ep" or "tts"."""
        text = str(chapter_url or "")
        for marker, route in (("/qm/album-ep/", "album-ep"), ("/qm/tts/", "tts")):
            if marker in text:
                return route, text.split(marker, 1)[1].split("?", 1)[0]
        return "", ""

    # ---- 音色解析 ---------------------------------------------------------

    async def _pick_voice_id(self, ctx, album_id: str) -> str:
        # 源设置优先：控制台指定的 TTS 音色 ID 覆盖随书音色。
        override = str(ctx.settings.get("tts_voice_id", "") or "").strip()
        if override and override != "0":
            return override
        cached = self._voice_cache.get(album_id)
        if cached:
            return cached
        payload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/album/info", {
            "album_id": album_id, "new_user": "0",
        })
        voices = (payload.get("data") or {}).get("voice_list") or []
        voice_id = ""
        for voice in voices:
            if str(voice.get("voice_type")) in {"5", "9"}:
                candidate = str(voice.get("voice_id") or "")
                if candidate and candidate != "0":
                    voice_id = candidate
                    break
        if voice_id:
            self._voice_cache[album_id] = voice_id
        return voice_id

    @staticmethod
    def _duration_seconds(raw) -> float:
        """'11:01' / '1:02:03' / 90 -> seconds."""
        try:
            text = str(raw or "").strip()
            if not text:
                return 0.0
            parts = text.split(":")
            if not all(p.isdigit() for p in parts):
                return 0.0
            seconds = 0
            for part in parts:
                seconds = seconds * 60 + int(part)
            return float(seconds)
        except Exception:
            return 0.0

    # ---- 生命周期 ---------------------------------------------------------

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        payload = await self._get(ctx, "https://api-bc.wtzw.com/search/v1/words", {
            "extend": "", "tab": "1", "gender": "0", "refresh_state": "8",
            "track_id": self._device["source_uid"] + str(int(time.time() * 1000)),
            "page": str(page), "book_id": "", "book_privacy": "1",
            "wd": keyword, "read_preference": "4", "is_short_story_user": "0",
        })
        items = []
        for raw in (payload.get("data") or {}).get("books") or []:
            album_id = str(raw.get("album_id") or raw.get("id") or "")
            title = str(raw.get("title") or raw.get("original_title") or "")
            if not album_id or not title:
                continue
            items.append({
                "sourceId": self.id,
                "name": title,
                "author": str(raw.get("author") or raw.get("original_author") or ""),
                "bookUrl": f"{_URL_BASE}/qm/album/{album_id}",
                "coverUrl": str(raw.get("image_link") or raw.get("thumb_image_link") or ""),
                "intro": str(raw.get("intro") or ""),
                "kind": "听书",
                "lastChapter": str(raw.get("sub_title") or ""),
                "wordCount": "",
                "score": 0,
                "extra": {"albumId": album_id},
            })
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        album_id = self._album_id(book_url)
        if not album_id:
            raise ValueError(f"invalid qimao album url: {book_url}")
        payload = await self._get(ctx, "https://api-bc.wtzw.com/api/v2/album/detail", {
            "album_id": album_id,
        })
        data = ((payload.get("data") or {}).get("book")) or {}
        tags = "/".join(
            str(tag.get("title") or "") for tag in (data.get("book_tag_list") or []) if isinstance(tag, dict)
        )
        return {
            "sourceId": self.id,
            "name": str(data.get("title") or ""),
            "author": str(data.get("author") or data.get("actors") or ""),
            "bookUrl": f"{_URL_BASE}/qm/album/{album_id}",
            "coverUrl": str(data.get("image_link") or data.get("thumb_image_link") or ""),
            "intro": str(data.get("intro") or ""),
            "kind": tags or "听书",
            "lastChapter": str(data.get("chapter_list_desc") or ""),
            "wordCount": "",
            "tocUrl": f"{_URL_BASE}/qm/album/{album_id}",
            "bookStatus": "completed" if str(data.get("is_over") or "") == "1" else "ongoing",
            "authRequired": False,
            "extra": {"albumId": album_id},
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        album_id = self._album_id(toc_url)
        if not album_id:
            raise ValueError(f"invalid qimao album toc url: {toc_url}")

        # Route 1: real-person album chapters.
        album_payload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/album/chapter-list", {
            "album_id": album_id, "chapter_ver": "0", "source": "0",
        })
        album_data = (album_payload.get("data") or {}).get("chapter_list") or []
        if album_data:
            self._album_mode_cache[album_id] = "album"
            chapters = []
            for index, raw in enumerate(album_data, start=1):
                chapter_id = str(raw.get("id") or raw.get("chapter_id") or "")
                if not chapter_id:
                    continue
                chapters.append({
                    "sourceId": self.id,
                    "index": int(raw.get("index") or index),
                    "title": str(raw.get("title") or raw.get("chapter_title") or f"第{index:02d}集"),
                    "chapterUrl": f"{_URL_BASE}/qm/album-ep/{album_id}/{chapter_id}",
                    "updateTime": "",
                    "isVip": False,
                    "isLocked": False,
                    "extra": {"durationSeconds": raw.get("duration") or 0},
                })
            return chapters

        # Route 2: AI TTS over the novel chapter list (album_id doubles as book id).
        self._album_mode_cache[album_id] = "tts"
        novel_payload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/chapter/chapter-list", {
            "id": album_id, "chapter_ver": "0", "reader_type": "0",
        })
        novel_data = (novel_payload.get("data") or {}).get("chapter_lists") or []
        chapters = []
        for index, raw in enumerate(novel_data, start=1):
            chapter_id = str(raw.get("id") or "")
            if not chapter_id:
                continue
            content_md5 = str(raw.get("content_md5") or "")
            chapters.append({
                "sourceId": self.id,
                "index": int(raw.get("index") or index),
                "title": str(raw.get("title") or f"第{index:02d}集"),
                "chapterUrl": f"{_URL_BASE}/qm/tts/{album_id}/{chapter_id}/{content_md5}",
                "updateTime": "",
                "isVip": False,
                "isLocked": False,
                "extra": {},
            })
        return self._finalize_toc(ctx, chapters)

    def _finalize_toc(self, ctx, chapters: list[dict]) -> list[dict]:
        """应用源设置中的目录顺序（正序/倒序）。"""
        if str(ctx.settings.get("toc_order", "") or "") == "倒序":
            chapters = list(reversed(chapters))
            for position, chapter in enumerate(chapters, start=1):
                chapter["index"] = position
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        route, payload = self._chapter_identity(chapter_url)
        duration = 0.0
        if route == "album-ep":
            album_id, _, tail = payload.partition("/")
            chapter_id = tail.split("?", 1)[0]
            if not album_id or not chapter_id:
                raise ValueError(f"invalid qimao album-ep url: {chapter_url}")
            info = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/album/info", {
                "album_id": album_id, "chapter_id": chapter_id, "new_user": "0",
            })
            voices = (info.get("data") or {}).get("voice_list") or []
            media_url = str(voices[0].get("voice_url") or "") if voices else ""
            duration = self._duration_seconds(voices[0].get("duration")) if voices else 0.0
            media_mime = "audio/mpeg"
        elif route == "tts":
            book_id, _, tail = payload.partition("/")
            rest = tail.split("?", 1)[0]
            chapter_id, _, content_md5 = rest.partition("/")
            if not book_id or not chapter_id:
                raise ValueError(f"invalid qimao tts url: {chapter_url}")
            voice_id = await self._pick_voice_id(ctx, book_id)
            if not voice_id:
                raise RuntimeError(f"qimao tts: no usable voice for book {book_id}")
            preload = await self._get(ctx, "https://api-ks.wtzw.com/api/v1/listen/preload-chapter-list", {
                "book_id": book_id,
                "chapter_id": chapter_id,
                "content_md5": content_md5,
                "voice_id": voice_id,
            })
            tlist = (preload.get("data") or {}).get("chapter_list") or []
            media_url = ""
            if tlist:
                media_url = str(tlist[0].get("voice_url") or tlist[0].get("url") or "")
                duration = self._duration_seconds(tlist[0].get("duration"))
            media_mime = "audio/mpeg"
        else:
            raise ValueError(f"invalid qimao chapter url: {chapter_url}")
        if not media_url:
            raise RuntimeError(f"qimao chapter has no playable audio: {str(chapter_url)[:120]}")
        return {
            "sourceId": self.id,
            "title": "",
            "chapterUrl": chapter_url,
            "content": "",
            "format": "audio",
            "mediaUrl": media_url,
            "mediaType": media_mime,
            "durationSeconds": duration,
            "authRequired": False,
            "isPaid": False,
            "extra": {},
        }
