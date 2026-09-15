"""Search-provider access owned by Source Access Bridge."""

from __future__ import annotations

import base64
import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.access_bridge.client import AccessBridgeUnavailable
from app.services.access_bridge.models import SearchProviderHit


from app.config import get_default_user_agent


DEFAULT_HEADERS = {
    "accept-language": "zh-CN,zh;q=0.9",
    "User-Agent": get_default_user_agent(),
}

DUCKDUCKGO_LIBRARY_PROVIDER = "duckduckgo_ddgs"
HTTP_RESULT_PAGE_PROVIDERS = {"bing_html", "bing_cn", "google_html"}


def build_provider_urls(
    keyword: str,
    *,
    target_domain: str,
    provider_order: list[str],
    query_site_path: str = "",
) -> list[tuple[str, str]]:
    """Build no-API search provider URLs for a site-scoped query."""
    site = f"{target_domain}{query_site_path}".strip()
    query = quote_plus(f"site:{site} {keyword}")
    urls: list[tuple[str, str]] = []
    for provider in provider_order:
        if provider == "bing_html":
            urls.append((provider, f"https://www.bing.com/search?q={query}"))
        elif provider == "bing_cn":
            urls.append((provider, f"https://cn.bing.com/search?q={query}"))
        elif provider == "google_html":
            urls.append((provider, f"https://www.google.com/search?q={query}"))
    return urls


def build_site_query(keyword: str, *, target_domain: str, query_site_path: str = "") -> str:
    """Build a site-scoped search-provider query."""
    site = f"{target_domain}{query_site_path}".strip()
    return f"site:{site} {keyword}"


def normalize_search_provider_url(
    href: str,
    *,
    target_domain: str,
    url_patterns: list[str],
) -> str:
    """Normalize direct, DuckDuckGo, Bing, and Google search-result URLs."""
    if not href:
        return ""
    href = _unwrap_redirect_url(href)
    href = unquote(href)
    matched_pattern = _matched_pattern(href, target_domain, url_patterns)
    if not matched_pattern:
        return ""
    match = re.search(_target_url_regex(target_domain, matched_pattern), href)
    if not match:
        return ""
    parsed = urlparse(match.group(0))
    return urlunparse(parsed._replace(scheme="https", netloc=target_domain))


def parse_search_provider_results(
    html: str,
    *,
    provider: str,
    target_domain: str,
    url_patterns: list[str],
) -> list[SearchProviderHit]:
    """Extract matching target URLs from a search-provider result page."""
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[tuple[str, str]] = []
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href", "") or "")
        title = " ".join(anchor.get_text(" ", strip=True).split())
        normalized = normalize_search_provider_url(
            href,
            target_domain=target_domain,
            url_patterns=url_patterns,
        )
        if normalized:
            candidates.append((normalized, title))

    decoded_html = unquote(html or "")
    for pattern in url_patterns:
        regex = _target_url_regex(target_domain, pattern)
        for match in re.findall(regex, decoded_html):
            normalized = normalize_search_provider_url(
                match,
                target_domain=target_domain,
                url_patterns=url_patterns,
            )
            if normalized:
                candidates.append((normalized, ""))

    hits: list[SearchProviderHit] = []
    seen: set[str] = set()
    for url, title in candidates:
        if url in seen:
            continue
        seen.add(url)
        matched = _matched_pattern(url, target_domain, url_patterns)
        hits.append(SearchProviderHit(
            title=title,
            url=url,
            provider=provider,
            rank=len(hits) + 1,
            matched_pattern=matched,
        ))
    return hits


def parse_search_provider_items(
    items: list[dict[str, Any]],
    *,
    provider: str,
    target_domain: str,
    url_patterns: list[str],
) -> list[SearchProviderHit]:
    """Normalize structured search-provider items such as DDGS results."""
    hits: list[SearchProviderHit] = []
    seen: set[str] = set()
    for item in items:
        href = str(item.get("href") or item.get("url") or "")
        title = " ".join(str(item.get("title") or "").split())
        snippet = " ".join(str(item.get("body") or item.get("snippet") or "").split())
        candidates = [href, snippet]
        for candidate in candidates:
            normalized = normalize_search_provider_url(
                candidate,
                target_domain=target_domain,
                url_patterns=url_patterns,
            )
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            hits.append(SearchProviderHit(
                title=title,
                url=normalized,
                provider=provider,
                rank=len(hits) + 1,
                snippet=snippet,
                matched_pattern=_matched_pattern(normalized, target_domain, url_patterns),
            ))
            break
    return hits


