"""Catalog orchestration for direct and aggregate chapter reviews."""

from __future__ import annotations

from typing import Any

from app.services.aggregate_reviews import (
    align_hot_paragraph_reviews,
    empty_aggregate_reviews,
    normalize_hot_paragraph_reviews,
    summarize_reviews,
)
from app.services.aggregate_virtual_source import VIRTUAL_SOURCE_ID, unpack_aggregate_chapter_url
from app.services.library_books import library_books_service
from app.source_plugins.id_codec import decode_chapter_id, encode_chapter_id

QIDIAN_APP_SOURCE_ID = "qidian_com_app"
QIDIAN_WEB_SOURCE_ID = "qidian_com_web"
QIDIAN_SOURCE_IDS = {QIDIAN_APP_SOURCE_ID, QIDIAN_WEB_SOURCE_ID}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _direct_review_response(chapter_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "implemented": True,
        "chapterId": chapter_id,
        "paragraphs": result.get("paragraphs", {}),
        "hotParagraphReviews": result.get("hotParagraphReviews", []),
        "chapterEnd": result.get("chapterEnd", []),
        "chapterEndHot": result.get("chapterEndHot", []),
        "authorReviews": result.get("authorReviews", []),
        "summary": result.get("summary", {}),
        "debug": result.get("debug", {}),
    }


async def _direct_chapter_reviews(scheduler: Any, chapter_id: str) -> dict[str, Any]:
    try:
        source_id, chapter_url = decode_chapter_id(chapter_id)
    except Exception:
        return {
            "implemented": True,
            "chapterId": chapter_id,
            "paragraphs": {},
            "hotParagraphReviews": [],
            "chapterEnd": [],
            "chapterEndHot": [],
            "authorReviews": [],
            "summary": {},
            "debug": {"error": "invalid chapter_id format"},
        }
    result = await scheduler.chapter_reviews(source_id, chapter_url)
    return _direct_review_response(chapter_id, result)


async def _review_operation(
    scheduler: Any,
    operation: str,
    source_id: str,
    chapter_url: str,
    *args: Any,
    **kwargs: Any,
) -> tuple[dict[str, Any], str, str]:
    """Run one review operation with host-owned Qidian App-first fallback."""
    candidates = (
        [QIDIAN_APP_SOURCE_ID, QIDIAN_WEB_SOURCE_ID]
        if source_id in QIDIAN_SOURCE_IDS
        else [source_id]
    )
    last_result: dict[str, Any] = {}
    for index, candidate in enumerate(candidates):
        method = getattr(scheduler, operation)
        result = await method(candidate, chapter_url, *args, **kwargs)
        if not isinstance(result, dict):
            result = {}
        last_result = result
        debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
        if not debug.get("error"):
            if candidate == QIDIAN_APP_SOURCE_ID and source_id == QIDIAN_WEB_SOURCE_ID:
                reason = "qidian_app_preferred"
            elif candidate == QIDIAN_WEB_SOURCE_ID and index > 0:
                reason = "app_failed_web_fallback"
            else:
                reason = "primary_source"
            return result, candidate, reason
    return last_result, candidates[-1], "app_failed_web_fallback" if len(candidates) > 1 else "primary_source"


def _mapped_review_target(chapter_id: str) -> tuple[str, str, str, bool]:
    source_id, chapter_url = decode_chapter_id(chapter_id)
    if source_id != VIRTUAL_SOURCE_ID:
        return source_id, chapter_url, chapter_id, False
    payload = unpack_aggregate_chapter_url(chapter_url)
    mapped_chapter_id = str(payload.get("sourceChapterId") or "")
    if not mapped_chapter_id:
        raise ValueError("source_chapter_missing")
    mapped_source_id, mapped_chapter_url = decode_chapter_id(mapped_chapter_id)
    return mapped_source_id, mapped_chapter_url, mapped_chapter_id, True


async def chapter_reviews(scheduler: Any, chapter_id: str) -> dict[str, Any]:
    """Resolve aggregate mapping, host-owned App-to-Web fallback, and alignment."""
    try:
        source_id, chapter_url = decode_chapter_id(chapter_id)
    except Exception:
        return await _direct_chapter_reviews(scheduler, chapter_id)
    if source_id != VIRTUAL_SOURCE_ID:
        result, actual_source_id, mapping_reason = await _review_operation(
            scheduler,
            "chapter_reviews",
            source_id,
            chapter_url,
        )
        response = _direct_review_response(chapter_id, result)
        response.update({
            "mappedSourceId": actual_source_id,
            "mappingReason": mapping_reason,
        })
        return response

    try:
        payload = unpack_aggregate_chapter_url(chapter_url)
        source_chapter_id = str(payload.get("sourceChapterId") or "")
    except Exception as exc:
        return empty_aggregate_reviews(
            chapter_id=chapter_id,
            mapping_reason=f"invalid_aggregate_chapter:{exc}",
        )
    if not source_chapter_id:
        return empty_aggregate_reviews(
            chapter_id=chapter_id,
            mapping_reason="source_chapter_missing",
        )

    shared_chapter = library_books_service.legado_chapter(chapter_id)
    if shared_chapter is None:
        return empty_aggregate_reviews(
            chapter_id=chapter_id,
            mapped_chapter_id=source_chapter_id,
            mapping_reason="chapter_not_published",
        )
    try:
        mapped_source_id, mapped_chapter_url = decode_chapter_id(source_chapter_id)
    except Exception:
        return empty_aggregate_reviews(
            chapter_id=chapter_id,
            mapped_chapter_id=source_chapter_id,
            mapping_reason="invalid_source_chapter",
        )

    result, mapped_source_id, mapping_reason = await _review_operation(
        scheduler,
        "chapter_reviews",
        mapped_source_id,
        mapped_chapter_url,
    )
    source_chapter_id = encode_chapter_id(mapped_source_id, mapped_chapter_url)

    aggregate_book_id = str(payload.get("aggregateBookId") or "")
    chapter_index = _safe_int(payload.get("index"), 0)
    snapshot_source_ids = [mapped_source_id]
    if mapped_source_id in {"qidian_com_app", "qidian_com_web"}:
        snapshot_source_ids.extend(["qidian_com_app", "qidian_com_web"])
    official_content, snapshot_source_id = library_books_service.source_snapshot_content(
        aggregate_book_id,
        chapter_index,
        snapshot_source_ids,
    )
    hot_reviews = align_hot_paragraph_reviews(
        normalize_hot_paragraph_reviews(result),
        official_content=official_content,
        aggregate_content=str(shared_chapter.get("content") or ""),
    )
    response = {
        "implemented": True,
        "chapterId": chapter_id,
        "mappedChapterId": source_chapter_id,
        "mappedSourceId": mapped_source_id,
        "mappingReason": mapping_reason,
        "paragraphs": result.get("paragraphs", {}),
        "hotParagraphReviews": hot_reviews,
        "chapterEndHot": result.get("chapterEndHot", []),
        "chapterEnd": result.get("chapterEnd", []),
        "authorReviews": result.get("authorReviews", []),
        "summary": dict(result.get("summary", {})) if isinstance(result.get("summary"), dict) else {},
        "debug": {
            **(result.get("debug", {}) if isinstance(result.get("debug"), dict) else {}),
            "aggregate": True,
            "reviewSource": mapping_reason,
            "snapshotSourceId": snapshot_source_id,
        },
    }
    response["summary"].update(summarize_reviews(response))
    return response


