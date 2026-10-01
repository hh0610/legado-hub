"""Signed media-stream proxy for audiobook/video chapter playback.

Chapter payloads from media plugins carry upstream audio/video URLs that are
usually hotlink-protected, plain HTTP, or HLS playlists. Readers receive a
signed ``/api/media/stream`` URL instead; the proxy verifies the signature,
checks the upstream host against the plugin's declared domains, applies an
SSRF guard, and streams bytes (with Range passthrough for seeking). HLS
playlists are rewritten so every segment/key/variant URI also goes through
the proxy.

The signature is the authorization for the stream request itself: media
elements (and the Legado app player) cannot attach Bearer headers, so the
signed URL plays the same role as a presigned object-store URL — it is only
minted for authenticated readers via the chapter APIs, is bound to exactly
one upstream URL, and expires.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse, urlencode

import httpx

from app.config import CONFIG_DIR

logger = logging.getLogger(__name__)

MEDIA_STREAM_PATH = "/api/media/stream"
DEFAULT_MEDIA_TTL_SECONDS = 24 * 3600
PLAYLIST_SEGMENT_TTL_SECONDS = 2 * 3600
_STREAM_CHUNK_BYTES = 64 * 1024
_HLS_MIME = "application/vnd.apple.mpegurl"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_M3U8_PATH_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_URI_ATTR_RE = re.compile(r'(URI=")([^"]*)"')

_SECRET_FILE = "media_proxy_secret.key"
_SECRET_BYTES = 32

_SECRET_CACHE: bytes | None = None


class MediaProxyError(Exception):
    """Raised when a media stream request cannot be authorized or served."""

    def __init__(self, message: str, *, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


def _load_secret() -> bytes:
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    path = Path(CONFIG_DIR) / _SECRET_FILE
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            _SECRET_CACHE = bytes.fromhex(raw)
            return _SECRET_CACHE
    except (FileNotFoundError, ValueError):
        pass
    secret = secrets.token_bytes(_SECRET_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.hex() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _SECRET_CACHE = secret
    return secret


def reset_media_proxy_secret_cache() -> None:
    """Test hook: forget the cached signing secret."""
    global _SECRET_CACHE
    _SECRET_CACHE = None


def _encode_payload(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_payload(token: str) -> dict | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sign(payload_token: str) -> str:
    digest = hmac.new(_load_secret(), payload_token.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_media_url(
    upstream_url: str,
    source_id: str,
    *,
    ttl_seconds: int = DEFAULT_MEDIA_TTL_SECONDS,
) -> str:
    """Mint a signed proxy path for one upstream media URL."""
    upstream = str(upstream_url or "").strip()
    if not upstream:
        return ""
    if upstream.startswith(f"{MEDIA_STREAM_PATH}?"):
        return upstream
    payload = {
        "u": upstream,
        "s": str(source_id or ""),
        "e": int(time.time()) + max(60, int(ttl_seconds)),
    }
    token = _encode_payload(payload)
    return f"{MEDIA_STREAM_PATH}?{urlencode({'p': token, 'sig': _sign(token)})}"


def verify_media_token(token: str, signature: str) -> dict:
    """Verify a signed proxy payload; returns {"url", "sourceId"} or raises."""
    if not token or not signature or len(token) > 8192 or len(signature) > 256:
        raise MediaProxyError("媒体地址签名无效")
    expected = _sign(token)
    if not hmac.compare_digest(expected, signature):
        raise MediaProxyError("媒体地址签名无效")
    payload = _decode_payload(token)
    if not payload or "u" not in payload or "e" not in payload:
        raise MediaProxyError("媒体地址载荷无效")
    if int(payload.get("e", 0) or 0) < time.time():
        raise MediaProxyError("媒体地址已过期，请重新打开章节", status_code=410)
    upstream = str(payload.get("u", "") or "")
    if not upstream.startswith(("http://", "https://")):
        raise MediaProxyError("不支持的媒体地址")
    return {"url": upstream, "sourceId": str(payload.get("s", "") or "")}


def plugin_stream_hosts(plugin) -> set[str]:
    """Collect the upstream hosts a plugin's media may be streamed from."""
    metadata = getattr(plugin, "metadata", None)
    hosts: set[str] = set()
    if metadata is None:
        return hosts
    for domain in getattr(metadata, "domains", None) or []:
        normalized = str(domain or "").lower().lstrip(".").rstrip(".")
        if normalized:
            hosts.add(normalized)
    for base_url in getattr(metadata, "base_urls", None) or []:
        hostname = str(urlparse(str(base_url or "")).hostname or "").lower().rstrip(".")
        if hostname:
            hosts.add(hostname)
    content = getattr(metadata, "content", None) or {}
    for domain in content.get("streamDomains") or []:
        normalized = str(domain or "").lower().lstrip(".").rstrip(".")
        if normalized:
            hosts.add(normalized)
    return hosts


def assert_stream_host_allowed(plugin, upstream_url: str) -> None:
    parsed = urlparse(upstream_url)
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    allowed = plugin_stream_hosts(plugin)
    for host in allowed:
        if hostname == host or hostname.endswith("." + host):
            return
    raise MediaProxyError("媒体地址不在书源声明的域名内")


