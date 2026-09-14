"""Private chapter reviews for Qidian (qidian_com_web).

Implements ReviewsContract:
  - async chapter_reviews(ctx, chapter_url)

Uses Qidian mobile API:
  - reviewsummary4m  -> paragraph list + total count
  - reviewlist4m     -> review details per paragraph / chapter-end
  - getchapterendcomments -> chapter-end reviews with user titles (badges)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse


MOBILE_BASE = "https://m.qidian.com"

# Limit concurrent review-page requests to avoid hammering Qidian and to keep
# memory/response sizes reasonable.
_REVIEW_PAGE_CONCURRENCY = 6


def _headers(referer: str = "") -> dict:
    return {"Referer": referer or f"{MOBILE_BASE}/"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_time(value: Any) -> str:
    """Format timestamp/string into a readable time string."""
    if not value:
        return ""
    # If it's a millisecond timestamp string/number
    try:
        ts = int(value)
        if ts > 10**11:  # milliseconds
            ts = ts // 1000
        from datetime import datetime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    # Otherwise return as-is (e.g. "2025-09-11 14:29:40")
    return str(value)


def _normalize_review_titles(
    item: dict[str, Any],
    *,
    related: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    """Preserve Web title labels and artwork when the endpoint returns them."""
    title_list = (
        item.get("RelatedTitleInfoList") or item.get("relatedTitleInfoList") or []
        if related
        else item.get("TitleInfoList") or item.get("titleInfoList") or []
    )
    if not isinstance(title_list, list):
        return [], []

    badges: list[str] = []
    titles: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for title in title_list:
        if not isinstance(title, dict):
            continue
        name = str(title.get("TitleName") or title.get("titleName") or "").strip()
        image = str(title.get("TitleImage") or title.get("titleImage") or "").strip()
        key = (name, image)
        if not any(key) or key in seen:
            continue
        normalized: dict[str, str] = {}
        if name:
            normalized["name"] = name
            if name not in badges:
                badges.append(name)
        if image:
            normalized["image"] = image
        titles.append(normalized)
        seen.add(key)
    return badges, titles


def _normalize_review_media(item: dict[str, Any]) -> dict[str, Any]:
    """Extract confirmed image/GIF fields shared by Web payload variants."""
    gif_attrs = item.get("GifAttrs") or item.get("gifAttrs") or {}
    if not isinstance(gif_attrs, dict):
        gif_attrs = {}
    image_url = str(item.get("ImageDetail") or item.get("imageDetail") or "").strip()
    image_preview = str(
        item.get("PreImage")
        or item.get("preImage")
        or item.get("imagePre")
        or gif_attrs.get("PreUrl")
        or gif_attrs.get("preUrl")
        or ""
    ).strip()
    has_image = bool(
        item.get("ImgInfo")
        or item.get("imgInfo")
        or item.get("hasImage")
        or image_url
        or image_preview
    )
    result: dict[str, Any] = {"hasImage": has_image}
    if image_url:
        result["imageUrl"] = image_url
    if image_preview:
        result["imagePreview"] = image_preview
    return result


def _normalize_review_emoticons(item: dict[str, Any], content: str) -> list[dict[str, str]]:
    """Keep official emoticon IDs without guessing an asset URL template."""
    emoticons: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append(kind: str, value: Any) -> None:
        identifier = str(value or "").strip()
        key = (kind, identifier)
        if not identifier or identifier == "0" or key in seen:
            return
        emoticons.append({"type": kind, "id": identifier})
        seen.add(key)

    for code in re.findall(r"\[fn=(\d+)\]", content):
        append("inline", code)
    append("face", item.get("FaceId") or item.get("faceId"))
    append("meme", item.get("BigmemeId") or item.get("bigmemeId") or item.get("bigMemeId"))
    append("ugcMeme", item.get("UgcmemeId") or item.get("ugcmemeId") or item.get("ugcMemeId"))
    return emoticons


def _csrf_token_from_ctx(ctx) -> str:
    """Extract _csrfToken from ctx cookies."""
    if not ctx:
        return ""
    for domain in ("m.qidian.com", "qidian.com", "www.qidian.com", "yuewen.com"):
        token = ctx.cookies.get(domain, "_csrfToken")
        if token:
            return str(token)
    return ""


def _extract_author_content(normalized: dict, item: dict) -> None:
    """Preserve author note/reply content if the API returns it as a dict/string."""
    for key in ("authorReview", "AuthorReviewStatus", "authorReply"):
        raw = item.get(key)
        if not raw:
            continue
        if isinstance(raw, dict):
            content = raw.get("content") or raw.get("Content") or ""
            if content:
                normalized["authorReviewContent"] = str(content)
                normalized["authorReview"] = True
                break
        elif isinstance(raw, str) and raw.strip():
            normalized["authorReviewContent"] = raw.strip()
            normalized["authorReview"] = True
            break


def _normalize_review(item: dict, paragraph_id: Any, include_replies: bool = False) -> dict:
    """Normalize a review item from reviewlist4m or getchapterendcomments."""
    # User info may be nested
    user_info = item.get("userInfo") if isinstance(item.get("userInfo"), dict) else {}

    # IDs
    review_id = (
        item.get("reviewId")
        or item.get("id")
        or item.get("Id")
        or item.get("commentId")
        or ""
    )
    user_id = (
        item.get("userId")
        or item.get("UserId")
        or user_info.get("userId")
        or user_info.get("UserId")
        or ""
    )

    # Names: reviewlist4m uses nickName; getchapterendcomments uses UserName
    user_name = (
        item.get("nickName")
        or item.get("userName")
        or item.get("UserName")
        or user_info.get("nickName")
        or user_info.get("userName")
        or ""
    )

    # Like / reply counts
    like_num = _safe_int(
        item.get("likeCount")
        or item.get("LikeCount")
        or item.get("AgreeAmount")
        or item.get("agreeAmount")
        or item.get("likeNum")
        or 0
    )
    reply_count = _safe_int(
        item.get("rootReviewReplyCount")
        or item.get("ReplyCount")
        or item.get("reviewCount")
        or item.get("ReviewCount")
        or item.get("replyCount")
        or 0
    )

    # Timestamps
    create_time = (
        item.get("createTime")
        or item.get("CreateTime")
        or item.get("timestamp")
        or item.get("TimeStamp")
        or ""
    )
    update_time = item.get("updateTime") or item.get("UpdateTime") or ""
    review_time = _format_time(update_time or create_time)

    # User level
    level = _safe_int(
        item.get("level") or item.get("Level") or user_info.get("level") or 0
    )

    # IP location
    ip_address = (
        item.get("ipAddress")
        or item.get("IpAddress")
        or item.get("ipLocation")
        or item.get("IpLocation")
        or ""
    )

    badges, titles = _normalize_review_titles(item)
    _, related_titles = _normalize_review_titles(item, related=True)
    content = str(item.get("content") or item.get("Content") or "")

    # Replies (only for reviewlist4m; limited list returned by API)
    replies = []
    if include_replies:
        raw_replies = item.get("replyList") or []
        if isinstance(raw_replies, list):
            replies = [
                _normalize_review(reply, paragraph_id, include_replies=False)
                for reply in raw_replies
                if isinstance(reply, dict)
            ]

    normalized: dict[str, Any] = {
        "id": str(review_id),
        "userId": str(user_id),
        "content": content,
        "userName": str(user_name),
        "likeNum": like_num,
        "replyCount": reply_count,
        "createTime": str(create_time),
        "reviewTime": review_time,
        "paragraphId": _safe_int(paragraph_id),
        "level": level,
        "ipAddress": str(ip_address),
        "isTop": bool(item.get("isTop") or item.get("TopStatus")),
        "essenceStatus": bool(item.get("essenceStatus") or item.get("EssenceType")),
        "authorReview": bool(item.get("authorReview") or item.get("AuthorReviewStatus")),
        "authorReply": bool(item.get("authorReply")),
        "badges": badges,
        "titles": titles,
        "relatedTitles": related_titles,
        "position": str(item.get("ShowTag") or item.get("showTag") or ""),
        "relatedPosition": str(item.get("RelatedShowTag") or item.get("relatedShowTag") or ""),
        **_normalize_review_media(item),
    }
    emoticons = _normalize_review_emoticons(item, content)
    if emoticons:
        normalized["emoticons"] = emoticons

    # Preserve author note/reply content when the API returns structured objects.
    _extract_author_content(normalized, item)

    avatar = (
        item.get("avatar")
        or item.get("Avatar")
        or item.get("UserHeadIcon")
        or item.get("userHeadIcon")
        or user_info.get("avatar")
        or ""
    )
    if avatar:
        normalized["avatar"] = str(avatar)
    avatar_frame = item.get("FrameUrl") or item.get("frameUrl") or ""
    if avatar_frame:
        normalized["avatarFrame"] = str(avatar_frame)

    if replies:
        normalized["replies"] = replies

    return normalized


async def chapter_reviews(ctx, chapter_url: str) -> dict:
    """Fetch chapter reviews (paragraph + chapter-end) for a Qidian chapter.

    Returns:
        {
            "paragraphs": {
                "0": [{"id": "", "content": "", "userName": "", "likeNum": 0, ...}],
                ...
            },
            "chapterEnd": [{...}],
            "summary": {
                "totalParagraphs": 10,
                "totalReviews": 156,
                "paragraphsWithReviews": [0, 1, 5],
                "paragraphStats": {"0": 12, "1": 5},
            },
        }
    """
    # Parse book_id and chapter_id from URL
    parsed = urlparse(chapter_url)
    path_match = re.search(r"/chapter/(\d+)/(\d+)/", parsed.path)
    if not path_match:
        return {
            "paragraphs": {},
            "chapterEnd": [],
            "summary": {},
            "debug": {"error": "无法解析章节URL"},
        }

    book_id = path_match.group(1)
    chapter_id = path_match.group(2)

    # Get cookies from context
    cookies: dict[str, str] = {}
    for domain in ("qidian.com", "www.qidian.com", "m.qidian.com", "yuewen.com"):
        jar = ctx.cookies.get(domain)
        if isinstance(jar, dict):
            cookies.update(jar)

    has_login = bool(cookies.get("ywguid") and cookies.get("ywkey"))
    if not has_login:
        # Some review APIs still work for public comments with just _csrfToken;
        # attempt the fetch and let the API decide.
        ctx.trace("qidian.reviews", url=chapter_url, message="login markers missing; trying public fetch")

    csrf_token = _csrf_token_from_ctx(ctx)
    if not csrf_token:
        return {
            "paragraphs": {},
            "chapterEnd": [],
            "summary": {},
            "debug": {"error": "缺少 _csrfToken，无法获取本章说"},
        }

    headers = _headers(f"{MOBILE_BASE}/chapter/{book_id}/{chapter_id}/")

    try:
        # Step 1: Get review summary (paragraph list + authoritative total)
        summary_data = await _fetch_review_summary(ctx, book_id, chapter_id, csrf_token, headers)
        summary_payload = (summary_data.get("data") or {}) if isinstance(summary_data, dict) else {}
        paragraph_list = summary_payload.get("list", []) if isinstance(summary_payload, dict) else []
        if not isinstance(paragraph_list, list):
            paragraph_list = []

        # Authoritative total from summary API (includes all paragraphs + chapter-end)
        total_reviews = _safe_int(summary_payload.get("total", 0), 0)
        enable_review = _safe_int(summary_payload.get("enableReview", 1), 1)

        # Build paragraph list from summary.
        # reviewsummary4m returns reviewNum=0 for most paragraphs, even when they
        # actually have reviews. The paragraphId list itself is authoritative, so
        # we query every paragraph ID present in the summary (excluding -1 which
        # represents chapter-end).
        paragraph_stats: dict[str, int] = {}
        hot_paragraph_ids: list[int] = []
        paragraphs_with_reviews: list[int] = []
        for para in paragraph_list:
            if not isinstance(para, dict):
                continue
            pid = _safe_int(para.get("paragraphId"), -2)
            if pid < 0:
                continue
            review_num = _safe_int(para.get("reviewNum", para.get("count", 0)), 0)
            is_hot = bool(
                para.get("isHotSegment")
                or para.get("isHotComment")
                or para.get("isHotReview")
                or para.get("hot")
            )
            paragraph_stats[str(pid)] = review_num
            # Keep hot flag in stats for sorting/prioritization.
            if is_hot:
                paragraph_stats[str(pid)] = max(review_num, 1)
                hot_paragraph_ids.append(pid)
            paragraphs_with_reviews.append(pid)

        # Step 2: Fetch chapter-end reviews from two complementary endpoints.
        # - getchapterendcomments returns badge-rich "hot" reviews, but only
        #   the first ~2 items (it ignores pagination). Use it for the hot list.
        # - reviewlist4m with paragraphId=-1 returns the real paginated chapter-end
        #   list. Use it as the main "本章说" source.
        chapter_end_hot = await _fetch_chapter_end_comments(
            ctx, book_id, chapter_id, csrf_token, headers, max_pages=1
        )
        chapter_end, chapter_end_reviewlist_total = await _fetch_chapter_end_reviews(
            ctx, book_id, chapter_id, csrf_token, headers, max_pages=5
        )
        # Sort by likes descending to match Qidian's hot-comment order.
        chapter_end.sort(key=lambda r: r.get("likeNum", 0), reverse=True)
        # Remove chapter-end reviews that already appear in the hot list so users
        # don't see the same review twice across the two sections.
        hot_ids = {r.get("id") for r in chapter_end_hot if r.get("id")}
        chapter_end = [r for r in chapter_end if r.get("id") not in hot_ids]

        # Step 3: Fetch only explicitly hot paragraphs for their preview cards.
        # Ordinary paragraph lists are loaded later through paragraph_say.
        paragraphs: dict[str, list] = {}
        fetched_pids: list[int] = []
        paragraph_totals: dict[str, int] = {}
        hot_paragraph_comments: dict[str, list] = {}

        if hot_paragraph_ids:
            paragraph_sem = asyncio.Semaphore(_REVIEW_PAGE_CONCURRENCY)

            async def _fetch_one_paragraph(pid: int) -> tuple[int, list, int]:
                async with paragraph_sem:
                    reviews, total = await _fetch_paragraph_reviews(
                        ctx, book_id, chapter_id, pid, csrf_token, headers, max_pages=1
                    )
                    return pid, reviews or [], total

            paragraph_results = await asyncio.gather(
                *[_fetch_one_paragraph(pid) for pid in sorted(set(hot_paragraph_ids))]
            )
            for pid, reviews, total in paragraph_results:
                paragraph_totals[str(pid)] = total
                if reviews:
                    hot_paragraph_comments[str(pid)] = reviews
                    fetched_pids.append(pid)

        hot_paragraph_reviews: list[dict[str, Any]] = []
        for pid in sorted(set(hot_paragraph_ids)):
            reviews = hot_paragraph_comments.get(str(pid), [])
            top_reviews = sorted(
                (dict(review) for review in reviews if isinstance(review, dict)),
                key=lambda review: _safe_int(review.get("likeNum"), 0),
                reverse=True,
            )[:3]
            comment_count = max(paragraph_totals.get(str(pid), 0), len(reviews))
            hot_paragraph_reviews.append(
                {
                    "paragraphId": pid,
                    "paragraphText": "",
                    "commentCount": comment_count,
                    "hotCommentCount": comment_count,
                    "totalCommentCount": comment_count,
                    "topReviews": top_reviews,
                }
            )

        # Step 4: Identify author-initiated notes/replies if any. For now this
        # surfaces reviews that the author has explicitly engaged with; true
        # "作家说" may require a separate endpoint once discovered.
        author_reviews: list = []
        for reviews in hot_paragraph_comments.values():
            for r in reviews:
                if r.get("isAuthor") or r.get("authorReview") or r.get("authorReply"):
                    author_reviews.append(r)
        for r in chapter_end:
            if r.get("isAuthor") or r.get("authorReview") or r.get("authorReply"):
                author_reviews.append(r)

        # VIP detection: when logged in but the review summary disables reviews
        # (enableReview=0) or the full chapter-end list is empty while hot
        # comments still exist, the chapter is likely VIP-locked / unsubscribed.
        vip_hint = ""
        if has_login:
            if enable_review == 0:
                vip_hint = "登录态有效，但本章评论未开启（可能为 VIP/订阅章节未购买）"
            elif chapter_end_reviewlist_total == 0 and len(chapter_end_hot) > 0:
                vip_hint = "登录态有效，但本章完整评论受 VIP/订阅限制，仅返回章末热评"

        summary = {
            "totalParagraphs": len(paragraphs_with_reviews),
            "totalReviews": total_reviews,
            "paragraphsWithReviews": sorted(paragraphs_with_reviews),
            "fetchedParagraphs": fetched_pids,
            "paragraphStats": paragraph_stats,
            "chapterEndCount": len(chapter_end),
            "chapterEndHotCount": len(chapter_end_hot),
            "chapterEndReviewlistTotal": chapter_end_reviewlist_total,
            "authorReviewCount": len(author_reviews),
            "hotParagraphCount": len(hot_paragraph_reviews),
            "vipHint": vip_hint,
        }
        debug: dict[str, Any] = {
            "source": "private",
            "hasLogin": has_login,
        }

        result: dict[str, Any] = {
            "paragraphs": paragraphs,
            "chapterEnd": chapter_end,
            "chapterEndHot": chapter_end_hot,
            "authorReviews": author_reviews,
            "hotParagraphReviews": hot_paragraph_reviews,
            "summary": summary,
        }
        if debug:
            result["debug"] = debug
        return result

    except Exception as exc:
        return {
            "paragraphs": {},
            "chapterEnd": [],
            "chapterEndHot": [],
            "authorReviews": [],
            "hotParagraphReviews": [],
            "summary": {},
            "debug": {"error": str(exc)},
        }


async def _fetch_review_summary(ctx, book_id: str, chapter_id: str, csrf_token: str, headers: dict) -> dict:
    """Call reviewsummary4m API."""
    return await ctx.access.http.fetch_json(
        f"{MOBILE_BASE}/webcommon/chapterreview/reviewsummary4m",
        params={"bookId": book_id, "chapterId": chapter_id, "_csrfToken": csrf_token},
        headers=headers,
    )


async def _fetch_chapter_end_comments(
    ctx, book_id: str, chapter_id: str, csrf_token: str, headers: dict, *, max_pages: int = 1
) -> list:
    """Fetch chapter-end reviews via getchapterendcomments (rich titles/badges).

    Falls back silently if the endpoint is unavailable or returns no data.
    Pages are fetched concurrently to reduce latency.

    NOTE: this endpoint currently ignores the `page` parameter and returns the
    same first ~2 items for every page. We therefore default to `max_pages=1`
    and use it only for badge-rich "hot" chapter-end reviews. The full
    chapter-end list should come from `reviewlist4m` (paragraphId=-1).
    """
    url = f"{MOBILE_BASE}/webcommon/review/getchapterendcomments"
    page_size = 20

    def _normalize(r: Any) -> Any:
        return _normalize_review(r, -1, include_replies=True) if isinstance(r, dict) else None

    try:
        first = await ctx.access.http.fetch_json(
            url,
            params={
                "bookId": book_id,
                "chapterId": chapter_id,
                "page": 1,
                "pageSize": page_size,
                "_csrfToken": csrf_token,
            },
            headers=headers,
        )
    except Exception:
        return []

    if not isinstance(first, dict):
        return []

    payload = (first.get("data") or {}) if isinstance(first, dict) else {}
    first_reviews = payload.get("DataList", []) if isinstance(payload, dict) else []
    if not isinstance(first_reviews, list):
        return []

    all_reviews = [_normalize(r) for r in first_reviews if isinstance(r, dict)]
    all_reviews = [r for r in all_reviews if r is not None]
    total = _safe_int(payload.get("TotalCount", 0), 0)
    if max_pages <= 1 or not total or len(all_reviews) >= total:
        return all_reviews

    # The endpoint returns 2 items per page regardless of pageSize. Cap pages
    # to keep runtime bounded.
    total_pages = min(max_pages, (total + 1) // 2)
    remaining = list(range(2, total_pages + 1))
    if not remaining:
        return all_reviews

    sem = asyncio.Semaphore(_REVIEW_PAGE_CONCURRENCY)

    async def _fetch_page(page: int) -> list:
        async with sem:
            try:
                data = await ctx.access.http.fetch_json(
                    url,
                    params={
                        "bookId": book_id,
                        "chapterId": chapter_id,
                        "page": page,
                        "pageSize": page_size,
                        "_csrfToken": csrf_token,
                    },
                    headers=headers,
                )
            except Exception:
                return []
            if not isinstance(data, dict):
                return []
            payload = (data.get("data") or {}) if isinstance(data, dict) else {}
            reviews = payload.get("DataList", []) if isinstance(payload, dict) else []
            if not isinstance(reviews, list):
                return []
            return [_normalize(r) for r in reviews if isinstance(r, dict)]

    for batch in await asyncio.gather(*[_fetch_page(p) for p in remaining]):
        all_reviews.extend(batch)
        if len(all_reviews) >= total:
            break

    return all_reviews


async def _fetch_chapter_end_reviews(
    ctx, book_id: str, chapter_id: str, csrf_token: str, headers: dict, *, max_pages: int = 5
) -> tuple[list, int]:
    """Fetch chapter-end reviews (paragraphId=-1) with pagination.

    These often number in the thousands for popular chapters, so callers can
    cap the number of pages to keep initial loads fast. Use `max_pages` to
    control how many pages (20 reviews each) are fetched.

    Returns:
        (normalized_reviews, reported_total_from_reviewlist4m)
    """
    url = f"{MOBILE_BASE}/webcommon/chapterreview/reviewlist4m"
    page_size = 20

    def _normalize(r: Any) -> Any:
        return _normalize_review(r, -1, include_replies=True) if isinstance(r, dict) else None

    first = await ctx.access.http.fetch_json(
        url,
        params={
            "bookId": book_id,
            "chapterId": chapter_id,
            "paragraphId": -1,
            "page": 1,
            "pageSize": page_size,
            "_csrfToken": csrf_token,
        },
        headers=headers,
    )
    if not isinstance(first, dict):
        return [], 0

    first_data = (first.get("data") or {}) if isinstance(first, dict) else {}
    first_reviews = first_data.get("list", []) if isinstance(first_data, dict) else []
    if not isinstance(first_reviews, list):
        return [], 0

    all_reviews = [_normalize(r) for r in first_reviews if isinstance(r, dict)]
    all_reviews = [r for r in all_reviews if r is not None]
    total = _safe_int(first_data.get("total", 0), 0)
    if not total or len(all_reviews) >= total:
        return all_reviews, total

    total_pages = min(max_pages, (total + page_size - 1) // page_size)
    remaining = list(range(2, total_pages + 1))
    if not remaining:
        return all_reviews, total

    sem = asyncio.Semaphore(_REVIEW_PAGE_CONCURRENCY)

    async def _fetch_page(page: int) -> list:
        async with sem:
            try:
                data = await ctx.access.http.fetch_json(
                    url,
                    params={
                        "bookId": book_id,
                        "chapterId": chapter_id,
                        "paragraphId": -1,
                        "page": page,
                        "pageSize": page_size,
                        "_csrfToken": csrf_token,
                    },
                    headers=headers,
                )
            except Exception:
                return []
            if not isinstance(data, dict):
                return []
            page_data = (data.get("data") or {}) if isinstance(data, dict) else {}
            reviews = page_data.get("list", []) if isinstance(page_data, dict) else []
            if not isinstance(reviews, list):
                return []
            return [_normalize(r) for r in reviews if isinstance(r, dict)]

    for batch in await asyncio.gather(*[_fetch_page(p) for p in remaining]):
        all_reviews.extend(batch)
        if len(all_reviews) >= total:
            break

    return all_reviews, total


async def _fetch_paragraph_review_page(
    ctx, book_id: str, chapter_id: str, paragraph_id: int,
    csrf_token: str, headers: dict, *, page: int, page_size: int,
) -> tuple[list, int]:
    """Read exactly the requested page; reject upstream failures as failures."""
    response = await ctx.access.http.fetch_json(
        f"{MOBILE_BASE}/webcommon/chapterreview/reviewlist4m",
        params={
            "bookId": book_id, "chapterId": chapter_id,
            "paragraphId": paragraph_id, "page": page,
            "pageSize": page_size, "_csrfToken": csrf_token,
        },
        headers=headers,
    )
    if not isinstance(response, dict) or response.get("code") != 0:
        raise RuntimeError("Web paragraph review request failed")
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise RuntimeError("Invalid Web paragraph review response")
    return (
        [_normalize_review(item, paragraph_id, include_replies=True)
         for item in data["list"] if isinstance(item, dict)],
        _safe_int(data.get("total"), 0),
    )


async def _fetch_paragraph_reviews(
    ctx, book_id: str, chapter_id: str, paragraph_id: int, csrf_token: str, headers: dict
, *, max_pages: int = 1
) -> tuple[list, int]:
    """Call reviewlist4m API for a specific paragraph with pagination.

    Pages are fetched concurrently to reduce latency for paragraphs with many
    comments. A safety cap prevents runaway chapters from consuming too many
    resources. By default only the first page is fetched; callers that need
    more can pass a higher `max_pages`.
    """
    url = f"{MOBILE_BASE}/webcommon/chapterreview/reviewlist4m"
    page_size = 20

    def _normalize(r: Any) -> Any:
        return _normalize_review(r, paragraph_id, include_replies=True) if isinstance(r, dict) else None

    first = await ctx.access.http.fetch_json(
        url,
        params={
            "bookId": book_id,
            "chapterId": chapter_id,
            "paragraphId": paragraph_id,
            "page": 1,
            "pageSize": page_size,
            "_csrfToken": csrf_token,
        },
        headers=headers,
    )
    if not isinstance(first, dict):
        return [], 0

    first_data = (first.get("data") or {}) if isinstance(first, dict) else {}
    first_reviews = first_data.get("list", []) if isinstance(first_data, dict) else []
    if not isinstance(first_reviews, list):
        return [], 0

    all_reviews = [_normalize(r) for r in first_reviews if isinstance(r, dict)]
    all_reviews = [r for r in all_reviews if r is not None]
    total = _safe_int(first_data.get("total", 0), 0)
    if not total or len(all_reviews) >= total:
        return all_reviews, total

    total_pages = min(max_pages, (total + page_size - 1) // page_size)
    remaining = list(range(2, total_pages + 1))
    if not remaining:
        return all_reviews, total

    sem = asyncio.Semaphore(_REVIEW_PAGE_CONCURRENCY)

    async def _fetch_page(page: int) -> list:
        async with sem:
            try:
                data = await ctx.access.http.fetch_json(
                    url,
                    params={
                        "bookId": book_id,
                        "chapterId": chapter_id,
                        "paragraphId": paragraph_id,
                        "page": page,
                        "pageSize": page_size,
                        "_csrfToken": csrf_token,
                    },
                    headers=headers,
                )
            except Exception:
                return []
            if not isinstance(data, dict):
                return []
            page_data = (data.get("data") or {}) if isinstance(data, dict) else {}
            reviews = page_data.get("list", []) if isinstance(page_data, dict) else []
            if not isinstance(reviews, list):
                return []
            return [_normalize(r) for r in reviews if isinstance(r, dict)]

    for batch in await asyncio.gather(*[_fetch_page(p) for p in remaining]):
        all_reviews.extend(batch)
        if len(all_reviews) >= total:
            break

    return all_reviews, total
