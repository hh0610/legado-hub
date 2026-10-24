"""Signed media streaming endpoints.

No session auth here on purpose: ``<audio>``/``<video>`` elements and the
Legado player cannot attach Bearer headers. Authorization is carried by the
HMAC-signed URL minted by the chapter APIs for authenticated readers only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.services.media_proxy import (
    MediaProxyError,
    open_media_stream,
    open_video_concat_stream,
    verify_media_token,
)

router = APIRouter(prefix="/api/media")
logger = logging.getLogger(__name__)


@router.get("/stream")
async def stream_media(request: Request, p: str = "", sig: str = "", concat: str = ""):
    unknown = set(request.query_params.keys()) - {"p", "sig", "concat"}
    repeated = {key for key in ("p", "sig", "concat") if len(request.query_params.getlist(key)) > 1}
    if unknown or repeated or not p or not sig:
        raise HTTPException(status_code=403, detail="媒体地址签名无效")
    try:
        payload = verify_media_token(p, sig)
    except MediaProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    range_header = request.headers.get("range")
    try:
        if concat == "1":
            # HLS 解密拼接模式：把 AES-128 m3u8 拼成单条渐进 TS 流，
            # 供无 HLS 模块的播放内核（legadoC）直接播放。
            media = await open_video_concat_stream(
                payload["sourceId"],
                payload["url"],
            )
        else:
            media = await open_media_stream(
                payload["url"],
                payload["sourceId"],
                range_header=range_header,
            )
    except MediaProxyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("media stream failed for %s: %s", payload["sourceId"], exc)
        raise HTTPException(status_code=502, detail="媒体获取失败") from exc

    media_type = media.headers.pop("Content-Type", "application/octet-stream")
    return StreamingResponse(
        media.aiter_bytes(),
        status_code=media.status_code,
        headers=media.headers,
        media_type=media_type,
        background=BackgroundTask(media.aclose),
    )