async def duckduckgo_library_search(
    keyword: str,
    *,
    target_domain: str,
    query_site_path: str = "",
    max_results: int = 10,
    proxy: str = "",
) -> list[dict[str, Any]]:
    """Run DuckDuckGo search through the optional DDGS Python package."""
    query = build_site_query(
        keyword,
        target_domain=target_domain,
        query_site_path=query_site_path,
    )

    def _run() -> list[dict[str, Any]]:
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError as exc:
                raise RuntimeError("ddgs is not installed") from exc
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                with DDGS(timeout=8, proxy=proxy or None) as ddgs:
                    rows = list(ddgs.text(
                        query,
                        region="wt-wt",
                        safesearch="off",
                        max_results=max_results,
                    ))
                if rows:
                    return rows
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    return await asyncio.to_thread(_run)


async def search_site(
    keyword: str,
    *,
    target_domain: str,
    url_patterns: list[str],
    provider_order: list[str],
    fetch_text: Callable[[str], Awaitable[str]],
    fetch_ddg: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    query_site_path: str = "",
    limit: int = 10,
) -> list[SearchProviderHit]:
    """Search a target site through explicitly declared providers in parallel."""
    fetch_ddg = fetch_ddg or duckduckgo_library_search
    provider_urls = dict(build_provider_urls(
        keyword,
        target_domain=target_domain,
        provider_order=provider_order,
        query_site_path=query_site_path,
    ))

    async def _run_provider(provider: str) -> list[SearchProviderHit]:
        if provider == DUCKDUCKGO_LIBRARY_PROVIDER:
            try:
                raw_items = await fetch_ddg(
                    keyword,
                    target_domain=target_domain,
                    query_site_path=query_site_path,
                    max_results=limit,
                )
            except Exception:
                return []
            return parse_search_provider_items(
                raw_items,
                provider=provider,
                target_domain=target_domain,
                url_patterns=url_patterns,
            )
        if provider not in HTTP_RESULT_PAGE_PROVIDERS:
            return []
        url = provider_urls.get(provider, "")
        if not url:
            return []
        try:
            html = await fetch_text(url)
        except AccessBridgeUnavailable:
            raise
        except Exception:
            return []
        return parse_search_provider_results(
            html,
            provider=provider,
            target_domain=target_domain,
            url_patterns=url_patterns,
        )

    results = await asyncio.gather(
        *[_run_provider(provider) for provider in provider_order],
        return_exceptions=True,
    )
    merged: list[SearchProviderHit] = []
    seen: set[str] = set()
    provider_rank = {provider: index for index, provider in enumerate(provider_order)}
    for result in results:
        if isinstance(result, AccessBridgeUnavailable):
            raise result
        if isinstance(result, Exception):
            continue
        for hit in result:
            if hit.url in seen:
                continue
            seen.add(hit.url)
            merged.append(hit)
    merged.sort(key=lambda hit: (provider_rank.get(hit.provider, 999), hit.rank))
    return merged[:limit]


def _unwrap_redirect_url(href: str) -> str:
    if href.startswith("/url?") or href.startswith("https://www.google.com/url?"):
        parsed = urlparse(urljoin("https://www.google.com", href))
        return parse_qs(parsed.query).get("q", [""])[0]
    if "duckduckgo.com/l/?" in href or href.startswith("//duckduckgo.com/l/?") or href.startswith("/l/?"):
        parsed = urlparse(urljoin("https://duckduckgo.com", href))
        return parse_qs(parsed.query).get("uddg", [""])[0]
    if href.startswith("/ck/a") or "bing.com/ck/a" in href:
        parsed = urlparse(urljoin("https://www.bing.com", href))
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            try:
                padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            except Exception:
                return encoded
    return href


def _matched_pattern(value: str, target_domain: str, url_patterns: list[str]) -> str:
    for pattern in url_patterns:
        if re.search(_target_url_regex(target_domain, pattern), value):
            return pattern
    return ""


def _target_url_regex(target_domain: str, pattern: str) -> str:
    domain = re.escape(target_domain)
    clean_pattern = pattern.lstrip("^")
    return rf"https?://{domain}{clean_pattern}"