def _assert_public_upstream(url: str) -> None:
    """SSRF guard: every resolved address must be public; only 80/443."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise MediaProxyError("不支持的媒体地址协议")
    hostname = str(parsed.hostname or "")
    if not hostname:
        raise MediaProxyError("媒体地址缺少主机名")
    if parsed.port is not None and parsed.port not in (80, 443):
        raise MediaProxyError("媒体地址端口不被允许")
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise MediaProxyError(f"媒体主机无法解析: {hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise MediaProxyError("媒体地址指向内网，已拒绝")


def _request_headers(upstream_url: str, range_header: str | None) -> dict[str, str]:
    parsed = urlparse(upstream_url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Referer": origin,
        "Accept": "*/*",
    }
    if range_header:
        headers["Range"] = range_header
    return headers


def _is_playlist_url(url: str, content_type: str) -> bool:
    mime = str(content_type or "").lower()
    if "mpegurl" in mime:
        return True
    return bool(_M3U8_PATH_RE.search(urlparse(url).path or ""))


def _absolute_url(uri: str, base_url: str) -> str:
    if uri.startswith(("http://", "https://")):
        return uri
    return urljoin(base_url, uri)


def _rewrite_playlist_line(line: str, playlist_url: str, source_id: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        if "URI=" in stripped:
            return _URI_ATTR_RE.sub(
                lambda match: _rewrite_uri_attr(match, playlist_url, source_id),
                stripped,
            )
        return line
    if stripped.lower().startswith("data:"):
        return line
    absolute = _absolute_url(stripped, playlist_url)
    return sign_media_url(absolute, source_id, ttl_seconds=PLAYLIST_SEGMENT_TTL_SECONDS)


def _rewrite_uri_attr(match: re.Match, playlist_url: str, source_id: str) -> str:
    uri = match.group(2).strip()
    if not uri or uri.lower().startswith("data:"):
        return match.group(0)
    absolute = _absolute_url(uri, playlist_url)
    signed = sign_media_url(absolute, source_id, ttl_seconds=PLAYLIST_SEGMENT_TTL_SECONDS)
    return f'{match.group(1)}{signed}"'


def rewrite_playlist(text: str, playlist_url: str, source_id: str) -> str:
    """Rewrite every reachable URI in an HLS playlist into a signed proxy URL."""
    rewritten = [
        _rewrite_playlist_line(line, playlist_url, source_id)
        for line in text.splitlines()
    ]
    body = "\n".join(rewritten)
    return body + "\n" if body else body


@dataclass
class MediaStreamResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes | None = None
    stream: AsyncIterator[bytes] | None = None
    _closers: list = field(default_factory=list)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self.content is not None:
            yield self.content
            return
        if self.stream is None:
            return
        async for chunk in self.stream:
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        for closer in self._closers:
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.debug("media stream close failed", exc_info=True)
        self._closers.clear()


async def open_media_stream(
    upstream_url: str,
    source_id: str,
    *,
    range_header: str | None = None,
) -> MediaStreamResponse:
    """Open one proxied media response (rewritten playlist or byte stream)."""
    plugin = _get_plugin(source_id)
    if plugin is None or not getattr(plugin.metadata, "enabled", False):
        raise MediaProxyError("书源不存在或已停用")
    assert_stream_host_allowed(plugin, upstream_url)
    _assert_public_upstream(upstream_url)

    headers = _request_headers(upstream_url, range_header)
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=4)
    try:
        if _is_playlist_url(upstream_url, ""):
            response = await client.get(upstream_url, headers=headers)
        else:
            request = client.build_request("GET", upstream_url, headers=headers)
            response = await client.send(request, stream=True)
    except (httpx.HTTPError, socket.gaierror, OSError) as exc:
        await client.aclose()
        raise MediaProxyError(f"媒体获取失败: {exc}", status_code=502) from exc

    if response.status_code >= 400:
        await response.aclose()
        await client.aclose()
        raise MediaProxyError(f"媒体上游返回 {response.status_code}", status_code=502)

    content_type = str(response.headers.get("content-type", "") or "")
    if _is_playlist_url(str(response.url), content_type):
        try:
            text = response.text
        finally:
            await response.aclose()
            await client.aclose()
        rewritten = rewrite_playlist(text, str(response.url), source_id)
        return MediaStreamResponse(
            status_code=200,
            headers={"Content-Type": _HLS_MIME, "Cache-Control": "no-store"},
            content=rewritten.encode("utf-8"),
        )

    out_headers: dict[str, str] = {
        "Content-Type": content_type or "application/octet-stream",
        "Accept-Ranges": str(response.headers.get("accept-ranges", "none")),
    }
    for name in ("content-length", "content-range", "etag", "last-modified"):
        value = response.headers.get(name)
        if value is not None:
            canonical = {"etag": "ETag"}.get(name, name.title())
            out_headers[canonical] = value

    stream_response = response

    async def _close_stream() -> None:
        await stream_response.aclose()
        await client.aclose()

    return MediaStreamResponse(
        status_code=stream_response.status_code,
        headers=out_headers,
        content=None,
        stream=stream_response.aiter_bytes(_STREAM_CHUNK_BYTES),
        _closers=[_close_stream],
    )


def _get_plugin(source_id: str):
    try:
        from app.source_plugins.scheduler import get_plugin_scheduler

        return get_plugin_scheduler()._plugins.get(source_id)
    except Exception:
        return None


def signed_media_fields(result: dict, *, source_id: str, base_api: str = "") -> dict:
    """Build the API response media fields for one chapter result.

    Converts the upstream ``mediaUrl`` into a signed, absolute proxy URL and
    passes through ``mediaType``/``durationSeconds``. Returns empty fields when
    the chapter carries no usable media reference.
    """
    upstream = str(result.get("mediaUrl", "") or "")
    if not upstream:
        return {"mediaUrl": "", "mediaType": "", "durationSeconds": 0.0}
    signed = sign_media_url(upstream, str(source_id or ""))
    absolute = f"{base_api}{signed}" if base_api and signed.startswith("/") else signed
    try:
        duration = float(result.get("durationSeconds", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "mediaUrl": absolute,
        "mediaType": str(result.get("mediaType", "") or ""),
        "durationSeconds": duration,
    }
