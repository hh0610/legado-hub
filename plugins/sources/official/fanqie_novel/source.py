"""
番茄小说 LegadoHub 插件

五接口全部走真机 Oracle /fetch 代发（真机 TTNet 栈自动带六神头签名），
不依赖任何镜像站。
Oracle 服务地址：http://127.0.0.1:8767（由 D:\\Dev\\root-munch\\oracle_service.py 启动）

接口契约（黄金样本实测路径）：
  - search          -> /reading/bookapi/search/tab/v        GET
  - detail          -> /reading/bookapi/multi-detail/v       GET  (book_id 逗号分隔)
  - toc             -> /reading/bookapi/directory/all_items/v GET
  - chapter         -> /reading/reader/full/v                  GET  (密文，主动解密)
  - 段评单段       -> /novel/commentapi/comment/list/{chapter_id}/v1  POST (comment_source=2, para_index=N；0=首段正文，标题不占索引)
  - 段评全章映射   -> /reading/ugc/idea/list/v/                        GET  (idea_data)
  - 章评(本章讨论) -> /reading/ugc/item/mix_data/get/v                POST (st=38 入口 → st=51 混合流)

全部走 host: api5-normal-sinfonlineb.fqnovel.com
段评接口需要自定义 header：comment-source / server-channel（2/0）

章节主动解密链路（2026-09-20 A 线路终局，零人工翻章）：
  1. 密钥引导：App 冷启 30-40 秒内自动向 KMS 发起 RegisterKey（零操作），
     Oracle agent 在 DecryptKey 构造时缓存 data.kmskey（即 v2）。
     插件轮询 getlastkey RPC（≤45s）直到拿到 v2；会话内跨书通用。
  2. Oracle /fetch 代发 reader/full -> 拿到密文 JSON（content 字段是 base64 密文，
     注意 crypt_status=0 是烟雾弹，content 永远是随机化密文）。
  3. RPC decode2(content, v2, 1001) -> frida 在 App 主线程调 CM.decrypt
     （唯一安全姿势：App 自己的线程、自己的 CryptManager；非 PC 直调 native）
     -> RPC decodelast 轮询拿 gzip_b64。
  4. 本地 gunzip(gzip_b64) -> XHTML -> _html_to_text -> 纯文本。
  5. 解密失败时重新 getlastkey（App 遇 KEY_TOO_OLD 会自动重注册，
     armDK 钩子同步刷新缓存）重试一次。
  6. 最后兜底 getlastpair（用户恰好在真机翻同一章时的被动捕获产物）。

密钥模型（实测确认）：
  - v2 = RegisterKeyResponse.data.kmskey（64 字符 base64，原样无变换），会话级、跨书通用
  - 跨会话完全轮换；服务端 keyver（如 317207321）随 reader/full 响应下发
  - CM.decrypt 第三参固定 m_ver=1001（不是 keyver；126+ 条 DCRYPT-PAIR 实测 ver 集合={1001}）
  - v1（32hex，由 data.key 经 native 变换）解密用不到
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import quote, urlencode

try:  # 优先用宿主已装的 pycryptodome（更快）；没有则走下方内联纯 Python 实现，
    from Crypto.Cipher import AES          # 保证原版 legado-hub 不装任何依赖也能加载/运行。
    from Crypto.Util.Padding import unpad
except ImportError:  # pragma: no cover - 仅在宿主缺 pycryptodome 时启用
    # ---------------- 内联纯 Python AES-128（ECB/CBC，加解密）+ PKCS7 unpad ----------------
    def _aes_build_sbox():
        p = q = 1; sbox = [0] * 256
        while True:
            p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
            q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xFF
            if q & 0x80: q ^= 0x09
            q &= 0xFF
            x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            sbox[p] = (x ^ 0x63) & 0xFF
            if p == 1: break
        sbox[0] = 0x63; return sbox
    _AES_SBOX = _aes_build_sbox()
    _AES_INV_SBOX = [0] * 256
    for _i, _v in enumerate(_AES_SBOX): _AES_INV_SBOX[_v] = _i

    def _aes_xtime(a):
        a <<= 1
        return (a ^ 0x11B) & 0xFF if a & 0x100 else a & 0xFF

    def _aes_gmul(a, b):
        r = 0
        for _ in range(8):
            if b & 1: r ^= a
            b >>= 1; a = _aes_xtime(a)
        return r & 0xFF

    def _aes_key_expansion(key):
        w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
        Rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]
        for i in range(4, 44):
            t = w[i - 1][:]
            if i % 4 == 0:
                t = [_AES_SBOX[x] for x in (t[1:] + t[:1])]; t[0] ^= Rcon[i // 4 - 1]
            w.append([w[i - 4][j] ^ t[j] for j in range(4)])
        return w

    def _aes_add_rk(s, w, rnd):
        for c in range(4):
            for r in range(4): s[r][c] ^= w[rnd * 4 + c][r]

    def _aes_enc_block(block, w):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        _aes_add_rk(s, w, 0)
        for rnd in range(1, 10):
            s = [[_AES_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
            s = [s[r][r:] + s[r][:r] for r in range(4)]
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _aes_gmul(a[0], 2) ^ _aes_gmul(a[1], 3) ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ _aes_gmul(a[1], 2) ^ _aes_gmul(a[2], 3) ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ _aes_gmul(a[2], 2) ^ _aes_gmul(a[3], 3)
                s[3][c] = _aes_gmul(a[0], 3) ^ a[1] ^ a[2] ^ _aes_gmul(a[3], 2)
            _aes_add_rk(s, w, rnd)
        s = [[_AES_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
        s = [s[r][r:] + s[r][:r] for r in range(4)]
        _aes_add_rk(s, w, 10)
        return bytes(s[r][c] for c in range(4) for r in range(4))

    def _aes_dec_block(block, w):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        _aes_add_rk(s, w, 10)
        for rnd in range(9, 0, -1):
            s = [s[r][(4 - r) % 4:] + s[r][:(4 - r) % 4] for r in range(4)]
            s = [[_AES_INV_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
            _aes_add_rk(s, w, rnd)
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _aes_gmul(a[0], 14) ^ _aes_gmul(a[1], 11) ^ _aes_gmul(a[2], 13) ^ _aes_gmul(a[3], 9)
                s[1][c] = _aes_gmul(a[0], 9) ^ _aes_gmul(a[1], 14) ^ _aes_gmul(a[2], 11) ^ _aes_gmul(a[3], 13)
                s[2][c] = _aes_gmul(a[0], 13) ^ _aes_gmul(a[1], 9) ^ _aes_gmul(a[2], 14) ^ _aes_gmul(a[3], 11)
                s[3][c] = _aes_gmul(a[0], 11) ^ _aes_gmul(a[1], 13) ^ _aes_gmul(a[2], 9) ^ _aes_gmul(a[3], 14)
        s = [s[r][(4 - r) % 4:] + s[r][:(4 - r) % 4] for r in range(4)]
        s = [[_AES_INV_SBOX[s[r][c]] for c in range(4)] for r in range(4)]
        _aes_add_rk(s, w, 0)
        return bytes(s[r][c] for c in range(4) for r in range(4))

    def _aes_ecb(key, data, enc):
        w = _aes_key_expansion(key); fn = _aes_enc_block if enc else _aes_dec_block
        return b"".join(fn(data[i:i + 16], w) for i in range(0, len(data), 16))

    def _aes_cbc_dec(key, iv, data):
        w = _aes_key_expansion(key); out = []; prev = iv
        for i in range(0, len(data), 16):
            blk = data[i:i + 16]
            out.append(bytes(a ^ b for a, b in zip(_aes_dec_block(blk, w), prev))); prev = blk
        return b"".join(out)

    def _aes_cbc_enc(key, iv, data):
        w = _aes_key_expansion(key); out = []; prev = iv
        for i in range(0, len(data), 16):
            blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
            enc = _aes_enc_block(blk, w); out.append(enc); prev = enc
        return b"".join(out)

    def _pkcs7_unpad(data, bs=16):
        if not data or len(data) % bs: raise ValueError("bad padding")
        n = data[-1]
        if n < 1 or n > bs or data[-n:] != bytes([n]) * n: raise ValueError("bad padding")
        return data[:-n]

    class AES:  # 与 pycryptodome 兼容的最小子集（本插件仅用 ECB/CBC 的 new().encrypt/.decrypt）
        MODE_ECB = 1
        MODE_CBC = 2

        @staticmethod
        def new(key, mode, iv=None):
            return _AESCipher(key, mode, iv)

    class _AESCipher:
        def __init__(self, key, mode, iv):
            self._key, self._mode, self._iv = key, mode, iv

        def encrypt(self, data):
            if self._mode == AES.MODE_ECB: return _aes_ecb(self._key, data, True)
            if self._mode == AES.MODE_CBC: return _aes_cbc_enc(self._key, self._iv or b"\x00" * 16, data)
            raise ValueError("unsupported mode for encrypt")

        def decrypt(self, data):
            if self._mode == AES.MODE_ECB: return _aes_ecb(self._key, data, False)
            if self._mode == AES.MODE_CBC: return _aes_cbc_dec(self._key, self._iv or b"\x00" * 16, data)
            raise ValueError("unsupported mode for decrypt")

    def unpad(data, bs=16):  # noqa: F811 - 与上分支同名，兜底实现
        return _pkcs7_unpad(data, bs)
    # ---------------- 内联纯 Python AES 结束 ----------------


# 离线六神签名器（unidbg 跑 libmetasec_ml.so，不依赖真机/frida）。
# 默认飞牛 NAS：http://192.168.31.8:8787 ；可用环境变量 FANQIE_SIGNER_BASE 覆盖。
# 签名器按其内置设备身份（当前=2609）生成六神头；URL 里的 device_id/iid 需与之一致。
SIGNER_BASE = os.environ.get("FANQIE_SIGNER_BASE", "http://192.168.31.5:8787").rstrip("/")

# 六神签名头字段（签名器 /sign 返回 data 里的键）
SIX_HEADER_KEYS = ("X-Argus", "X-Gorgon", "X-Helios", "X-Khronos", "X-Ladon", "X-Medusa")

# 番茄 App UA（与 device 2609 / version 73733 匹配，实测可直发官方 API）
FANQIE_USER_AGENT = (
    "com.dragon.read/73733 (Linux; U; Android 12; zh_CN; 22021211RG) "
    "Cronet/58.0.3029.116"
)

# 离线 registerkey（取会话 kmskey=v2，跨书通用）。reader/full 的 novel_data 也会回传 key，
# 这里作为启动注册 / 刷新备用；设备已注册时服务端可能不回 kmskey，此时回退 novel_data key。
REGISTERKEY_URL = "https://reading.snssdk.com/reading/crypt/registerkey"
REGISTERKEY_AES_KEY = "ac25c67ddd8f38c1b37a2348828e222e"          # kps 加密密钥（实测常量）
REGISTERKEY_INTERNAL_DID = "7682107871840241920"                   # 2609 的内部 deviceId
REGISTERKEY_KEYVER = 1001                                          # RegisterKey 请求的 keyver（实测值）

# ---------------------------------------------------------------------------
# kmskey 持久缓存（按 key_version 索引）
#   番茄密钥模型（实测确认）：
#     - reader/full 只回传 content(密文) + key_version(「用哪版 key」的索引)，
#       **不回传真正的 kmskey**。
#     - 真正的 kmskey 由 registerkey 首次下发，App 存 SharedPreferences，按
#       key_version 缓存；服务端 KEY_TOO_OLD 才重注册换新 key。
#     - 设备已注册时 registerkey 重调会 code=2 不下发，故插件不能依赖每次
#       registerkey，必须自己缓存。
#   注入方式（启动时读环境变量，进程内常驻；registerkey 成功也会写进来）：
#     FANQIE_KMSKEY         = 已知有效的 kmskey（64 位 base64）
#     FANQIE_KMSKEY_VERSION = 该 key 对应的 key_version（缺省作通用兜底 key）
#     FANQIE_KMSKEY_MAP     = JSON {"key_version": "kmskey", ...}（多版本，最高优先）
# ---------------------------------------------------------------------------
_KMSKEY_STORE: dict[str, str] = {}


def _seed_kmskey_store() -> None:
    """从环境变量把已知 kmskey 灌入缓存（启动时执行一次）。"""
    mp = os.environ.get("FANQIE_KMSKEY_MAP", "").strip()
    if mp:
        try:
            obj = json.loads(mp)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k and v:
                        _KMSKEY_STORE[str(k)] = str(v)
        except Exception:
            pass
    single = os.environ.get("FANQIE_KMSKEY", "").strip()
    if single:
        ver = os.environ.get("FANQIE_KMSKEY_VERSION", "").strip() or "__default__"
        _KMSKEY_STORE.setdefault(ver, single)


_seed_kmskey_store()

# 番茄官方 API（全部需要六神头签名，经 Oracle /fetch 代发）
# 注意：实测 host 是 api5-normal-sinfonlineb.fqnovel.com，不是 reading.snssdk.com
API_BASE = "https://api5-normal-sinfonlineb.fqnovel.com"
SEARCH_API = API_BASE + "/reading/bookapi/search/tab/v"
DETAIL_API = API_BASE + "/reading/bookapi/multi-detail/v"
TOC_API = API_BASE + "/reading/bookapi/directory/all_items/v"
CHAPTER_API = API_BASE + "/reading/reader/full/v"
# 评论接口分工（2026-09-22 真机抓包定案）：
#   段评单段：POST /novel/commentapi/comment/list/{chapter_id}/v1
#       comment_source=2 comment_type=1 server_channel=0
#   段评全章映射：GET /reading/ugc/idea/list/v/?book_id&item_id&item_version
#       → data.idea_data{"{k}": {idea_count}}
#   段评单段 comment/list 的 para_index 与 idea key 同构（黄金响应铁证）：
#       k=0/para0=第一个正文段（标题不占 para 索引）
#   阅读器渲染：matchedParagraphIndex = idea key + 1（标题占段落序 0），
#       paragraphId 仍为 idea key（paragraph_say 的 para_index 透传）
#   章评（本章讨论）：同一 endpoint POST /reading/ugc/item/mix_data/get/v 两步：
#       ① query_type=0 source_type=38 item_id=章
#          → forum_data.forum_id + item_related_count（角标数）
#       ② query_type=0 source_type=51 item_id=章 forum_id=①
#          → data.mix_data[]：data_type=3 帖子(post_data)/data_type=4 章内评论(comment)
#          翻页用 next_offset.post_next_offset
#       注意：postdata/list(relative_type=5) 是全书帖子流，不能当本章讨论
#   楼中楼（2026-09-22 真机抓包定案）：
#       章内评论(dt4)/段评评论：GET /reading/ugc/reply/item_detail/v
#         ?source_page=item&group_id=章id&book_id=书id&comment_id=根评论id
#         &service_id=0&offset&count
#       章评帖子(dt3)：GET /reading/ugc/postdata/comment/v?post_id&forum_book_id&offset&count
#       （commentapi/reply/list cs=502 实返 0，已废弃）
COMMENT_API_TEMPLATE = API_BASE + "/novel/commentapi/comment/list/{item_id}/v1"
REPLY_API_TEMPLATE = API_BASE + "/novel/commentapi/reply/list/{comment_id}/v1"
MIX_DATA_API = API_BASE + "/reading/ugc/item/mix_data/get/v"
IDEA_LIST_API = API_BASE + "/reading/ugc/idea/list/v/"
POSTDATA_LIST_API = API_BASE + "/reading/ugc/postdata/list/v"
POSTDATA_COMMENT_API = API_BASE + "/reading/ugc/postdata/comment/v"
# 真机楼中楼（dt4 章内评论 + 段评评论统一端点，2026-09-22 biz_ugc 抓包）
REPLY_ITEM_DETAIL_API = API_BASE + "/reading/ugc/reply/item_detail/v"

# 设备参数（2609，服务端认可的可用身份；与 NAS 签名器内置设备一致）。
# 换设备：同步改这里 + NAS 签名器的 FANQIE_DEVICE_ID/FANQIE_INSTALL_ID。
DEVICE_PARAMS = {
    "iid": "2609602404592746",
    "device_id": "2609602404588650",
    "ac": "wifi",
    "channel": "43536133a",
    "aid": "1967",
    "app_name": "novelapp",
    "version_code": "73733",
    "version_name": "7.3.7.33",
    "device_platform": "android",
    "os": "android",
    "ssmix": "a",
    "device_type": "22021211RG",
    "device_brand": "Xiaomi",
    "language": "zh",
    "os_api": "31",
    "os_version": "12",
    "manifest_version_code": "73733",
    "resolution": "1264*2780",
    "dpi": "480",
    "update_version_code": "73733",
    "cdid": "14643ba3-c839-4cc5-82bc-be82bf933438",
}

# URL 前缀（用于构造可识别的 book_url / chapter_url）
URL_BOOK_PREFIX = "https://fanqie.novel/book/"
URL_CHAPTER_PREFIX = "https://fanqie.novel/chapter/"


# ---------------------------------------------------------------------------
# 离线解密（与 e:\逆向\unidbg_offline\fanqie_decrypt.py 同算法，本地内联避免依赖外部文件）
#   通用派生密钥 K2 不随书籍/会话变化；给定服务端 48 字节 kmskey(base64) 与密文：
#     body_key = AES-128-ECB_dec(K2, K[16:32]) XOR K[0:16]
#     明文     = AES-128-CBC_dec(body_key, ct[16:], iv=ct[:16]) -> 去 PKCS7 -> gzip 流
# ---------------------------------------------------------------------------
_KMS_BODY_K2 = bytes.fromhex("556d6c735a545531575467774d6a4d34")


def _derive_body_key(K: bytes) -> bytes:
    mid = AES.new(_KMS_BODY_K2, AES.MODE_ECB).decrypt(K[16:32])
    return bytes(a ^ b for a, b in zip(mid, K[0:16]))


def _fanqie_decrypt(content_b64: str, kmskey: str):
    """返回 (gzip_plaintext_bytes, ok, err)。"""
    try:
        ct = base64.b64decode(content_b64)
        K = base64.b64decode(kmskey)
    except Exception as exc:
        return None, False, f"b64decode: {exc}"
    if len(ct) < 32 or len(ct) % 16 != 0:
        return None, False, f"bad ciphertext len={len(ct)}"
    if len(K) < 32:
        return None, False, f"bad kmskey len={len(K)}"
    try:
        body_key = _derive_body_key(K)
        pt = AES.new(body_key, AES.MODE_CBC, ct[:16]).decrypt(ct[16:])
        try:
            pt = unpad(pt, 16)
        except ValueError:
            pass
        return pt, True, ""
    except Exception as exc:
        return None, False, f"aes: {exc}"


class Source:
    """番茄小说 LegadoHub 源插件（全离线版）。

    五接口全部走「NAS 离线六神签名器」代签 + 本地直发官方 API，不依赖真机/frida。
    chapter 密文由本地 AES-128-CBC 解密（key 取 reader/full 回传的 novel_data key，
    或离线 registerkey 取得的会话 kmskey），再 gunzip → 纯文本。
    """

    id = "fanqie_novel"
    name = "番茄小说"
    contract_version = "1.0"
    last_modified = "2026-09-27"

    # ------------------------------------------------------------------
    # 离线签名 / 传输层（替代旧 Oracle 代发）
    # ------------------------------------------------------------------

    async def _sign(self, ctx, url: str) -> dict | None:
        """向 NAS 离线签名器要六神头。成功返回 {X-Argus,...}，失败 None。"""
        try:
            resp = await ctx.access.http.fetch_json(
                f"{SIGNER_BASE}/sign?url={quote(url, safe='')}",
                timeout=40,
                proxy=False,
            )
        except Exception as exc:
            ctx.trace("fanqie.sign", url=url, message=f"signer error: {exc}")
            return None
        if not isinstance(resp, dict) or resp.get("code") != 0:
            ctx.trace("fanqie.sign", url=url, message=f"sign failed: {resp}")
            return None
        return resp.get("data") or {}

    async def _signed_fetch(
        self,
        ctx,
        url: str,
        method: str = "GET",
        body: str = "",
        content_type: str = "application/json; charset=utf-8",
        timeout: int = 20,
        headers: dict | None = None,
    ) -> dict:
        """NAS 签名器代签六神头 + 本地直发官方 API。

        返回 {ok, body}（与旧 _signed_fetch 同契约，调用方无需改动）；
        失败返回 {ok: False, error: ...}。
        """
        god = await self._sign(ctx, url)
        if god is None:
            return {"ok": False, "error": "sign unavailable"}
        req_headers = {"User-Agent": FANQIE_USER_AGENT}
        for k in SIX_HEADER_KEYS:
            if god.get(k):
                req_headers[k] = god[k]
        if headers:
            req_headers.update(headers)
        try:
            if method == "GET" or not body:
                text = await ctx.access.http.fetch_text(
                    url, method=method, headers=req_headers,
                    timeout=timeout, proxy=False,
                )
            else:
                # POST body：优先按 JSON 对象发送（服务端按 JSON 解析，空白不敏感）；
                # 解析失败则原样字符串发出。
                try:
                    body_obj = json.loads(body)
                    text = await ctx.access.http.fetch_text(
                        url, method="POST", json=body_obj, headers=req_headers,
                        timeout=timeout, proxy=False,
                    )
                except Exception:
                    text = await ctx.access.http.fetch_text(
                        url, method="POST", data=body, headers=req_headers,
                        timeout=timeout, proxy=False,
                    )
        except Exception as exc:
            return {"ok": False, "error": f"fetch: {exc}"}
        return {"ok": True, "body": text}

    async def _signer_health(self, ctx) -> bool:
        """检查 NAS 离线签名器是否就绪（/health -> {ready:true}）。"""
        try:
            resp = await ctx.access.http.fetch_json(
                f"{SIGNER_BASE}/health", timeout=5, proxy=False,
            )
            return bool(resp and resp.get("ready"))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 离线 registerkey（取会话 kmskey=v2，跨书通用，作启动/刷新备用）
    # ------------------------------------------------------------------

    async def _register_key(self, ctx) -> tuple[str, str]:
        """离线注册拿会话 kmskey（64 字符 base64）。成功返回 (kmskey, key_version)，否则 ("", "")。

        请求格式与 App 冷启 RegisterKey 完全一致（_regkey.py 实测 code=0 拿 key）：
          POST /reading/crypt/registerkey?<设备参数(含 klink_egdi)>
          body = JSON {"keyver": 1001,
                       "content": base64(IV + AES-128-CBC(pad(did(8)+uid(8))))}
          IV = uuid4().hex[:16]（16 字节），AES key = REGISTERKEY_AES_KEY。
        拿到的 key 会由调用方写入 _KMSKEY_STORE，会话内跨书通用，
        因此真机无需预先注入 FANQIE_KMSKEY 环境变量也能解正文。
        """
        try:
            aes_key = bytes.fromhex(REGISTERKEY_AES_KEY)
            internal_did = int(REGISTERKEY_INTERNAL_DID)
            iv = uuid.uuid4().hex[:16].encode("utf-8")          # 16 字节随机 IV
            plain = internal_did.to_bytes(8, "big") + (0).to_bytes(8, "big")
            pad_len = 16 - (len(plain) % 16)                     # PKCS7（plain=16 -> 补 16）
            plain = plain + bytes([pad_len]) * pad_len
            ct = AES.new(aes_key, AES.MODE_CBC, iv=iv).encrypt(plain)
            content = base64.b64encode(iv + ct).decode("ascii")
        except Exception as exc:
            ctx.trace("fanqie.registerkey", message=f"content build error: {exc}")
            return "", ""
        q = dict(DEVICE_PARAMS)
        q["klink_egdi"] = ""
        reg_url = f"{REGISTERKEY_URL}?{self._build_query(q)}"
        god = await self._sign(ctx, reg_url)
        if not god:
            return "", ""
        body = {"keyver": REGISTERKEY_KEYVER, "content": content}
        hdrs = {
            "User-Agent": FANQIE_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
        }
        for k in SIX_HEADER_KEYS:
            if god.get(k):
                hdrs[k] = god[k]
        try:
            text = await ctx.access.http.fetch_text(
                reg_url, method="POST", json=body, headers=hdrs,
                timeout=40, proxy=False,
            )
            data = json.loads(text)
        except Exception as exc:
            ctx.trace("fanqie.registerkey", message=f"http error: {exc}")
            return "", ""
        d = data.get("data") or {}
        kmskey = d.get("kmskey") or ""
        keyver = str(d.get("keyver") or d.get("key_version") or "")
        if data.get("code") == 0 and kmskey:
            ctx.trace("fanqie.registerkey", message=f"got kmskey ver={keyver}")
            return kmskey, keyver
        ctx.trace(
            "fanqie.registerkey",
            message=f"code={data.get('code')} msg={data.get('message')}",
        )
        return "", ""

    # ------------------------------------------------------------------
    # 章节解密（本地离线 AES：prefer novel_data key，回退 registerkey v2）
    # ------------------------------------------------------------------

    def _get_decode_lock(self) -> asyncio.Lock:
        """串行化解密/取 key，避免并发章节重复 registerkey。"""
        lock = getattr(self, "_decode_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._decode_lock = lock
        return lock

    async def _ensure_key(self, ctx, key_version: str = "", *, force_refresh: bool = False) -> str | None:
        """按 key_version 取 kmskey。优先查持久缓存(_KMSKEY_STORE)，未命中才 registerkey。

        key_version 来自 reader/full 响应；缓存里有同版本 key 直接用。
        force_refresh 时跳过缓存、强制 registerkey 拉取（解密失败/换新版本时用）。

        轮换策略（key 会变）：
          - 指定了 key_version 且缓存命中同版本 → 直接用（最准）。
          - 指定了版本但缓存没有 → 认为服务端可能轮换到新版本，**直接 registerkey** 拉当前 key，
            而不是返回旧的过期 key 白跑一次解密。
          - 没指定版本 → 用任一已缓存 key 兜底（同设备短期内版本不变即正确）。
          - registerkey 失败 → 最后才回退到任一缓存 key（由上层 gzip 校验兜住，失败会强刷）。
        """
        kv = str(key_version or "")
        if not force_refresh:
            if kv and kv in _KMSKEY_STORE:
                return _KMSKEY_STORE[kv]
            if not kv and _KMSKEY_STORE:
                return next(iter(_KMSKEY_STORE.values()))
        kmskey, got_ver = await self._register_key(ctx)
        if kmskey:
            store_ver = got_ver or kv or "__default__"
            _KMSKEY_STORE[store_ver] = kmskey
            self._cached_v2 = kmskey
            return kmskey
        if _KMSKEY_STORE:
            return next(iter(_KMSKEY_STORE.values()))
        return None

    def _decrypt_to_html(self, ctx, content_b64: str, kmskey: str) -> str:
        """本地 AES-128-CBC 解密 → gunzip → XHTML。成功返回 HTML，否则空串。"""
        pt, ok, err = _fanqie_decrypt(content_b64, kmskey)
        if not ok:
            ctx.trace("fanqie.decrypt", message=f"aes error: {err}")
            return ""
        if pt[:4] != b"\x1f\x8b\x08\x00":
            ctx.trace("fanqie.decrypt", message=f"gzip magic mismatch: {pt[:4].hex()}")
            return ""
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(pt), mode="rb") as gz:
                return gz.read().decode("utf-8", errors="replace")
        except Exception as exc:
            ctx.trace("fanqie.decrypt", message=f"gunzip error: {exc}")
            return ""

    async def _active_decrypt(self, ctx, content_b64: str, prefer_key: str = "",
                              key_version: str = "") -> str:
        """本地离线解密。prefer_key 为 reader/full 回传的本会话 key（若有则最准）；
        否则按 key_version 查 _KMSKEY_STORE 缓存，再不行 registerkey 拉取。
        失败时强刷一次 registerkey 重试。最终失败抛 RuntimeError。"""
        async with self._get_decode_lock():
            key = prefer_key or await self._ensure_key(ctx, key_version)
            if key:
                html = self._decrypt_to_html(ctx, content_b64, key)
                if html:
                    return html
            ctx.trace("fanqie.decrypt", message="decrypt failed; refreshing key and retrying")
            v2 = await self._ensure_key(ctx, key_version, force_refresh=True)
            if v2:
                html = self._decrypt_to_html(ctx, content_b64, v2)
                if html:
                    return html
            raise RuntimeError("offline decrypt failed (no valid key)")

    def _gunzip_content(self, ctx, gzip_b64: str) -> str:
        """base64 decode + gunzip -> XHTML 正文（解密链路最后一步）。

        实测 DCRYPT-PAIR 样本与 decode2 输出一致：
          base64decode -> gzip magic 1f8b0800 -> gunzip
          -> <?xml...?><!DOCTYPE html>...<h1 class="chapterTitle1">...<p idx="N">...
        """
        try:
            raw = base64.b64decode(gzip_b64)
            if raw[:4] != b"\x1f\x8b\x08\x00":
                ctx.trace(
                    "fanqie.decrypt.gunzip",
                    message=f"gzip magic mismatch: {raw[:4].hex()}",
                )
                return ""
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
                html = gz.read().decode("utf-8", errors="replace")
            return html
        except Exception as exc:
            ctx.trace(
                "fanqie.decrypt.gunzip",
                message=f"gunzip error: {exc}",
            )
            return ""

    def _html_to_text(self, ctx, html: str) -> tuple[str, str]:
        """章节 HTML -> (title, 纯文本)。

        实测格式（DCRYPT-PAIR gzip_b64 gunzip 后）：
          <header></header><article>
            <h1 class="chapterTitle1" idx="10000" p_idx="40000">
              <blk p_idx="10000" e_idx="0" e_order="0">第五 章 练气</blk>
            </h1>
            <p idx="0" p_idx="40000">
              <blk p_idx="0" e_idx="0" e_order="1">"段落内容..."</blk>
            </p>
            ...
          </article><footer></footer>

        注意：所有文字都包在 <blk>...</blk> 内（不是直接在 <h1>/<p> 之间），
        所以正则必须穿透 <blk> 标签取内层文本，不能直接用 [^<]+ 匹配。
        一个 <p> 内可能有多个 <blk>（罕见），全部拼接为一段。
        """
        if not html:
            return "", ""

        # 标题：取 <h1> 内所有 <blk> 文本拼接（h1 通常只有一个 blk）
        title = ""
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
        if h1_match:
            h1_blks = re.findall(r"<blk[^>]*>([^<]*)</blk>", h1_match.group(1))
            title = "".join(b.strip() for b in h1_blks if b.strip())

        # 段落：取每个 <p> 内所有 <blk> 文本拼接为一个段落
        paragraphs: list[str] = []
        for p_match in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.DOTALL):
            p_blks = re.findall(r"<blk[^>]*>([^<]*)</blk>", p_match.group(1))
            para_text = "".join(b.strip() for b in p_blks if b.strip())
            if para_text:
                paragraphs.append(para_text)

        text = "\n\n".join(paragraphs)
        return title, text

    # ------------------------------------------------------------------
    # URL 编码辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_book_url(book_id: str | int) -> str:
        """构造可识别的 book_url（编码 book_id）。"""
        return f"{URL_BOOK_PREFIX}{book_id}"

    @staticmethod
    def _parse_book_id(book_url: str) -> str:
        """从 book_url 解析 book_id。"""
        # 容忍多种格式：https://fanqie.novel/book/123 或纯 book_id
        match = re.search(r"/book/(\w+)", book_url)
        if match:
            return match.group(1)
        # 兜底：URL 末尾的数字段
        match = re.search(r"(\d+)$", book_url)
        return match.group(1) if match else book_url

    @staticmethod
    def _build_chapter_url(item_id: str | int, book_id: str | int = "",
                           item_version: str = "", title: str = "") -> str:
        """构造可识别的 chapter_url（编码 item_id + 可选上下文）。

        chapter_reviews 接口需要 book_id 和 item_version，所以这里把它们也带上。
        格式：https://fanqie.novel/chapter/{item_id}?book_id=X&item_version=Y&title=Z
        """
        url = f"{URL_CHAPTER_PREFIX}{item_id}"
        params = {}
        if book_id:
            params["book_id"] = str(book_id)
        if item_version:
            params["item_version"] = item_version
        if title:
            params["title"] = title
        if params:
            url += "?" + urlencode(params)
        return url

    @staticmethod
    def _parse_item_id(chapter_url: str) -> str:
        """从 chapter_url 解析 item_id（路径段，? 之前）。"""
        # 先剥掉 query
        path = chapter_url.split("?", 1)[0]
        match = re.search(r"/chapter/(\w+)", path)
        if match:
            return match.group(1)
        match = re.search(r"(\d+)$", path)
        return match.group(1) if match else chapter_url

    @staticmethod
    def _parse_chapter_url_params(chapter_url: str) -> dict:
        """从 chapter_url 解析 query 参数（book_id, item_version, title）。"""
        result = {"book_id": "", "item_version": "", "title": ""}
        if "?" not in chapter_url:
            return result
        query = chapter_url.split("?", 1)[1]
        for pair in query.split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            if k in result:
                from urllib.parse import unquote
                result[k] = unquote(v)
        return result

    @staticmethod
    def _build_query(params: dict) -> str:
        """构造 URL query string（值需 URL 编码，中文不能裸传）。"""
        return urlencode(params)

    # ------------------------------------------------------------------
    # 五接口实现
    # ------------------------------------------------------------------

    async def search(self, ctx, keyword: str, page: int) -> list[dict]:
        """搜索接口：reading/bookapi/search/tab/v

        返回结果在 search_tabs 字段（不是直接在 data 里）。
        Oracle /fetch 代发，自动带六神头签名。
        """
        if not await self._signer_health(ctx):
            ctx.trace("fanqie.search", message="oracle offline")
            return []

        # 黄金样本实测参数
        params = dict(DEVICE_PARAMS)
        params["query"] = keyword
        params["count"] = "0"
        params["search_source"] = "1"
        params["bookstore_tab"] = "2"
        params["bookshelf_search_plan"] = "4"
        params["live_room_id"] = "0"
        params["user_is_login"] = "0"
        # page 从 1 开始；offset 翻页
        if page and int(page) > 1:
            params["offset"] = str((int(page) - 1) * 10)
        url = f"{SEARCH_API}?{self._build_query(params)}"

        resp = await self._signed_fetch(ctx, url)
        if not resp.get("ok"):
            ctx.trace("fanqie.search", url=url, message=f"oracle err: {resp.get('error')}")
            return []

        body = resp.get("body") or ""
        if not body:
            ctx.trace("fanqie.search", url=url, message="empty body")
            return []

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            ctx.trace("fanqie.search", url=url, message=f"json parse: {exc}")
            return []

        if data.get("code") != 0:
            ctx.trace(
                "fanqie.search",
                url=url,
                message=f"api code={data.get('code')} msg={data.get('message') or data.get('BaseResp', {}).get('StatusMessage')}",
            )
            return []

        # 实测结构（2026-09 官方 /reading/bookapi/search/tab/v）：
        #   root.search_tabs[N].data[M] = cell
        #   cell.book_id 在 cell 层；书籍字段在 cell.book_data[0]
        #   （book_name/author/abstract/category/creation_status/...）
        # 注意 search_tabs 在响应**顶层**，不在 data 里。
        results: list[dict] = []
        try:
            search_tabs = data.get("search_tabs") or []
            seen_ids: set[str] = set()
            for tab in search_tabs:
                for cell in tab.get("data") or []:
                    book_id = str(cell.get("book_id") or "")
                    book_data_list = cell.get("book_data") or []
                    item = book_data_list[0] if book_data_list else cell
                    if not book_id:
                        book_id = str(item.get("book_id") or "")
                    # 过滤非书籍 cell：banner(show_type=191)、社区入口(539)、
                    # 相关搜索(300) 等无 book_name 的条目
                    name = item.get("book_name") or item.get("title") or ""
                    if not book_id or not name or book_id in seen_ids:
                        continue
                    seen_ids.add(book_id)
                    results.append({
                        "sourceId": self.id,
                        "name": name,
                        "author": item.get("author") or item.get("author_name") or "",
                        "bookUrl": self._build_book_url(book_id),
                        "coverUrl": item.get("thumb_url") or item.get("thumb_uri")
                        or item.get("cover") or "",
                        "intro": item.get("abstract") or item.get("intro") or "",
                        "kind": item.get("category") or item.get("category_name") or "",
                        "wordCount": str(item.get("word_count") or item.get("word_number") or ""),
                        "lastChapter": item.get("last_chapter_name") or "",
                    })
        except Exception as exc:
            ctx.trace("fanqie.search", url=url, message=f"parse error: {exc}")
            return []

        ctx.trace("fanqie.search", url=url, message=f"got {len(results)} results")
        return results

    async def detail(self, ctx, book_url: str) -> dict:
        """书籍详情：reading/bookapi/multi-detail/v

        入参 book_url 编码 book_id，从这里解析出来构造 API URL。
        响应是数组（multi-detail 支持一次查多本书）。
        """
        if not await self._signer_health(ctx):
            return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

        book_id = self._parse_book_id(book_url)
        # multi-detail 接受逗号分隔的 book_id 列表，我们只查一本
        params = dict(DEVICE_PARAMS)
        params["book_id"] = book_id  # 注意：实测是 book_id（单数），不是 book_ids
        params["book_type"] = "0"
        params["source_page"] = "33"
        params["use_shelf_book_type"] = "0"
        params["from"] = "2"
        params["get_related_audio_infos"] = "1"
        url = f"{DETAIL_API}?{self._build_query(params)}"

        resp = await self._signed_fetch(ctx, url)
        if not resp.get("ok"):
            ctx.trace("fanqie.detail", url=url, message=f"oracle err: {resp.get('error')}")
            return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

        try:
            data = json.loads(resp.get("body") or "")
        except json.JSONDecodeError as exc:
            ctx.trace("fanqie.detail", url=url, message=f"json parse: {exc}")
            return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

        if data.get("code") != 0:
            ctx.trace(
                "fanqie.detail",
                url=url,
                message=f"api code={data.get('code')} msg={data.get('message')}",
            )
            return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

        # multi-detail 实测结构：data["data"] 直接是**数组**，data[0] 即本书
        try:
            inner = data.get("data")
            if isinstance(inner, list):
                book = inner[0] if inner else {}
            elif isinstance(inner, dict):
                book_list = inner.get("book_list") or inner.get("data") or []
                book = book_list[0] if isinstance(book_list, list) and book_list else (book_list or {})
            else:
                book = {}
            if not book:
                ctx.trace("fanqie.detail", url=url, message="empty book data")
                return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

            book_name = book.get("book_name") or book.get("title") or ""
            author = book.get("author") or ""
            abstract = book.get("abstract") or ""
            cover = book.get("thumb_url") or book.get("thumb_uri") or book.get("cover") or ""
            category = book.get("category") or book.get("category_name") or ""
            word_count = str(book.get("word_count") or book.get("word_number") or "")
            # creation_status: 1=已完结（实测慈航市 status=1 为完结），其余连载中
            status_val = book.get("creation_status") or book.get("status")
            status = "已完结" if str(status_val) == "1" else "连载中"
            last_chapter = (book.get("last_chapter_title") or book.get("last_chapter_name")
                            or book.get("last_item_name") or "")
            update_time = str(book.get("last_chapter_update_time") or book.get("update_time") or "")

            return {
                "sourceId": self.id,
                "name": book_name,
                "author": author,
                "bookUrl": self._build_book_url(book_id),
                "coverUrl": cover,
                "intro": abstract,
                "kind": category,
                "wordCount": word_count,
                "status": status,
                "lastChapter": last_chapter,
                "updateTime": update_time,
                "tocUrl": self._build_book_url(book_id),
            }
        except Exception as exc:
            ctx.trace("fanqie.detail", url=url, message=f"parse error: {exc}")
            return {"sourceId": self.id, "name": "", "author": "", "bookUrl": book_url}

    async def toc(self, ctx, toc_url: str) -> list[dict]:
        """目录：reading/bookapi/directory/all_items/v

        实测 519 章返回完整目录（all_items 不分页）。
        toc_url 编码 book_id，从中解析。
        """
        if not await self._signer_health(ctx):
            return []

        book_id = self._parse_book_id(toc_url)
        params = dict(DEVICE_PARAMS)
        params["book_id"] = book_id
        params["book_type"] = "0"
        params["need_version"] = "true"
        params["filter_copyright_page"] = "true"
        url = f"{TOC_API}?{self._build_query(params)}"

        resp = await self._signed_fetch(ctx, url)
        if not resp.get("ok"):
            ctx.trace("fanqie.toc", url=url, message=f"oracle err: {resp.get('error')}")
            return []

        try:
            data = json.loads(resp.get("body") or "")
        except json.JSONDecodeError as exc:
            ctx.trace("fanqie.toc", url=url, message=f"json parse: {exc}")
            return []

        if data.get("code") != 0:
            ctx.trace(
                "fanqie.toc",
                url=url,
                message=f"api code={data.get('code')} msg={data.get('message')}",
            )
            return []

        # 实测目录结构：data["data"]["item_data_list"] = [
        #   {item_id, title, version, chapter_word_number, ...}, ...]
        # 版本字段名是 version（不是 item_version）；旧版 item_list 作兜底。
        items: list[dict] = []
        try:
            inner = data.get("data", {}) or {}
            item_list = inner.get("item_data_list") or inner.get("item_list") or []
            for idx, item in enumerate(item_list):
                item_id = str(item.get("item_id") or "")
                if not item_id:
                    continue
                title = item.get("title") or f"第{idx+1}章"
                item_version = str(item.get("version") or item.get("item_version") or "")
                items.append({
                    "sourceId": self.id,
                    "index": idx,
                    "title": title,
                    "chapterUrl": self._build_chapter_url(
                        item_id, book_id=book_id, item_version=item_version, title=title
                    ),
                    "itemId": item_id,
                    "itemVersion": item_version,
                })
        except Exception as exc:
            ctx.trace("fanqie.toc", url=url, message=f"parse error: {exc}")
            return []

        ctx.trace("fanqie.toc", url=url, message=f"got {len(items)} chapters")
        return items

    async def chapter(self, ctx, chapter_url: str) -> dict:
        """章节内容：reading/reader/full/v（密文，主动解密）

        链路：
          1. _ensure_key：轮询 getlastkey 拿 App 冷启自动注册的 v2（≤45s，跨书通用）
          2. Oracle /fetch 代发 reader/full 拿密文 content（crypt_status 是烟雾弹）
          3. _active_decrypt：decode2（App 主线程 CM.decrypt）→ decodelast → gunzip
          4. _html_to_text：XHTML -> 标题 + 段落文本
          5. 失败刷 v2 重试一次；最后 getlastpair 被动捕获兜底。
        """
        if not await self._signer_health(ctx):
            return {
                "sourceId": self.id, "title": "",
                "chapterUrl": chapter_url, "content": "",
                "authRequired": True,
            }

        item_id = self._parse_item_id(chapter_url)
        params = self._parse_chapter_url_params(chapter_url)
        book_id = params["book_id"]

        query = dict(DEVICE_PARAMS)
        query["item_id"] = item_id
        query["key_register_ts"] = "0"
        if book_id:
            query["book_id"] = book_id
        url = f"{CHAPTER_API}?{self._build_query(query)}"

        resp = await self._signed_fetch(ctx, url)
        if not resp.get("ok"):
            ctx.trace("fanqie.chapter", url=url, message=f"oracle err: {resp.get('error')}")
            return {
                "sourceId": self.id, "title": "",
                "chapterUrl": chapter_url, "content": "",
                "authRequired": True,
            }

        try:
            data = json.loads(resp.get("body") or "")
        except json.JSONDecodeError as exc:
            ctx.trace("fanqie.chapter", url=url, message=f"json parse: {exc}")
            return {
                "sourceId": self.id, "title": "",
                "chapterUrl": chapter_url, "content": "",
                "authRequired": True,
            }

        if data.get("code") != 0:
            ctx.trace(
                "fanqie.chapter",
                url=url,
                message=f"api code={data.get('code')} msg={data.get('message')}",
            )
            return {
                "sourceId": self.id, "title": "",
                "chapterUrl": chapter_url, "content": "",
                "authRequired": True,
            }

        # reader/full 响应结构：
        # data["data"]["content"]  -> base64 密文（crypt_status 是烟雾弹，恒为密文）
        # data["data"]["novel_data"] -> book 元数据（abstract, book_id, book_name, author, title, ...）
        # data["data"]["key_version"] -> 服务端密钥版本（不是 CM.decrypt 的 1001）
        try:
            inner = data.get("data", {}) or {}
            content_b64 = inner.get("content") or ""
            key_version = str(inner.get("key_version") or "")
            novel_data = inner.get("novel_data") or {}

            # 从 novel_data 提取元数据
            title_from_meta = novel_data.get("title") or params.get("title") or ""
            book_name = novel_data.get("book_name") or ""
            author = novel_data.get("author") or ""

            # 本会话解密 key：优先取 reader/full 回传的 novel_data.data_list[0].key
            # （实测与内容一一对应、最准）；缺失时 _active_decrypt 回退 registerkey v2。
            novel_key = ""
            try:
                data_list = novel_data.get("data_list") or []
                if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
                    novel_key = data_list[0].get("key") or ""
            except Exception:
                novel_key = ""

            if not content_b64:
                ctx.trace("fanqie.chapter", url=url, message="no content field")
                return {
                    "sourceId": self.id, "title": title_from_meta,
                    "chapterUrl": chapter_url, "content": "",
                    "authRequired": False,
                }

            # 本地离线解密（AES-128-CBC(MD5(key)) → gunzip → 文本）
            try:
                html = await self._active_decrypt(ctx, content_b64, prefer_key=novel_key,
                                                  key_version=key_version)
                title, text = self._html_to_text(ctx, html)
                if not title:
                    title = title_from_meta
                if text:
                    # 缓存正文段落（供段评 matchedText 锚定，0 基 = 番茄 p_idx）
                    self._cache_set_paras(ctx, chapter_url, [
                        p for p in text.split("\n\n") if p.strip()
                    ])
                    return {
                        "sourceId": self.id,
                        "title": title,
                        "chapterUrl": chapter_url,
                        "content": text,
                        "format": "text",
                        "authRequired": False,
                        "isPaid": False,
                        "extra": {
                            "encrypted": True, "decrypted": True,
                            "via": "offline_local_aes",
                            "keySource": ("novel_data" if novel_key
                                          else ("kmskey_cache" if _KMSKEY_STORE else "registerkey")),
                            "bookName": book_name, "author": author,
                        },
                    }
                ctx.trace("fanqie.chapter", url=url,
                          message="offline decrypt produced empty text")
            except RuntimeError as exc:
                ctx.trace("fanqie.chapter", url=url,
                          message=f"offline decrypt failed: {exc}")

            # 全部失败：检查 NAS 签名器(192.168.31.8:8787)与设备 key 是否有效
            return {
                "sourceId": self.id,
                "title": title_from_meta,
                "chapterUrl": chapter_url,
                "content": "",
                "format": "text",
                "authRequired": True,
                "isPaid": False,
                "extra": {
                    "encrypted": True, "decrypted": False,
                    "reason": "decrypt_unavailable",
                    "bookName": book_name, "author": author,
                    "contentB64Len": len(content_b64),
                    "hint": "check NAS signer 192.168.31.8:8787 & device 2609 key",
                },
            }
        except Exception as exc:
            ctx.trace("fanqie.chapter", url=url, message=f"parse error: {exc}")
            return {
                "sourceId": self.id, "title": "",
                "chapterUrl": chapter_url, "content": "",
                "authRequired": True,
            }

    @staticmethod
    def _extract_images(obj: dict) -> tuple[str, str]:
        """从评论对象提取 (imageUrl, imagePreview)。

        覆盖三种实测形态（2026-09-22 golden/真机数据）：
          - commentapi content.image_data_list: [{"expand_web_url",...}] 或
            {"image_data": [...]}；image_type: 1=静态图(.jpeg.heic)、
            2=动图表情(~tplv-...:300:0.awebp)、3=大表情贴图(取 dynamic_url 动图)、None=截图
          - 章内评论平铺: image_url=[完整URL] + image_data=[{web_uri(300px 缩略)}]
          - 楼中楼回复行可能带同名字段（shape 未知，按上面两类兜底）
        URL 含 ':300:0' 的是服务端转码缩略图，作 imagePreview。
        """
        urls_full: list[str] = []
        urls_small: list[str] = []
        content = obj.get("content") if isinstance(obj.get("content"), dict) else {}
        idl = content.get("image_data_list") or obj.get("image_data_list")
        items: list = []
        if isinstance(idl, list):
            items = idl
        elif isinstance(idl, dict):
            items = idl.get("image_data") or [idl]
        for it in items:
            nest = it.get("image_data") if isinstance(it, dict) else None
            cand = nest if isinstance(nest, list) else [it]
            for im in cand:
                if not isinstance(im, dict):
                    continue
                # type=3 大表情贴图：expand_web_url 是静态 cover 帧，dynamic_url 才是动图 gif
                u = str(im.get("dynamic_url") or "") if im.get("image_type") == 3 else ""
                u = u or str(im.get("expand_web_url") or im.get("web_uri") or "")
                if not u:
                    continue
                (urls_small if ":300:0" in u else urls_full).append(u)
        raw_urls = obj.get("image_url")
        if isinstance(raw_urls, list):
            urls_full.extend(str(u) for u in raw_urls if u)
        raw_data = obj.get("image_data")
        if isinstance(raw_data, list):
            for im in raw_data:
                if isinstance(im, dict):
                    u = str(im.get("web_uri") or "")
                    if u:
                        urls_small.append(u)
        image_url = urls_full[0] if urls_full else (urls_small[0] if urls_small else "")
        image_preview = urls_small[0] if urls_small else image_url
        return image_url, image_preview

    @staticmethod
    def _parse_comment_entry(entry: dict) -> dict:
        """解析 commentapi/comment/list 的单条评论结构。"""
        comment = entry.get("comment") or {}
        common = comment.get("common") or {}
        content = common.get("content") or {}
        user_info = (common.get("user_info") or {}).get("base_info") or {}
        stat = comment.get("stat") or {}
        expand = comment.get("expand") or {}
        image_url, image_preview = Source._extract_images(common)
        return {
            "commentId": str(comment.get("comment_id") or ""),
            "content": content.get("text", ""),
            "userName": user_info.get("user_name", ""),
            "userId": str(user_info.get("user_id", "")),
            "userAvatar": user_info.get("user_avatar", ""),
            "createTime": common.get("create_timestamp", 0),
            "diggCount": stat.get("digg_count", 0),
            "replyCount": stat.get("reply_count", 0),
            "paraContent": expand.get("para_src_content", ""),
            "imageUrl": image_url,
            "imagePreview": image_preview,
            "raw": entry,
        }

    # ---- 评论（LegadoHub chapter_reviews 契约）----
    # 段评逐条走 commentapi；章评走 forum 管线（mix_data→forum_id→postdata/list）；
    # 全章气泡映射走 idea/list（1 请求替代逐段盲扫）。
    REVIEW_PARAGRAPH_SCAN = 5  # 从 idea/list 选出评论数最多的前 N 段，补拉 commentapi 热评

    async def _resolve_review_refs(self, ctx, chapter_url: str):
        """从 chapter_url 解析 (item_id, book_id, item_version)，失败返回 None。"""
        if not await self._signer_health(ctx):
            return None
        item_id = self._parse_item_id(chapter_url)
        params = self._parse_chapter_url_params(chapter_url)
        book_id = params["book_id"]
        item_version = params["item_version"]
        if not item_id or not book_id or not item_version:
            ctx.trace(
                "fanqie.reviews",
                message=f"missing refs: item_id={item_id} book_id={book_id} ver={'Y' if item_version else 'N'}",
            )
            return None
        return item_id, book_id, item_version

    async def _fetch_comment_list(
        self, ctx, item_id: str, *,
        comment_source: int, comment_type: int, server_channel: int,
        group_id: str, book_id: str, item_version: str,
        para_index: int = 0, count: int = 20, offset: int = 0,
    ) -> tuple[list, int]:
        """统一调用 commentapi/comment/list，返回 (data_list, total)。"""
        body_obj = {
            "comment_source": comment_source,
            "server_channel": server_channel,
            "group_id": str(group_id),
            "group_type": 15,
            "comment_type": comment_type,
            "sort": 1,
            "business_param": {
                "need_count": True,
                "para_index": para_index,
                "item_version": item_version,
                "fold_type": 1,
                "book_id": str(book_id),
            },
            "count": count,
            "aid": 1967,
            "compliance_status": 0,
        }
        if offset:
            body_obj["offset"] = offset
        url = f"{COMMENT_API_TEMPLATE.format(item_id=item_id)}?{self._build_query(DEVICE_PARAMS)}"
        resp = await self._signed_fetch(
            ctx, url, method="POST",
            body=json.dumps(body_obj, separators=(",", ":")),
            headers={
                "comment-source": str(comment_source),
                "server-channel": str(server_channel),
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        if not resp.get("ok"):
            return [], 0
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            ctx.trace(
                "fanqie.reviews",
                message=f"commentapi code={data.get('code')} msg={data.get('message')}",
            )
            return [], 0
        inner = data.get("data") or {}
        total = int((inner.get("common_list_info") or {}).get("total") or 0)
        return inner.get("data_list") or [], total

    # ---- 章评（本章讨论）mix_data 管线（2026-09-22 真机抓包定案）----
    # 入口：mix_data query_type=0 source_type=38 item_id=章
    #   → data.forum_data.forum_id（书圈 forum）+ data.item_related_count（本章讨论数，即角标）
    # 列表：mix_data query_type=0 source_type=51 item_id=章 forum_id=上一步
    #   → data.mix_data[]：data_type=3 论坛帖子(post_data)，data_type=4 章内评论(comment)
    #   翻页用 data.next_offset.post_next_offset
    # 注意：postdata/list（relative_type=5）返回的是全书帖子流，不能用于本章讨论。

    @staticmethod
    def _mix_zero_offset(post_offset: int = 0) -> dict:
        return {"book_next_offset": 0, "comment_next_offset": 0,
                "post_next_offset": int(post_offset), "self_offset": 0,
                "topic_next_offset": 0}

    async def _mix_data_call(
        self, ctx, body_obj: dict, *, timeout: int = 20,
    ) -> dict | None:
        resp = await self._signed_fetch(
            ctx, f"{MIX_DATA_API}?{self._build_query(DEVICE_PARAMS)}",
            method="POST",
            body=json.dumps(body_obj, separators=(",", ":")),
            timeout=timeout,
        )
        if not resp.get("ok"):
            return None
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            ctx.trace(
                "fanqie.reviews",
                message=f"mix_data code={data.get('code')} msg={data.get('message')}",
            )
            return None
        return data.get("data") or {}

    async def _fetch_chapter_review_entry(
        self, ctx, book_id: str, chapter_id: str,
    ) -> tuple[str, int]:
        """mix_data(st=38) → (forum_id, 本章讨论总数)。"""
        inner = await self._mix_data_call(ctx, {
            "book_id": str(book_id), "count": 20, "forum_id": "",
            "include_other_item_data": False, "item_id": str(chapter_id),
            "offset": self._mix_zero_offset(),
            "query_type": 0, "should_not_impr": True, "source_type": 38,
        })
        if not inner:
            return "", 0
        forum_id = str((inner.get("forum_data") or {}).get("forum_id") or "")
        related = int(inner.get("item_related_count") or 0)
        return forum_id, related

    async def _fetch_chapter_mix_items(
        self, ctx, book_id: str, chapter_id: str, forum_id: str, *,
        offset: int = 0, count: int = 20,
    ) -> tuple[list, bool, int]:
        """mix_data(st=51) → (mix 条目(含 dt3 帖子/dt4 评论), has_more, next_post_offset)。"""
        inner = await self._mix_data_call(ctx, {
            "book_id": str(book_id), "count": count,
            "forum_id": str(forum_id),
            "include_other_item_data": False, "item_id": str(chapter_id),
            "offset": self._mix_zero_offset(offset),
            "query_type": 0, "should_not_impr": True, "source_type": 51,
        })
        if not inner:
            return [], False, offset
        items = inner.get("mix_data") or []
        next_offset = int(((inner.get("next_offset") or {}).get("post_next_offset")) or offset)
        return items, bool(inner.get("has_more")), next_offset

    @staticmethod
    def _chapter_comment_to_review(comment: dict) -> dict:
        """mix_data 里 data_type=4 的章内评论 → review item 契约。

        楼中楼走 ugc/reply/item_detail/v（真机 biz_ugc 抓包定案）。
        """
        user = comment.get("user_info") or {}
        digg = int(comment.get("digg_count") or 0)
        reply = int(comment.get("reply_count") or 0)
        image_url, image_preview = Source._extract_images(comment)
        return {
            "reviewId": str(comment.get("comment_id") or ""),
            "reviewType": "chapter_comment",
            "content": comment.get("text") or "",
            "userName": user.get("user_name") or "",
            "userId": str(user.get("user_id") or ""),
            "avatarUrl": user.get("user_avatar") or "",
            "createTime": int(comment.get("create_timestamp") or 0),
            "diggCount": digg,
            "likeCount": digg,
            "replyCount": reply,
            "commentCount": reply,
            "imageUrl": image_url,
            "imagePreview": image_preview,
        }

    def _mix_item_to_review(self, item: dict) -> dict | None:
        """mix 条目 → review item；非帖子/评论类型（卡片等）返回 None。"""
        data_type = item.get("data_type")
        if data_type == 3 and isinstance(item.get("post_data"), dict):
            return self._post_to_review_item(item["post_data"])
        if data_type == 4 and isinstance(item.get("comment"), dict):
            return self._chapter_comment_to_review(item["comment"])
        return None

    async def _fetch_idea_map(self, ctx, book_id: str, chapter_id: str,
                              item_version: str) -> dict:
        """idea/list → 全章段评映射 {para_index(int): idea_count}。"""
        query = dict(DEVICE_PARAMS)
        query.update({
            "book_id": str(book_id),
            "item_id": str(chapter_id),
            "item_version": str(item_version),
        })
        resp = await self._signed_fetch(ctx, f"{IDEA_LIST_API}?{self._build_query(query)}")
        if not resp.get("ok"):
            return {}
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            ctx.trace(
                "fanqie.reviews",
                message=f"idea/list code={data.get('code')} msg={data.get('message')}",
            )
            return {}
        ideas = ((data.get("data") or {}).get("idea_data")) or {}
        result: dict[int, int] = {}
        for key, val in ideas.items():
            if isinstance(val, dict) and val.get("idea_count"):
                try:
                    result[int(key)] = int(val.get("idea_count") or 0)
                except (TypeError, ValueError):
                    continue
        return result

    @staticmethod
    def _post_to_review_item(post: dict) -> dict:
        """章评帖子 → review item 契约（createTime 秒级，与 commentapi 一致）。"""
        user = post.get("user_info") or {}
        title = (post.get("title") or "").strip()
        content = (post.get("pure_content") or "").strip()
        if title and content and content != title:
            text = f"{title}\n{content}"
        else:
            text = title or content
        digg = int(post.get("digg_cnt") or 0)
        reply = int(post.get("reply_cnt") or 0)
        image_url, image_preview = Source._extract_images(post)
        return {
            "reviewId": str(post.get("post_id") or ""),
            "reviewType": "forum_post",
            "content": text or "（图片/卡片帖）",
            "userName": user.get("user_name") or "",
            "userId": str(user.get("user_id") or ""),
            "avatarUrl": user.get("user_avatar") or "",
            "createTime": int(post.get("create_time") or 0),
            "diggCount": digg,
            "likeCount": digg,
            "replyCount": reply,
            "commentCount": reply,
            "imageUrl": image_url,
            "imagePreview": image_preview,
        }

    @staticmethod
    def _reply_to_review_item(reply: dict) -> dict:
        """楼中楼回复 → review item 契约（兼容 postdata/comment 与 reply/list 两种结构）。"""
        # postdata/comment/v: user_info 平铺 + text
        user = reply.get("user_info") or {}
        text = reply.get("text")
        if text is None:
            # reply/list: Common{user_info{base_info}, content{text}}（jos 用大写 Common）
            common = reply.get("Common") or reply.get("common") or {}
            user = (common.get("user_info") or {}).get("base_info") or user
            text = ((common.get("content") or {}).get("text")) or ""
            stat = reply.get("stat") or {}
            digg = int(stat.get("digg_count") or 0)
            ctime = int(common.get("create_timestamp") or 0)
            rid = reply.get("reply_id") or reply.get("comment_id")
        else:
            digg = int(reply.get("digg_count") or 0)
            ctime = int(reply.get("create_timestamp") or 0)
            rid = reply.get("comment_id") or reply.get("reply_id")
        image_url, image_preview = Source._extract_images(reply)
        return {
            "reviewId": str(rid or ""),
            "content": text or "",
            "userName": user.get("user_name") or "",
            "userId": str(user.get("user_id") or ""),
            "avatarUrl": user.get("user_avatar") or "",
            "createTime": ctime,
            "diggCount": digg,
            "likeCount": digg,
            "likeNum": digg,
            "replyCount": 0,
            "commentCount": 0,
            "imageUrl": image_url,
            "imagePreview": image_preview,
        }

    async def _fetch_post_replies(self, ctx, book_id: str, post_id: str, *,
                                  page: int = 1, page_size: int = 20) -> list:
        """章评帖子(dt3)回复（postdata/comment/v）。"""
        query = dict(DEVICE_PARAMS)
        query.update({
            "post_id": str(post_id),
            "forum_book_id": str(book_id),
            "offset": str((page - 1) * page_size),
            "count": str(page_size),
        })
        resp = await self._signed_fetch(
            ctx, f"{POSTDATA_COMMENT_API}?{self._build_query(query)}",
        )
        if not resp.get("ok"):
            return []
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            return []
        return ((data.get("data") or {}).get("comment")) or []

    async def _fetch_reply_item_detail(
        self, ctx, chapter_id: str, book_id: str, comment_id: str, *,
        page: int = 1, page_size: int = 20,
    ) -> tuple[list, int, bool]:
        """评论类楼中楼（dt4 章内评论 + 段评评论统一端点，真机抓包定案）。

        GET /reading/ugc/reply/item_detail/v
            ?source_page=item&offset&group_id=章id&service_id=0
             &count&book_id=书id&comment_id=根评论id
        返回 (回复列表, total, has_more)；响应字段名做兼容解析。
        """
        query = dict(DEVICE_PARAMS)
        query.update({
            "source_page": "item",
            "offset": str((page - 1) * page_size),
            "group_id": str(chapter_id),
            "service_id": "0",
            "count": str(page_size),
            "book_id": str(book_id),
            "comment_id": str(comment_id),
        })
        resp = await self._signed_fetch(
            ctx, f"{REPLY_ITEM_DETAIL_API}?{self._build_query(query)}",
        )
        if not resp.get("ok"):
            return [], 0, False
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            ctx.trace(
                "fanqie.reviews",
                message=f"reply/item_detail code={data.get('code')} msg={data.get('message')}",
            )
            return [], 0, False
        inner = data.get("data") or {}
        rows: list = []
        for key in ("reply_list", "comment", "comments", "reply_infos",
                    "reply_info_list", "list"):
            cand = inner.get(key)
            if isinstance(cand, list) and cand:
                rows = cand
                break
        total = int(inner.get("total") or inner.get("total_count")
                    or (inner.get("common_info") or {}).get("total") or 0)
        has_more = bool(inner.get("has_more"))
        if not total and rows:
            total = len(rows)
        if not has_more and total:
            has_more = page * page_size < total
        # 服务端偶发 has_more=1 但本页已取全（total<=page*size）：不再下发下一页链接，
        # 避免阅读端滚动时自动拉到空回复页（前端会把空态文案搬进楼层）
        if has_more and total and page * page_size >= total:
            has_more = False
        if not rows:
            return [], 0, False
        return rows, total, has_more

    async def _fetch_comment_replies(self, ctx, chapter_id: str, book_id: str,
                                     comment_id: str, *, page: int = 1,
                                     page_size: int = 20) -> list:
        """段评楼中楼（commentapi/reply/list，comment_source=502）。"""
        body_obj = {
            "business_param": {"book_id": str(book_id), "need_count": True},
            "comment_id": str(comment_id),
            "comment_source": 502,
            "comment_type": 1,
            "count": page_size,
            "group_id": str(chapter_id),
            "group_type": 15,
            "cursor": str((page - 1) * page_size),
        }
        resp = await self._signed_fetch(
            ctx, f"{REPLY_API_TEMPLATE.format(comment_id=comment_id)}?{self._build_query(DEVICE_PARAMS)}",
            method="POST",
            body=json.dumps(body_obj, separators=(",", ":")),
            headers={"comment-source": "502", "server-channel": "0"},
        )
        if not resp.get("ok"):
            return []
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            return []
        return ((data.get("data") or {}).get("reply_list")) or []

    @staticmethod
    def _to_review_item(entry: dict) -> dict:
        """转成 LegadoHub 评论条目契约。

        番茄 comment_id 是 19 位 snowflake（超 JS 2^53），统一用字符串 reviewId，
        不在条目里放大整数 id，避免阅读端 WebView 丢精度。
        """
        c = Source._parse_comment_entry(entry)
        return {
            "reviewId": c["commentId"],
            "content": c["content"],
            "userName": c["userName"],
            "userId": c["userId"],
            "avatarUrl": c["userAvatar"],
            "createTime": c["createTime"],
            "diggCount": c["diggCount"],
            "likeCount": c["diggCount"],
            "replyCount": c["replyCount"],
            "commentCount": c["replyCount"],
            "paraContent": c["paraContent"],
            "imageUrl": c.get("imageUrl", ""),
            "imagePreview": c.get("imagePreview", ""),
        }

    @staticmethod
    def _review_empty(debug: dict | None = None) -> dict:
        return {
            "paragraphs": {},
            "hotParagraphReviews": [],
            "chapterEnd": [],
            "chapterEndHot": [],
            "authorReviews": [],
            "summary": {},
            "debug": debug or {},
        }

    # ---- 段落文本缓存（走宿主 ctx.cache，规范不允许插件自建缓存）----
    @staticmethod
    def _paras_cache_key(chapter_url: str) -> str:
        return f"fanqie_paras::{chapter_url}"

    def _cache_get_paras(self, ctx, chapter_url: str):
        """从宿主缓存取正文段落列表；无 ctx.cache 时返回 None（退化为回源）。"""
        getter = getattr(ctx, "cache_get", None)
        if not callable(getter):
            return None
        try:
            v = getter(self._paras_cache_key(chapter_url))
        except Exception:
            return None
        return v if isinstance(v, list) else None

    def _cache_set_paras(self, ctx, chapter_url: str, paras) -> None:
        setter = getattr(ctx, "cache_set", None)
        if not callable(setter):
            return
        try:
            setter(self._paras_cache_key(chapter_url), list(paras), 600)
        except Exception:
            pass

    async def book_reviews(self, ctx, book_url: str) -> dict:
        """书评（commentapi 书级流）：评分概览番茄无独立端点，回传总量+列表。"""
        book_id = self._parse_book_id(book_url)
        if not book_id:
            raise ValueError(f"invalid fanqie book url: {book_url}")
        body_obj = {
            "comment_source": 1,
            "server_channel": 4,
            "group_id": str(book_id),
            "group_type": 1,
            "comment_type": 2,
            "sort": 1,
            "business_param": {
                "need_count": True,
                "para_index": 0,
                "book_id": str(book_id),
            },
            "count": 20,
            "aid": 1967,
            "compliance_status": 0,
        }
        url = f"{COMMENT_API_TEMPLATE.format(item_id=book_id)}?{self._build_query(DEVICE_PARAMS)}"
        resp = await self._signed_fetch(
            ctx, url, method="POST",
            body=json.dumps(body_obj, separators=(",", ":")),
            headers={
                "comment-source": "1",
                "server-channel": "4",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        if not resp.get("ok"):
            return {"summary": {}, "items": [], "debug": {"error": resp.get("error", "book reviews fetch failed")}}
        data = json.loads(resp.get("body") or "{}")
        if data.get("code") != 0:
            return {"summary": {}, "items": [], "debug": {"error": f"book reviews code={data.get('code')}"}}
        inner = data.get("data") or {}
        info = inner.get("common_list_info") or {}
        total = int(info.get("total") or 0)
        items = []
        for entry in inner.get("data_list") or []:
            parsed = self._parse_comment_entry(entry)
            items.append({
                "id": parsed["commentId"],
                "userName": parsed["userName"],
                "avatar": parsed["userAvatar"],
                "content": parsed["content"],
                "likeCount": parsed["diggCount"],
                "replyCount": parsed["replyCount"],
                "time": str(parsed["createTime"] or ""),
            })
        return {
            "summary": {"peopleCount": str(total)},
            "items": items,
            "debug": {},
        }

    async def chapter_reviews(self, ctx, chapter_url: str) -> dict:
        """聚合评论（正文页一次拉齐）：章评 chapterEnd + 全章段评气泡映射。

        章评 = mix_data(st=38) 入口拿 forum_id/本章讨论数 → mix_data(st=51) 拿
        dt3 帖子 + dt4 章内评论混合流；段评气泡 = idea/list 全章映射，
        评论最多的前 REVIEW_PARAGRAPH_SCAN 段再补 commentapi 热评详情。
        """
        refs = await self._resolve_review_refs(ctx, chapter_url)
        if refs is None:
            return self._review_empty({"error": "oracle offline or missing refs"})
        item_id, book_id, item_version = refs

        # 三路并发：章评入口（forum_id+角标数）/ 章评首页 / 全章段评映射
        async def _chapter_entry_and_first_page():
            forum_id, related = await self._fetch_chapter_review_entry(ctx, book_id, item_id)
            if not forum_id:
                return [], related
            mix_items, _more, _off = await self._fetch_chapter_mix_items(
                ctx, book_id, item_id, forum_id, offset=0, count=20,
            )
            return mix_items, related

        chapter_task = _chapter_entry_and_first_page()
        idea_task = self._fetch_idea_map(ctx, book_id, item_id, item_version)
        try:
            chapter_result = await chapter_task
        except Exception as exc:
            chapter_result = exc
        try:
            idea_map = await idea_task
        except Exception as exc:
            idea_map = exc
        if isinstance(chapter_result, Exception):
            ctx.trace("fanqie.chapter_reviews", message=f"chapter task error: {chapter_result}")
            chapter_result = ([], 0)
        if isinstance(idea_map, Exception):
            ctx.trace("fanqie.chapter_reviews", message=f"idea task error: {idea_map}")
            idea_map = {}
        mix_items, related_total = chapter_result

        # ---- 章评 chapterEnd：dt3 帖子 + dt4 章内评论，按服务端顺序去重 ----
        chapter_end: list[dict] = []
        seen_ids: set[str] = set()
        for mix_item in mix_items:
            review = self._mix_item_to_review(mix_item)
            if not review:
                continue
            rid = review.get("reviewId") or ""
            if rid and rid in seen_ids:
                continue
            seen_ids.add(rid)
            chapter_end.append(review)
        chapter_total = related_total or len(chapter_end)

        # ---- 段评气泡：全章映射 + 重点段热评 ----
        # 索引空间（2026-09-22 黄金响应铁证 say_c1_para0.json）：
        #   idea/list 键 k 与 comment/list para_index 同构，0 起 = 第一个正文段
        #   （para_index=0 返回的 20 条评论 expand.para_src_content 全部等于首段
        #     “滋滋……现在的时间是2030年…”，章标题不占 para 索引）。
        #   阅读器段落序 = [标题(0), 正文段1(1), 正文段2(2) …]（0 基），
        #   实测 paragraphIndex=0 的气泡渲染在标题行。
        # 故：paragraphId = idea key（段评说拉取 para_index 透传，勿改），
        #     matchedParagraphIndex = idea key + 1（标题占 0，正文整体后移一位）。
        hot_paragraphs: list[dict] = []
        paragraphs: dict[str, list] = {}
        # 取正文段落（命中缓存则零开销），作为 matchedText 的权威来源。
        # 评论接口只对预拉的少数段返回 paraContent，其余段必须靠正文原文锚定，
        # 否则 matchedText 为空 → 宿主无法定位 → "本章暂无可定位的段评"。
        paras: list[str] = list(self._cache_get_paras(ctx, chapter_url) or [])
        if not paras:
            try:
                _chap = await self.chapter(ctx, chapter_url)
                paras = [p for p in (_chap.get("content") or "").split("\n\n") if p.strip()]
            except Exception as exc:
                ctx.trace("fanqie.chapter_reviews",
                          message=f"para cache refill error: {exc}")
        if idea_map:
            top_ids = sorted(idea_map, key=lambda k: idea_map[k], reverse=True)
            hot_ids = [i for i in top_ids if idea_map[i] > 0][: self.REVIEW_PARAGRAPH_SCAN]

            async def _one_paragraph(idx: int):
                entries, _total = await self._fetch_comment_list(
                    ctx, item_id,
                    comment_source=2, comment_type=1, server_channel=0,
                    group_id=item_id, book_id=book_id, item_version=item_version,
                    para_index=idx, count=20,
                )
                return idx, entries

            para_results = []
            for _coro in [_one_paragraph(i) for i in hot_ids]:
                try:
                    para_results.append(await _coro)
                except Exception as exc:
                    para_results.append(exc)
            for result in para_results:
                if isinstance(result, Exception):
                    ctx.trace(
                        "fanqie.chapter_reviews",
                        message=f"paragraph task error: {result}",
                    )
                    continue
                idx, entries = result
                paragraphs[str(idx)] = [self._to_review_item(e) for e in entries]
            for idx in top_ids:
                if idea_map[idx] <= 0:
                    continue
                items = paragraphs.get(str(idx)) or []
                # matchedText 权威来源 = 正文第 idx 段（idx 即番茄 p_idx，0 基连续）；
                # 评论接口的 paraContent 仅作兜底（预拉段才有）。保证每段都有值，
                # 宿主才能把气泡锚定到对应段落。
                para_text = (
                    paras[idx] if 0 <= idx < len(paras) else ""
                ) or next(
                    (
                        str(entry.get("paraContent") or "").strip()
                        for entry in items
                        if str(entry.get("paraContent") or "").strip()
                    ),
                    "",
                )
                hot_paragraphs.append({
                    "paragraphId": idx,
                    "matchedParagraphIndex": idx + 1,
                    "matchedParagraphCount": 1,
                    "matchedText": para_text,
                    "paragraphText": para_text,
                    "commentCount": idea_map[idx],
                    "hotCommentCount": len(items),
                    "topReviews": items[:3],
                })

        paragraph_total = sum(idea_map.values())
        summary = {
            "chapterEndCount": chapter_total,
            "totalComments": chapter_total + paragraph_total,
            "paragraphCount": paragraph_total,
        }
        ctx.trace(
            "fanqie.chapter_reviews",
            message=f"got {len(chapter_end)} chapter reviews (badge={chapter_total}) + "
                    f"{len(hot_paragraphs)} hot paragraphs of {len(idea_map)} mapped "
                    f"(paragraph_total={paragraph_total})",
        )
        return {
            "paragraphs": paragraphs,
            "hotParagraphReviews": hot_paragraphs,
            "chapterEnd": chapter_end,
            # 混合流无独立“热门章评”排序，置空避免与 chapterEnd 重复预览
            "chapterEndHot": [],
            "authorReviews": [],
            "summary": summary,
            "debug": {"ok": True, "parsed": True},
        }

    async def paragraph_say(
        self, ctx, chapter_url: str, paragraph_id: int, *,
        page: int = 1, page_size: int = 20, cursor_id: int = 0,
    ) -> dict:
        """单段段评分页（webview 段评说）。

        paragraph_id 即 idea key / comment/list 的 para_index（0=第一个正文段，
        标题不占 para 索引），直接透传；与气泡 matchedParagraphIndex 差 1。
        """
        refs = await self._resolve_review_refs(ctx, chapter_url)
        if refs is None:
            return {
                "comments": [], "totalCount": 0, "hasMore": False,
                "debug": {"error": "refs unavailable"},
            }
        item_id, book_id, item_version = refs
        entries, total = await self._fetch_comment_list(
            ctx, item_id,
            comment_source=2, comment_type=1, server_channel=0,
            group_id=item_id, book_id=book_id, item_version=item_version,
            para_index=int(paragraph_id), count=page_size,
            offset=(page - 1) * page_size,
        )
        return {
            "comments": [self._to_review_item(e) for e in entries],
            "totalCount": total,
            "hasMore": page * page_size < total,
            "paragraphId": int(paragraph_id),
        }

    async def page_hot_reviews(
        self, ctx, chapter_url: str, paragraph_ids: list, *,
        page: int = 1, page_size: int = 20,
    ) -> dict:
        """页热评：多段段评合并（每段取 page_size 条）。"""
        refs = await self._resolve_review_refs(ctx, chapter_url)
        if refs is None:
            return {
                "comments": [], "totalCount": 0, "hasMore": False,
                "debug": {"error": "refs unavailable"},
            }
        item_id, book_id, item_version = refs

        async def _one(pid):
            entries, total = await self._fetch_comment_list(
                ctx, item_id,
                comment_source=2, comment_type=1, server_channel=0,
                group_id=item_id, book_id=book_id, item_version=item_version,
                para_index=int(pid), count=page_size,
                offset=(page - 1) * page_size,
            )
            return pid, [self._to_review_item(e) for e in entries], total

        results = []
        for _coro in [_one(pid) for pid in list(paragraph_ids)[:50]]:
            try:
                results.append(await _coro)
            except Exception as exc:
                results.append(exc)
        comments: list[dict] = []
        total_all = 0
        has_more = False
        for result in results:
            if isinstance(result, Exception):
                continue
            _pid, items, total = result
            total_all += total
            comments.extend(items)
            has_more = has_more or page * page_size < total
        return {"comments": comments, "totalCount": total_all, "hasMore": has_more}

    async def chapter_say(
        self, ctx, chapter_url: str, *, page: int = 1, page_size: int = 20,
    ) -> dict:
        """章评分页（webview 本章说）：mix_data(st=51) 混合流。

        翻页偏移用服务端 post_next_offset（页大小固定时即页首序号）。
        """
        refs = await self._resolve_review_refs(ctx, chapter_url)
        if refs is None:
            return {
                "comments": [], "totalCount": 0, "hasMore": False,
                "debug": {"error": "refs unavailable"},
            }
        item_id, book_id, _item_version = refs
        forum_id, related = await self._fetch_chapter_review_entry(ctx, book_id, item_id)
        if not forum_id:
            return {
                "comments": [], "totalCount": related, "hasMore": False,
                "debug": {"ok": True, "empty": True},
            }
        # mix_data(st=51) 是游标制分页：翻页必须回传上一页的 next_offset.post_next_offset，
        # 不能用 (page-1)*page_size（服务端按原始 mix 条目分页，过滤后对不齐，会打到空页）。
        # 用宿主 ctx.cache 跨调用记住游标（page=1 重置），规范不允许插件自建缓存。
        cursor_key = f"fanqie_say_cursor::{chapter_url}"
        offset = 0
        if page > 1:
            cached = None
            getter = getattr(ctx, "cache_get", None)
            if callable(getter):
                try:
                    cached = getter(cursor_key)
                except Exception:
                    cached = None
            try:
                offset = int(cached) if cached not in (None, "") else (page - 1) * page_size
            except (TypeError, ValueError):
                offset = (page - 1) * page_size
        mix_items, has_more, next_off = await self._fetch_chapter_mix_items(
            ctx, book_id, item_id, forum_id,
            offset=offset, count=page_size,
        )
        comments: list[dict] = []
        seen: set[str] = set()
        for mix_item in mix_items:
            review = self._mix_item_to_review(mix_item)
            if not review:
                continue
            rid = review.get("reviewId") or ""
            if rid and rid in seen:
                continue
            seen.add(rid)
            comments.append(review)
        total = related or 0
        # 服务端 has_more 是游标制权威信号；total(item_related_count)含被过滤的帖子类型，
        # 不可靠，绝不用它 OR 覆盖。仅做防御：本页不足一页则必然无下一页。
        has_more = bool(has_more)
        if has_more and len(comments) < page_size:
            has_more = False
        # 更新游标（基于最终 has_more）：仍有更多则存 next_offset，否则清掉避免下次打空页
        setter = getattr(ctx, "cache_set", None)
        if callable(setter):
            try:
                if has_more:
                    setter(cursor_key, str(int(next_off or offset + page_size)), 600)
                else:
                    setter(cursor_key, "0", 1)
            except Exception:
                pass
        return {
            "comments": comments,
            "totalCount": total,
            "hasMore": has_more,
        }

    async def review_replies(
        self, ctx, chapter_url: str, root_review_id: int, *,
        page: int = 1, page_size: int = 20, cursor_id: int = 0,
    ) -> dict:
        """楼中楼回复：root_review_id 可能是 dt4 章内评论/段评评论/章评帖子。

        真机管线（2026-09-22 biz_ugc 抓包）：
          1) 评论类（dt4 章内评论 + 段评评论，绝大多数情况）：
             GET /reading/ugc/reply/item_detail/v
          2) 空则回落章评帖子(dt3)：postdata/comment/v
          （commentapi/reply/list cs=502 真机实返 0，不再使用）
        """
        refs = await self._resolve_review_refs(ctx, chapter_url)
        if refs is None:
            return {
                "comments": [], "replies": [], "totalCount": 0, "hasMore": False,
                "rootReviewId": str(root_review_id),
                "debug": {"error": "refs unavailable"},
            }
        item_id, book_id, _item_version = refs
        root_id = str(root_review_id)

        # 1) 评论类楼中楼：reply/item_detail/v
        raw_replies, total, has_more = await self._fetch_reply_item_detail(
            ctx, item_id, book_id, root_id, page=page, page_size=page_size,
        )
        via = "reply_item_detail"

        # 2) 回落：dt3 章评帖子 postdata/comment/v
        if not raw_replies:
            raw_replies = await self._fetch_post_replies(
                ctx, book_id, root_id, page=page, page_size=page_size,
            )
            if raw_replies:
                total, has_more, via = len(raw_replies), True, "postdata_comment"

        # 兼容包装层：{comment: {...}} / {reply: {...}} / {reply_info: {...}}
        replies: list = []
        for row in raw_replies:
            if isinstance(row, dict):
                inner = row
                for wrap in ("comment", "reply", "reply_info", "ReplyInfo"):
                    if isinstance(row.get(wrap), dict) and "user_info" not in row \
                            and "Common" not in row and "common" not in row:
                        inner = row[wrap]
                        break
                replies.append(self._reply_to_review_item(inner))

        # review_replies 契约：渲染器读 replies/rootReview/nextCursorId（非 comments）
        return {
            "replies": replies,
            "rootReview": None,
            "totalCount": total or len(replies),
            "hasMore": has_more if replies else False,
            "nextCursorId": None,
            "rootReviewId": root_id,
            "debug": {"ok": True, "via": via},
        }
