"""Runtime context API exposed to plugins.

Plugins use ctx for fetch, parse, trace, cookies, auth, and utilities.
LegadoHub core owns concurrency, timeout, proxy, cache, and trace policy.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from lxml import html as lh
from bs4 import BeautifulSoup

from app.services.access_bridge.facade import SourceAccessBridge
from app.services.cookie_store import CookieStore
from app.services.text_convert import to_simplified, to_traditional
from app.source_plugins.fetcher import Fetcher
from app.source_plugins.errors import ParseEmpty, ParseError


class CookieJar:
    """Per-plugin cookie jar backed by the host CookieStore.

    The host only stores/retrieves an opaque JSON payload. This class keeps the
    existing convention of wrapping the domain-keyed jar as
    ``{"cookies": {"domain": {"name": "value"}}}`` for backward compatibility
    with current plugins, but the file path and persistence are owned by the host.
    """

    def __init__(
        self,
        fetcher: Fetcher,
        plugin_id: str,
        cookie_store: CookieStore | None = None,
        allowed: bool = True,
    ):
        self._fetcher = fetcher
        self._plugin_id = plugin_id
        self._store = cookie_store or CookieStore()
        self._allowed = allowed
        self._known_domains: set[str] = set()

    def get(self, domain: str, name: str | None = None) -> str | dict[str, str] | None:
        self._known_domains.add(domain)
        jar = self._fetcher.cookies_for_domain(domain)
        if name is None:
            return jar if jar else None
        return jar.get(name)



    def _guard(self) -> bool:
        """Return True if this jar is allowed to read/write persisted cookies."""
        return self._allowed

    def set(self, domain: str, cookie: dict[str, str]) -> None:
        """Replace all cookies for ``domain`` with the provided dict.

        This matches the documented contract: callers pass the full cookie dict
        they want the domain to have.  Previously the implementation merged
        keys, which made it easy for plugins to accidentally leave stale keys
        or, conversely, drop unknown keys when they only passed a whitelist.
        """
        if not self._guard():
            return
        self._known_domains.add(domain)
        normalized = self._fetcher._normalize_cookie_domain(domain)
        self._fetcher.clear_cookies(normalized)
        for k, v in cookie.items():
            self._fetcher.set_cookie(normalized, k, v)
        self._persist()

    def merge(self, domain: str, cookie: dict[str, str]) -> None:
        """Merge ``cookie`` into the existing cookies for ``domain``.

        Use this when you only want to update a few keys and keep everything
        else intact.
        """
        if not self._guard():
            return
        normalized = self._fetcher._normalize_cookie_domain(domain)
        self._known_domains.add(normalized)
        for k, v in cookie.items():
            self._fetcher.set_cookie(normalized, k, v)
        self._persist()

    def set_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Apply browser cookies to this request; persist only when declared."""
        for item in cookies:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain", "")).lstrip(".")
            name = str(item.get("name", ""))
            value = str(item.get("value", ""))
            if not domain or not name:
                continue
            self._known_domains.add(domain)
            self._fetcher.set_cookie(domain, name, value)
        if self._guard():
            self._persist()

    def clear(self, domain: str | None = None) -> None:
        if not self._guard():
            return
        self._fetcher.clear_cookies(domain)
        if domain is None:
            self._store.clear(self._plugin_id)
            return
        payload = self._load_cookie_payload()
        jar = self._extract_jar(payload)
        jar.pop(domain, None)
        self._store.save(self._plugin_id, {"cookies": jar})

    def _load_cookie_payload(self) -> dict:
        payload = self._store.load(self._plugin_id)
        return payload if isinstance(payload, dict) else {}

    def _extract_jar(self, payload: dict) -> dict[str, dict[str, str]]:
        """Extract domain-keyed jar from payload.

        Supports both the wrapped ``{"cookies": {...}}`` form and the legacy
        direct jar form.
        """
        cookies = payload.get("cookies")
        if isinstance(cookies, dict):
            return {
                str(domain): {str(k): str(v) for k, v in jar.items() if v is not None}
                for domain, jar in cookies.items()
                if isinstance(jar, dict)
            }
        if all(isinstance(v, dict) for v in payload.values() if isinstance(v, dict)):
            return {
                str(domain): {str(k): str(v) for k, v in jar.items() if v is not None}
                for domain, jar in payload.items()
                if isinstance(jar, dict)
            }
        return {}

    def load_into_fetcher(self) -> None:
        """Load persisted cookies into the fetcher jar."""
        if not self._guard():
            return
        for domain, cookies in self._extract_jar(self._load_cookie_payload()).items():
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    self._fetcher.set_cookie(domain, name, value)

    def _persist(self) -> None:
        if not self._guard():
            return
        from urllib.parse import urlparse

        domains = set(self._extract_jar(self._load_cookie_payload()).keys()) | self._known_domains
        for trace in self._fetcher.get_traces():
            url = trace.get("url", "")
            domain = urlparse(url).netloc
            if domain:
                domains.add(domain)
        cookies: dict[str, dict[str, str]] = {}
        for domain in domains:
            jar = self._fetcher.cookies_for_domain(domain)
            if jar:
                cookies[domain] = jar
        cookie_snapshot = getattr(self._fetcher, "cookie_snapshot", lambda: {})()
        if isinstance(cookie_snapshot, dict):
            for domain, jar in cookie_snapshot.items():
                if isinstance(jar, dict) and jar:
                    cookies[domain] = jar
        cookies = {domain: jar for domain, jar in cookies.items() if jar}
        if not cookies:
            return
        self._store.save(self._plugin_id, {"cookies": cookies})