async def _paged_review_operation(
    scheduler: Any,
    chapter_id: str,
    operation: str,
    *args: Any,
    page: int = 1,
    page_size: int = 20,
    **kwargs: Any,
) -> dict[str, Any]:
    """Resolve one paged review call through the mapped source and App-first policy."""
    try:
        source_id, chapter_url, _mapped_chapter_id, aggregate = _mapped_review_target(chapter_id)
    except Exception as exc:
        return {
            "implemented": True,
            "chapterId": chapter_id,
            "comments": [],
            "totalCount": 0,
            "hasMore": False,
            "debug": {"error": str(exc)},
        }
    result, actual_source_id, mapping_reason = await _review_operation(
        scheduler,
        operation,
        source_id,
        chapter_url,
        *args,
        page=page,
        page_size=page_size,
        **kwargs,
    )
    debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
    return {
        **result,
        "implemented": True,
        "chapterId": chapter_id,
        "mappedChapterId": encode_chapter_id(actual_source_id, chapter_url),
        "mappedSourceId": actual_source_id,
        "mappingReason": mapping_reason,
        "debug": {**debug, "aggregate": aggregate, "reviewSource": mapping_reason},
    }


async def page_hot_reviews(
    scheduler: Any,
    chapter_id: str,
    paragraph_ids: list[int],
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    result = await _paged_review_operation(
        scheduler,
        chapter_id,
        "page_hot_reviews",
        paragraph_ids,
        page=page,
        page_size=page_size,
    )
    comments = result.get("comments") if isinstance(result.get("comments"), list) else []
    if comments:
        return result
    # 回退：插件未实现 page_hot_reviews（如起点 Web）时，改用章节评论里
    # 已内嵌的 hotParagraphReviews.topReviews（形状与视图渲染字段同构）。
    debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
    if "page_hot_reviews method" not in str(debug.get("error", "")):
        return result
    try:
        reviews = await chapter_reviews(scheduler, chapter_id)
    except Exception:
        return result
    wanted = {int(value) for value in paragraph_ids if isinstance(value, int)}
    comments = []
    for item in reviews.get("hotParagraphReviews") or []:
        if not isinstance(item, dict):
            continue
        try:
            paragraph_id = int(item.get("paragraphId", -1))
        except (TypeError, ValueError):
            continue
        if paragraph_id not in wanted:
            continue
        for entry in item.get("topReviews") or []:
            if isinstance(entry, dict):
                comments.append({
                    **entry,
                    "paragraphId": paragraph_id,
                    "paragraphText": str(item.get("paragraphText", "") or ""),
                })
    comments.sort(key=lambda entry: int(entry.get("likeNum") or 0), reverse=True)
    total = len(comments)
    start = (max(1, int(page or 1)) - 1) * page_size
    return {
        "implemented": True,
        "chapterId": chapter_id,
        "comments": comments[start:start + page_size],
        "totalCount": total,
        "hasMore": start + page_size < total,
        "debug": {"fallback": "hot_paragraph_reviews"},
    }


async def chapter_say(
    scheduler: Any,
    chapter_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    return await _paged_review_operation(
        scheduler,
        chapter_id,
        "chapter_say",
        page=page,
        page_size=page_size,
    )


async def paragraph_reviews(
    scheduler: Any,
    chapter_id: str,
    paragraph_id: int,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    result = await _paged_review_operation(
        scheduler,
        chapter_id,
        "paragraph_reviews",
        paragraph_id,
        page=page,
        page_size=page_size,
    )
    result["paragraphId"] = paragraph_id
    return result


async def review_replies(
    scheduler: Any,
    chapter_id: str,
    root_review_id: int,
    *,
    page: int = 1,
    page_size: int = 20,
    cursor_id: int = 0,
) -> dict[str, Any]:
    result = await _paged_review_operation(
        scheduler,
        chapter_id,
        "review_replies",
        root_review_id,
        page=page,
        page_size=page_size,
        cursor_id=cursor_id,
    )
    result["rootReviewId"] = str(root_review_id)
    return result
