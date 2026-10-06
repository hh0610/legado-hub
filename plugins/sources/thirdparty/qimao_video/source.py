"""七猫短剧书源（qimao_video）。

content.kind: video 媒体书源，api-gw.wtzw.com 短剧/漫剧链路（此前逆向
成果从未实测，本插件首次验证通过，2026-10-01）：
- 搜索：GET api-bc.wtzw.com/search/v1/playlet?wd=&page=
- 详情：GET api-gw.wtzw.com/playlet/api/detail?playlet_id=（含简介/标签/全集 play_list）
- 剧集：同一响应的 data.play_list[]（sort/duration/video_url/video_url_h265）
- 播放：video_url 即最终地址（cdn-vod-playlet.wtzw.com 的 m3u8 或 mp4），
  无需额外签名，由平台媒体代理转发（HLS 播放列表自动重写）。

伪 URL 编码：
- 书：https://api-gw.wtzw.com/qm/playlet/{playlet_id}
- 集：https://api-gw.wtzw.com/qm/ep/{playlet_id}/{sort}
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

_URL_BASE = "https://api-gw.wtzw.com"
_INFO_TTL_SECONDS = 600

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


def _media_mime(url: str) -> str:
    lowered = str(url or "").lower()
    if ".m3u8" in lowered:
        return "application/vnd.apple.mpegurl"
    if ".mp4" in lowered:
        return "video/mp4"
    return ""


class Source:
    id = "qimao_video"
    name = "七猫短剧"
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
        self._play_list_cache: dict[str, tuple[float, list]] = {}

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
    def _playlet_id(book_url: str) -> str:
        text = str(book_url or "")
        marker = "/qm/playlet/"
        if marker in text:
            return text.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]
        return ""

    @staticmethod
    def _episode(chapter_url: str) -> tuple[str, int]:
        text = str(chapter_url or "")
        marker = "/qm/ep/"
        if marker in text:
            tail = text.split(marker, 1)[1].split("?", 1)[0]
            playlet_id, _, sort = tail.partition("/")
            digits = "".join(ch for ch in sort if ch.isdigit())
            return playlet_id, int(digits or 1)
        return "", 1

    async def _play_list(self, ctx, playlet_id: str) -> dict:
        cached = self._play_list_cache.get(playlet_id)
        if cached and time.time() < cached[0]:
            return cached[1]
        payload = await self._get(ctx, f"{_URL_BASE}/playlet/api/info", {"playlet_id": playlet_id})
        data = payload.get("data") or {}
        if not data.get("play_list"):
            raise RuntimeError(f"qimao playlet info empty: {str(payload)[:160]}")
        self._play_list_cache[playlet_id] = (time.time() + _INFO_TTL_SECONDS, data)
        return data

    # ---- 生命周期 ---------------------------------------------------------

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        payload = await self._get(ctx, "https://api-bc.wtzw.com/search/v1/playlet", {
            "wd": keyword, "read_preference": "4", "page": str(page),
            "track_id": self._device["source_uid"] + str(int(time.time() * 1000)),
        })
        data = payload.get("data") or {}
        raw_items = data.get("books") or data.get("list") or data.get("playlets") or []
        if not raw_items and isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    raw_items = value
                    break
        items = []
        for raw in raw_items:
            playlet_id = str(raw.get("playlet_id") or raw.get("id") or "")
            title = str(raw.get("title") or raw.get("original_title") or "")
            if not playlet_id or not title:
                continue
            total = str(raw.get("total_num") or "")
            items.append({
                "sourceId": self.id,
                "name": title,
                "author": "七猫短剧",
                "bookUrl": f"{_URL_BASE}/qm/playlet/{playlet_id}",
                "coverUrl": str(raw.get("image_link") or ""),
                "intro": str(raw.get("intro") or ""),
                "kind": "短剧",
                "lastChapter": f"共 {total} 集" if total else "",
                "wordCount": str(raw.get("play_num") or ""),
                "score": 0,
                "extra": {"playletId": playlet_id},
            })
        return items

    async def detail(self, ctx, book_url: str) -> dict:
        playlet_id = self._playlet_id(book_url)
        if not playlet_id:
            raise ValueError(f"invalid qimao playlet url: {book_url}")
        payload = await self._get(ctx, f"{_URL_BASE}/playlet/api/detail", {
            "playlet_id": playlet_id, "playlet_privacy": "1", "read_preference": "0",
        })
        data = payload.get("data") or {}
        creator = data.get("creator") or {}
        creator_name = (
            creator.get("nickname") or creator.get("name") or ""
            if isinstance(creator, dict) else str(creator)
        )
        kind = "漫剧" if str(data.get("content_type") or "") == "2" else "短剧"
        tags = "/".join(
            str(tag.get("title") or "") for tag in (data.get("playlet_tag_list") or []) if isinstance(tag, dict)
        )
        play_list = data.get("play_list") or []
        return {
            "sourceId": self.id,
            "name": str(data.get("title") or ""),
            "author": str(creator_name or f"七猫{kind}"),
            "bookUrl": f"{_URL_BASE}/qm/playlet/{playlet_id}",
            "coverUrl": str(data.get("image_link") or ""),
            "intro": str(data.get("intro") or ""),
            "kind": "/".join(x for x in (kind, tags) if x),
            "lastChapter": f"共 {data.get('total_num') or len(play_list)} 集",
            "wordCount": str(data.get("play_num") or ""),
            "tocUrl": f"{_URL_BASE}/qm/playlet/{playlet_id}",
            "bookStatus": "completed" if str(data.get("is_over") or "") == "1" else "ongoing",
            "authRequired": False,
            "extra": {"playletId": playlet_id},
        }

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        playlet_id = self._playlet_id(toc_url)
        if not playlet_id:
            raise ValueError(f"invalid qimao playlet toc url: {toc_url}")
        data = await self._play_list(ctx, playlet_id)
        chapters = []
        for position, raw in enumerate(data.get("play_list") or [], start=1):
            if not isinstance(raw, dict):
                continue
            if not (raw.get("video_url") or raw.get("video_url_h265")):
                continue
            sort = raw.get("sort")
            sort = int(sort) if sort is not None and str(sort).strip().isdigit() else position
            chapters.append({
                "sourceId": self.id,
                "index": position,
                "title": f"第{sort}集",
                "chapterUrl": f"{_URL_BASE}/qm/ep/{playlet_id}/{sort}",
                "updateTime": "",
                "isVip": False,
                "isLocked": False,
                "extra": {"durationSeconds": float(raw.get("duration") or 0)},
            })
        return chapters

    async def chapter(self, ctx, chapter_url: str) -> dict:
        playlet_id, sort = self._episode(chapter_url)
        if not playlet_id:
            raise ValueError(f"invalid qimao playlet chapter url: {chapter_url}")
        data = await self._play_list(ctx, playlet_id)
        item = None
        for raw in data.get("play_list") or []:
            if isinstance(raw, dict) and str(raw.get("sort") or "") == str(sort):
                item = raw
                break
        if item is None:
            # fall back to positional lookup (sort may start at 1)
            play_list = [x for x in (data.get("play_list") or []) if isinstance(x, dict)]
            if 1 <= sort <= len(play_list):
                item = play_list[sort - 1]
        if not item:
            raise RuntimeError(f"qimao playlet episode missing: {playlet_id} #{sort}")
        media_url = str(item.get("video_url") or item.get("video_url_h265") or "")
        if not media_url:
            raise RuntimeError(f"qimao playlet episode has no video url: {playlet_id} #{sort}")
        return {
            "sourceId": self.id,
            "title": f"第{sort}集",
            "chapterUrl": chapter_url,
            "content": "",
            "format": "video",
            "mediaUrl": media_url,
            "mediaType": _media_mime(media_url),
            "durationSeconds": float(item.get("duration") or 0),
            "authRequired": False,
            "isPaid": False,
            "extra": {},
        }
