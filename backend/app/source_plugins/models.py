"""Canonical plugin metadata and normalized output shapes.

Follows docs/architecture/source-plugin-contract.md exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PluginMetadata:
    contract_version: str
    id: str
    name: str
    version: str
    type: str  # "source"
    domains: list[str]
    base_urls: list[str]
    capabilities: list[str]
    auth: dict
    content: dict
    tags: list[str]
    # optional
    author: str = ""
    priority: int = 50
    enabled: bool = True
    language: str = "zh-CN"
    rate_limit: dict = field(default_factory=dict)
    proxy: dict = field(default_factory=dict)
    browser: dict = field(default_factory=dict)
    access_bridge: dict = field(default_factory=dict)
    domain_profiles: list[dict] = field(default_factory=list)
    source_seed: dict = field(default_factory=dict)
    access_strategy: dict = field(default_factory=dict)
    search_provider: dict = field(default_factory=dict)
    ad_patterns: list[str] = field(default_factory=list)

    CONTENT_KINDS = {"text", "audio", "video"}

    @classmethod
    def from_dict(cls, data: dict) -> PluginMetadata:
        return cls(
            contract_version=data.get("contractVersion", "1.0"),
            id=data["id"],
            name=data["name"],
            version=data["version"],
            type=data.get("type", "source"),
            domains=data.get("domains", []),
            base_urls=data.get("baseUrls", []),
            capabilities=data.get("capabilities", []),
            auth=data.get("auth", {}),
            content=data.get("content", {}),
            tags=data.get("tags", []),
            author=str(data.get("author", "")).strip(),
            priority=data.get("priority", 50),
            enabled=data.get("enabled", True),
            language=data.get("language", "zh-CN"),
            rate_limit=data.get("rateLimit", {}),
            proxy=data.get("proxy", {}),
            browser=data.get("browser", {}),
            access_bridge=data.get("accessBridge", {}),
            domain_profiles=data.get("domainProfiles", []),
            source_seed=data.get("sourceSeed", {}),
            access_strategy=data.get("accessStrategy", {}),
            search_provider=data.get("searchProvider", {}),
            ad_patterns=data.get("adPatterns", []) or [],
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.contract_version != "1.0":
            errors.append(f"unsupported contractVersion: {self.contract_version}")
        if not self.id:
            errors.append("id is required")
        if not self.name:
            errors.append("name is required")
        if self.type != "source":
            errors.append(f"type must be 'source', got {self.type}")
        valid_caps = {"search", "detail", "toc", "chapter", "chapter_reviews", "explore", "auth"}
        for cap in self.capabilities:
            if cap not in valid_caps:
                errors.append(f"invalid capability: {cap}")
        if "explore" in self.capabilities and not self.is_official_source():
            errors.append("explore capability is only allowed for official sources")
        auth_mode = self.auth.get("mode", "none")
        if auth_mode not in {"none", "optional", "required", "manual"}:
            errors.append(f"invalid auth.mode: {auth_mode}")
        content_access = self.content.get("access", "unknown")
        if content_access not in {"free", "paid", "mixed", "unknown"}:
            errors.append(f"invalid content.access: {content_access}")
        content_kind = str(self.content.get("kind", "text") or "text").strip().lower()
        if content_kind not in self.CONTENT_KINDS:
            errors.append(f"invalid content.kind: {content_kind}")
        proxy_mode = self.proxy.get("mode", "auto")
        if proxy_mode not in {"auto", "always", "never"}:
            errors.append(f"invalid proxy.mode: {proxy_mode}")
        valid_strategy = {
            "http",
            "stealth_http",
            "tls_impersonate",
            "search_provider",
            "headless_browser",
            "remote_browser",
            "api",
            "feed",
            "local_file",
        }
        for stage, mode in self.access_strategy.items():
            if stage not in {"search", "detail", "toc", "chapter", "explore"}:
                errors.append(f"invalid accessStrategy stage: {stage}")
            if mode not in valid_strategy:
                errors.append(f"invalid accessStrategy.{stage}: {mode}")
        if not isinstance(self.rate_limit, dict):
            errors.append("rateLimit must be a mapping")
        else:
            for field_name, minimum in (("perHostConcurrency", 1), ("minIntervalMs", 0)):
                if field_name not in self.rate_limit:
                    continue
                value = self.rate_limit[field_name]
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                    errors.append(f"rateLimit.{field_name} must be an integer >= {minimum}")
        if not isinstance(self.ad_patterns, list):
            errors.append("adPatterns must be a list")
        else:
            for idx, pat in enumerate(self.ad_patterns):
                if not isinstance(pat, str):
                    errors.append(f"adPatterns[{idx}] must be a string regex pattern")
                    break
        profile_ids: set[str] = set()
        for profile in self.domain_profiles:
            if not isinstance(profile, dict):
                errors.append("domainProfiles entries must be mappings")
                continue
            profile_id = profile.get("id", "")
            if not profile_id:
                errors.append("domainProfiles[].id is required")
            elif profile_id in profile_ids:
                errors.append(f"duplicate domainProfiles id: {profile_id}")
            profile_ids.add(profile_id)
            role = profile.get("role", "mirror")
            if role not in {"mirror", "mobile", "desktop", "api", "legacy"}:
                errors.append(f"invalid domainProfiles[].role: {role}")
        return errors

    def access_mode(self, stage: str) -> str:
        """Return the configured runtime access strategy for a lifecycle stage."""
        return str(self.access_strategy.get(stage, "") or "")

    def uses_search_provider(self, stage: str = "search") -> bool:
        """Whether this stage is explicitly routed through access-provider search."""
        return self.access_mode(stage) == "search_provider"

    def is_official_source(self) -> bool:
        """Whether this plugin represents an official/licensed content source."""
        tags = {str(tag).strip().lower() for tag in self.tags}
        source_role = str(self.content.get("sourceRole", "") or "").strip().lower()
        return "official" in tags or source_role == "official"

    @property
    def content_kind(self) -> str:
        """Declared media kind of chapter content: "text" | "audio" | "video"."""
        kind = str(self.content.get("kind", "text") or "text").strip().lower()
        return kind if kind in self.CONTENT_KINDS else "text"

    @property
    def cookie_domains(self) -> list[str]:
        """Domains declared by the plugin for cookie persistence."""
        domains = self.auth.get("cookieDomains")
        if isinstance(domains, list):
            return [str(d) for d in domains]
        return []

    @property
    def declares_cookies(self) -> bool:
        """Whether the plugin metadata explicitly declares cookie storage."""
        return bool(self.cookie_domains)


@dataclass
class LoadedPlugin:
    metadata: PluginMetadata
    module: Any
    source: Any
    capabilities: list[str] = field(default_factory=list)


@dataclass
class PluginHealth:
    plugin_id: str
    enabled: bool
    last_smoke_at: str = ""
    last_smoke_pass: bool = False
    last_error: str = ""
    consecutive_failures: int = 0
    avg_latency_ms: int = 0


@dataclass
class SearchResult:
    source_id: str
    name: str
    author: str = ""
    book_url: str = ""
    cover_url: str = ""
    intro: str = ""
    kind: str = ""
    last_chapter: str = ""
    word_count: str = ""
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "name": self.name,
            "author": self.author,
            "bookUrl": self.book_url,
            "coverUrl": self.cover_url,
            "intro": self.intro,
            "kind": self.kind,
            "lastChapter": self.last_chapter,
            "wordCount": self.word_count,
            "score": self.score,
            "extra": self.extra,
        }


@dataclass
class BookDetail:
    source_id: str
    name: str = ""
    author: str = ""
    book_url: str = ""
    cover_url: str = ""
    intro: str = ""
    kind: str = ""
    last_chapter: str = ""
    word_count: str = ""
    toc_url: str = ""
    auth_required: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "name": self.name,
            "author": self.author,
            "bookUrl": self.book_url,
            "coverUrl": self.cover_url,
            "intro": self.intro,
            "kind": self.kind,
            "lastChapter": self.last_chapter,
            "wordCount": self.word_count,
            "tocUrl": self.toc_url,
            "authRequired": self.auth_required,
            "extra": self.extra,
        }


@dataclass
class ChapterItem:
    source_id: str
    index: int
    title: str
    chapter_url: str
    update_time: str = ""
    is_vip: bool = False
    is_locked: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "index": self.index,
            "title": self.title,
            "chapterUrl": self.chapter_url,
            "updateTime": self.update_time,
            "isVip": self.is_vip,
            "isLocked": self.is_locked,
            "extra": self.extra,
        }


@dataclass
class ChapterContent:
    source_id: str
    title: str = ""
    chapter_url: str = ""
    content: str = ""
    format: str = "text"
    auth_required: bool = False
    is_paid: bool = False
    extra: dict = field(default_factory=dict)
    # media chapters (format "audio"/"video"): `content` stays empty and these
    # carry the playable stream reference instead.
    media_url: str = ""
    media_mime: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "title": self.title,
            "chapterUrl": self.chapter_url,
            "content": self.content,
            "format": self.format,
            "mediaUrl": self.media_url,
            "mediaType": self.media_mime,
            "durationSeconds": self.duration_seconds,
            "authRequired": self.auth_required,
            "isPaid": self.is_paid,
            "extra": self.extra,
        }


@dataclass
class PluginFailure:
    source_id: str
    code: str
    stage: str
    url: str = ""
    message: str = ""
    proxy_used: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "code": self.code,
            "stage": self.stage,
            "url": self.url,
            "message": self.message,
            "proxyUsed": self.proxy_used,
            "extra": self.extra,
        }