class PluginSettingsView:
    """Read-only view of the plugin's persisted ui settings for the plugin."""

    def __init__(self, values: dict | None = None):
        self._values: dict = dict(values or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def as_dict(self) -> dict:
        return dict(self._values)


class PluginContext:
    def __init__(
        self,
        fetcher: Fetcher,
        plugin_id: str,
        cookie_store: CookieStore | None = None,
        access_bridge: Any = None,
        proxy_mode: str = "auto",
        proxy_url: str = "",
        cookie_allowed: bool = True,
        settings: dict | None = None,
    ):
        self._fetcher = fetcher
        self.plugin_id = plugin_id
        self.cookies = CookieJar(fetcher, plugin_id, cookie_store, allowed=cookie_allowed)
        self._access_bridge = access_bridge
        self.proxy_mode = proxy_mode
        self.proxy_url = proxy_url
        self.access = SourceAccessBridge(self)
        self.settings = PluginSettingsView(settings)
        self._traces: list[dict] = []

    # -- Parsing --

    def _to_lxml(self, html_or_node: str | Any) -> Any:
        if isinstance(html_or_node, str):
            return lh.fromstring(html_or_node)
        return html_or_node

    def select(self, html_or_node: str | Any, selector: str) -> list[Any]:
        """CSS selector; returns list of lxml elements."""
        tree = self._to_lxml(html_or_node)
        return tree.cssselect(selector)

    def text(self, html_or_node: str | Any, selector: str | None = None) -> str:
        """Extract text. If selector is None, extract from root."""
        if selector is None:
            tree = self._to_lxml(html_or_node)
            text = tree.text_content() if hasattr(tree, "text_content") else str(tree)
            return self.clean_text(text)
        nodes = self.select(html_or_node, selector)
        if not nodes:
            return ""
        return self.clean_text(nodes[0].text_content())

    def html(self, html_or_node: str | Any, selector: str | None = None) -> str:
        """Extract inner HTML."""
        if selector is None:
            tree = self._to_lxml(html_or_node)
            return lh.tostring(tree, encoding="unicode")
        nodes = self.select(html_or_node, selector)
        if not nodes:
            return ""
        return lh.tostring(nodes[0], encoding="unicode")

    def attr(self, html_or_node: str | Any, selector: str, name: str) -> str:
        """Extract attribute from first matched element."""
        nodes = self.select(html_or_node, selector)
        if not nodes:
            return ""
        return nodes[0].get(name, "")

    def json_path(self, data: Any, path: str) -> Any:
        """Simple JSON path like 'data.items' or 'data.items.0.name'."""
        current = data
        for part in path.split("."):
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    idx = int(part)
                    current = current[idx]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    def regex(self, text: str, pattern: str, group: int = 1, default: str = "") -> str:
        m = re.search(pattern, text)
        if not m:
            return default
        try:
            return m.group(group)
        except IndexError:
            # If group=1 requested but no capturing groups exist, return full match
            if group == 1:
                return m.group(0)
            return default

    # -- Utilities --

    def urljoin(self, base: str, href: str) -> str:
        return urljoin(base, href)

    def clean_html(self, html: str) -> str:
        """Remove chrome/ads/scripts and return readable paragraph text."""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "iframe"]):
            tag.decompose()
        for tag in soup.find_all(["br"]):
            tag.replace_with("\n")

        blocks: list[str] = []
        block_tags = soup.find_all(["p", "li"])
        if not block_tags:
            block_tags = soup.find_all(["div", "section", "article"])
        full_text = soup.get_text("\n", strip=True)
        if block_tags:
            for tag in block_tags:
                text = self.clean_text(tag.get_text(" ", strip=True))
                if text:
                    blocks.append(text)
            block_text_len = sum(len(block) for block in blocks)
            if len(self.clean_text(full_text)) > block_text_len * 2:
                blocks = [self.clean_text(line) for line in full_text.splitlines() if self.clean_text(line)]
        else:
            blocks = [self.clean_text(line) for line in full_text.splitlines() if self.clean_text(line)]

        cleaned: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            if block in seen:
                continue
            seen.add(block)
            cleaned.append(block)
        return "\n\n".join(cleaned)

    def clean_text(self, text: str) -> str:
        """Normalize whitespace."""
        if not text:
            return ""
        return " ".join(text.split())

    def to_simplified(self, value: Any) -> Any:
        """Convert Traditional Chinese output text to Simplified Chinese."""
        return to_simplified(value)

    def to_traditional(self, value: Any) -> Any:
        """Convert Simplified Chinese input text to Traditional Chinese."""
        return to_traditional(value)

    def decode_text(self, content_bytes: bytes, charset: str | None = None) -> str:
        if charset:
            return content_bytes.decode(charset, errors="replace")
        # Try utf-8 first, then gbk
        for enc in ("utf-8", "gbk", "gb2312", "big5"):
            try:
                return content_bytes.decode(enc)
            except UnicodeDecodeError:
                pass
        return content_bytes.decode("utf-8", errors="replace")

    def trace(
        self,
        stage: str,
        url: str = "",
        message: str = "",
        data: Any = None,
        **extra: Any,
    ) -> None:
        payload = {
            "stage": stage,
            "url": url,
            "message": message,
            "data": data,
        }
        if extra:
            payload["extra"] = extra
        self._traces.append(payload)

    def get_traces(self) -> list[dict]:
        return list(self._traces)

    # -- Cache hooks (pass-through; scheduler may wrap) --

    def cache_get(self, key: str) -> Any:
        # Default no-op; scheduler may inject a real cache
        return None

    def cache_set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        pass

    # -- Auth/session --

    async def auth_status(self) -> dict:
        """Default auth status for sources without auth hooks."""
        return {
            "sourceId": self.plugin_id,
            "authenticated": False,
            "accountName": "",
            "expiresAt": "",
            "message": "未登录",
            "requiredActions": [],
        }

    async def request_manual_login(self, login_url: str, cookie_domains: list[str], message: str = "") -> dict:
        return {
            "sourceId": self.plugin_id,
            "mode": "manual_browser",
            "loginUrl": login_url,
            "instructions": message or "在打开的浏览器中完成登录，然后回到后台点击检测登录状态。",
            "cookieDomains": cookie_domains,
        }


