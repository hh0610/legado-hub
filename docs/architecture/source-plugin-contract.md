# LegadoHub Source Plugin Contract

> **For agentic workers:** This contract defines the plugin API. Do not invent a different interface during implementation. If a site needs behavior not covered here, add a backward-compatible extension to this document first, then implement it.

**Contract version:** `1.0`

**Runtime owner:** LegadoHub core.

**Plugin owner:** Site-specific adapter code.

---

## Core Rule

A source plugin describes how one site or site family is adapted. It does not control the engine.

## Host-Layer Boundaries

After the refactor, the following responsibilities belong to the host layer and must not be bypassed or duplicated by plugins:

1. **Configuration is centralized**: Runtime configuration is loaded by the host from `backend/config/app_config.json`. Old files `backend/config/source_pool.json`, `backend/config/aggregate_source.json`, and `backend/data/ai_provider.json` are retired.
2. **Cookie files are host-owned**: Cookies are stored by the host at `backend/config/cookies/<plugin_id>.json`. Plugins no longer read or write `Cookie.json` inside their own directories. The host provides an opaque load/save/clear interface; the JSON payload structure is defined by the plugin.
3. **Auth state is probed in real time**: Login/auth state is not persisted in the database. `auth_status` is probed in real time by the plugin. The host may cache the Cookie payload file, but the plugin decides whether the Cookie is valid.
4. **Search events / plugin health / runtime proxy state are not persisted**: Search events are kept in memory by the `SearchCoordinator`; process logs and debug data go to `backend/runtime/logs/` instead of the database.
5. **Proxy policy is tightened**: Default direct. Proxy is used only when a plugin declares `proxy.mode: always`, or `proxy.mode: auto` **and** `proxy.required: true` **and** the host config `proxy.allowAutoRetry: true`. `auto` no longer means "try proxy on every source".
6. **Search scheduling is host-owned**: Plugins must not assume search process events will be persisted, nor schedule search retries themselves.

Plugins may:

- Build site-specific URLs.
- Parse HTML/JSON/API responses.
- Generate site-specific signatures or tokens.
- Decrypt chapter payloads.
- Describe authentication requirements.
- Describe site-family domain profiles and choose among them while parsing.
- Request controlled browser/manual-login assistance through `ctx`.

Plugins must not:

- Control source-level concurrency.
- Create unmanaged background threads.
- Create global HTTP sessions that bypass `ctx`.
- Own global retry/proxy/cache policy.
- Shape Reading/Legado aggregate-source JSON.
- Import or depend on `engine-jvm`, `app.legado_engine`, or `app.engine`.

## Directory Layout

```text
plugins/sources/<plugin_id>/
  metadata.yaml
  source.py
  README.md
  requirements.txt       # optional; private project, install if needed
  tests/
    smoke.yaml
  skills/
    SKILL.md             # optional plugin-specific adaptation notes
```

For Stage 1, dependencies may be installed when needed. Record added backend dependencies in `backend/requirements.txt`, frontend dependencies in `frontend/package.json`, or plugin-local dependencies in `plugins/sources/<plugin_id>/requirements.txt`.

## metadata.yaml

Required fields:

```yaml
contractVersion: "1.0"
id: qidian
name: 起点中文网
version: 0.1.0
type: source
domains:
  - qidian.com
baseUrls:
  - https://www.qidian.com
capabilities:
  - search
  - detail
  - toc
  - chapter
auth:
  mode: optional
  loginUrl: https://www.qidian.com
  accountRequiredFor:
    - paid_chapter
  cookieDomains:
    - qidian.com
  sessionCheck:
    url: https://www.qidian.com
    successText:
      - 书架
content:
  access: mixed
  paid: supported_after_login
tags:
  - official
  - login
  - paid
  - json-api
```

Required field rules:

- `contractVersion`: must be `"1.0"` for Stage 1.
- `id`: stable ASCII identifier, unique across all plugins.
- `name`: Chinese display name when available.
- `version`: plugin version in SemVer-like form (e.g. `0.1.0`). Bumped whenever parsing rules, domain profiles, or access strategy change.
- `type`: must be `source`.
- `domains`: domains this plugin is allowed or expected to access.
- `baseUrls`: starting URLs for this site or site family.
- `capabilities`: subset of `search`, `detail`, `toc`, `chapter`, `explore`, `auth`.
- `auth.mode`: one of `none`, `optional`, `required`, `manual`.
- `content.access`: one of `free`, `paid`, `mixed`, `unknown`.
- `content.kind`: one of `text` (default), `audio`, `video`. Declares the media type of chapter content: audiobook sources use `audio`, film/series sources use `video`. Media sources share the exact same `search`/`detail`/`toc`/`chapter` lifecycle as text sources; only the chapter payload differs (see "Chapter content" below).
- `content.streamDomains` (optional, recommended for media sources): CDN/streaming host list where the audio/video files actually live. Playback flows through the server-side media proxy, which only allows hosts from `domains`, `baseUrls`, and this list.
- `tags`: operational hints.

`explore` covers ranking, category, hot-list, completed-list, and other discovery
surfaces. It is only allowed for official/licensed sources. A source is treated
as official when it has the `official` tag or `content.sourceRole: official`.
Ordinary mirror/scraper sources must expose only `search`, `detail`, `toc`, and
`chapter`; they must not declare ranking or category capabilities even if the
site has those pages.

`ui` (optional, declarative source settings): the plugin declares settings
items and actions in metadata; the console ("Source management → Source
settings") renders them automatically (admin-only), values persist on the
server side and are injected into the plugin runtime via
`ctx.settings.get(key, default)`. Action invocations also carry the effective
values as `payload["values"]`.

```yaml
ui:
  - title: Guest identity
    description: Device fingerprint and token live server-side.
    items:
      - { type: button, id: status, label: Check status, action: ui_status }
      - { type: text,   id: api_key, label: API key, help: optional }
      - { type: toggle, id: verbose, label: Verbose logs, default: false }
      - { type: number, id: limit, label: Page size, default: 20 }
      - { type: select, id: mode, label: Mode, choices: [fast, full], default: fast }
      - { type: color,  id: accent, label: Accent color, default: "#E34B4B" }
      - { type: hint,   label: Settings are admin-only }
```

- Item types: `button`, `toggle`, `text`, `number`, `select`, `color`, `hint`;
  non-hint items need a unique `id` and a `label` within the same `ui` block.
- `button.action` must be a callable async method of the same name on `Source`:
  `async def ui_status(self, ctx, payload) -> dict`, returning
  `{"ok": bool, "message": str, "data": {...}}` (a plain string is treated as
  the message); `payload` carries `values` (effective settings), `action`,
  and `input` (extra console-provided input).
- Actions are triggered only by admins from the console (plugin rate limits and
  timeouts apply); readers are unaffected. Do not use `ui` for reader-facing
  content configuration.

Optional fields:

```yaml
author: Yunwei
priority: 50
enabled: true
language: zh-CN
rateLimit:
  perHostConcurrency: 1
  minIntervalMs: 800
proxy:
  mode: auto        # never / auto / always
  required: false   # true / false
browser:
  mode: optional    # none / optional / required
  reason: cloudflare
accessStrategy:
  search: search_provider
  detail: stealth_http
  toc: stealth_http
  chapter: stealth_http
searchProvider:
  providerOrder:
    - duckduckgo_ddgs
    - bing_html
    - google_html
  targetDomain: www.example.com
  urlPatterns:
    - /book/\d+\.htm
domainProfiles:
  - id: mobile
    baseUrl: https://m.example.com
    domains:
      - m.example.com
    role: mobile
    fallback: true
  - id: desktop
    baseUrl: https://www.example.com
    domains:
      - www.example.com
    role: desktop
    fallback: true
sourceSeed:
  type: so-novel
  upstreamId: qidian
  upstreamFile: bundle/rules/main.json
  upstreamCommit: ""

adPatterns:
  - "regex pattern 1"
  - "regex pattern 2"
```

`author` is the plugin maintainer attribution displayed by the console. It is
not the author of books returned by `search()` or `detail()`.

`enabled`: whether the source is enabled by default after loading. The console can toggle this per-source.

`proxy.mode`:
- `never`: never use the configured runtime proxy (default behavior).
- `auto`: proxy is allowed only when `proxy.required: true` and the host config `proxy.allowAutoRetry: true` are both satisfied; otherwise stays direct.
- `always`: always route through the configured proxy.

`proxy.required`: if `true`, the source normally needs a proxy, but whether it is enabled is still decided by `proxy.mode` together with the host's `proxy.allowAutoRetry`.

`browser.mode`:
- `none`: no browser rendering.
- `optional`: try HTTP/stealth first, fallback to `ctx.access.browser` on challenge/JS-rendered pages.
- `required`: always use `ctx.access.browser.fetch_*`.

`browser.reason`: short diagnostic label such as `cloudflare`, `js_rendered`, or `login`.

`accessStrategy` is the optional final runtime route for each source lifecycle
stage. Valid routes are `http`, `stealth_http`, `tls_impersonate`,
`search_provider`, `headless_browser`, `remote_browser`, `api`, `feed`, and
`local_file`. A finished source should declare the intended route instead of
keeping hidden permanent fallback chains.

`searchProvider` is optional Source Access Bridge search-provider configuration.
DuckDuckGo uses the DDGS library provider. Bing and Google use HTTP result-page
providers. Declared providers are executed in parallel; the runtime does not add
implicit fallback providers. Individual source plugins should only map returned
hits into source result objects.

## source.py Class

Each plugin exports a `Source` class.

Required class fields:

```python
class Source:
    id = "qidian"
    name = "起点中文网"
    contract_version = "1.0"
    last_modified = "2026-06-09"
```

- `id`: stable ASCII identifier, must match the directory name and `metadata.yaml`.
- `name`: Chinese display name.
- `contract_version`: must be `"1.0"` for Stage 1.
- `last_modified`: ISO-8601 date (`YYYY-MM-DD`) indicating the last time the plugin logic was verified or updated. The console UI displays this for operators to spot stale sources.

Lifecycle methods must be async when declared in metadata capabilities:

```python
async def search(self, ctx, keyword: str, page: int) -> list[dict]:
    ...

async def detail(self, ctx, book_url: str) -> dict:
    ...

async def toc(self, ctx, toc_url: str) -> list[dict]:
    ...

async def chapter(self, ctx, chapter_url: str) -> dict:
    ...

async def explore_groups(self, ctx) -> list[dict]:
    ...

async def explore(self, ctx, group_id: str | None = None, page: int = 1) -> list[dict]:
    ...
```

`explore` is the source discovery lifecycle. It covers Reading/Legado
`enabledExplore`, `exploreUrl`, and `ruleExplore` equivalents such as ranking
lists, category pages, completed-book lists, and new-book shelves. A plugin that
declares `explore` must provide both `explore_groups` and `explore`. The loader
validates this pair.

Optional auth methods:

```python
async def auth_status(self, ctx) -> dict:
    ...

async def prepare_login(self, ctx) -> dict:
    ...

async def after_login(self, ctx) -> dict:
    ...
```

Do not require official-source login in Stage 1 plugins unless the user explicitly selects that plugin for login support. Stage 1 should define the contract and console UI hooks for login.

## Optional content-purification methods

```python
@classmethod
def get_ad_patterns(cls) -> list[str]:
    """Return source-specific ad/watermark regex patterns.

    These patterns supplement (not replace) the host's global fallback patterns.
    Patterns are compiled with re.IGNORECASE | re.MULTILINE and applied line-wise.
    """
    return [
        r"advertisement pattern 1",
        r"advertisement pattern 2",
    ]
```

## Smoke Fixture Contract

Each plugin should provide `tests/smoke.yaml`. Normal test and console smoke runs use local fixture files by default; live-network checks must be opt-in.

```yaml
keyword: 凡人修仙传
fixtures:
  search:
    url: https://example.com/search?q=%E5%87%A1%E4%BA%BA
    file: search.html
  detail:
    url: https://example.com/book/1/
    file: detail.html
  toc:
    url: https://example.com/book/1/
    file: toc.html
  chapter:
    url: https://example.com/book/1/1.html
    file: chapter.html
expect:
  search:
    minResults: 1
    firstName: 凡人修仙传
  detail:
    name: 凡人修仙传
    author: 忘语
    hasTocUrl: true
  toc:
    minChapters: 1
    firstTitleContains: 第
  chapter:
    minContentLength: 20
    titleContains: 第
```

Fixture files live under `tests/fixtures/` inside the plugin directory. URLs in `fixtures.*.url` must match the URLs requested by the plugin parser exactly. The fixture runner replaces network fetch with fixture fetch, but the plugin still calls only `ctx.access.http.fetch_text`, `ctx.access.http.fetch_json`, or `ctx.access.http.fetch_bytes`.

When a catalog or chapter needs follow-up page requests, add a top-level `extraFixtures` list. Each entry has the same `url` and `file` fields and maps pages beyond the four standard stages without adding a new smoke stage.

Smoke result shape:

```python
{
    "pluginId": "xbiqugu_la",
    "mode": "fixture",
    "pass": True,
    "stages": {
        "search": {"status": "ok", "count": 1, "elapsedMs": 3},
        "detail": {"status": "ok", "elapsedMs": 2},
        "toc": {"status": "ok", "count": 2, "elapsedMs": 2},
        "chapter": {"status": "ok", "contentLength": 120, "elapsedMs": 1},
    },
    "errors": [],
    "diagnostics": [],
}
```

Fixture smoke errors use normal plugin error codes plus:

- `SMOKE_CONTRACT_ERROR`: fixture output does not match the declared expectation.
- `SMOKE_FIXTURE_MISSING`: smoke spec or fixture files are missing or malformed.

## Standard Data Shapes

Search result:

```python
{
    "sourceId": "qidian",
    "name": "书名",
    "author": "作者",
    "bookUrl": "https://...",
    "coverUrl": "https://...",
    "intro": "简介",
    "kind": "分类/状态/标签",
    "lastChapter": "最新章节",
    "wordCount": "123万字",
    "score": 0.0,
    "extra": {},
}
```

Search result completeness is a source-plugin responsibility, not a scheduler
responsibility. A plugin should parse standard fields directly from the search
page whenever possible. If the search page does not expose `lastChapter`,
`author`, `coverUrl`, `intro`, `kind`, `wordCount`, or `updateTime`, the
plugin's own `search()` method must use the same source's `detail()` parser to
fill the missing fields for the returned candidates, usually the first few exact
or high-confidence matches. The runtime scheduler must not silently complete
business fields because single-source tests need to reveal source-specific
parser defects.

`kind` is reserved for book metadata that Reading can render as badges on the
full search page: category, status, tags, audience, rating, or other stable book
attributes. Do not put the source display name in `kind`, and do not return
generic provider labels such as `搜索提供器` as `kind`. LegadoHub's aggregate
source exposes source display through Reading-facing fields such as
`readingSourceName` and `readingLastChapter`, while preserving `kind` for
category/status/word-count style metadata.

When the aggregate source returns results, the runtime may write the original
`sourceName` into `kind` (for example `"笔趣阁22 / 玄幻"`) so Reading can show
the origin source for each result. Source plugins should not pre-populate `kind`
with the source name; leave that to the runtime or use it for real category/status
metadata.

The Reading search page should receive enough data to look useful without first
opening detail: `name`, `author`, `coverUrl`, `intro`, `kind`, `lastChapter`,
and `wordCount` should be populated whenever the source exposes them. If the
search page omits latest chapter, category, word count, or serial/completion
status but the detail page contains them, the plugin must enrich the returned
search candidates by calling its own `detail()` parser. This enrichment belongs
to the source plugin, not to the scheduler.

When detail enrichment is needed, keep it source-local and bounded:

- Enrich only candidates that already have a stable `bookUrl`.
- Prefer exact title matches before broad search results.
- Use a short timeout or catch failures so a detail-page failure does not erase
  a valid search result.
- Only fill fields that are empty; do not overwrite better search-page values
  unless the source has a clear reason.
- Mark source-local enrichment in `extra.detailEnriched = true` when useful for
  diagnostics.

For unstable searches, source plugins should degrade in this order: normal HTTP
search first, then browser-backed fetching when configured and available, then a
site-local ranking/category fallback that can still locate the requested title.
If all stable routes fail, return an empty list with diagnostics rather than
fabricated metadata.

A common implementation pattern is a private `_search_from_explore(ctx, keyword)`
method that scans the site's ranking, category, or recent-update pages for books
whose title contains the keyword. The main `search()` method tries the primary
search endpoint first and falls back to this helper when the primary endpoint
returns no results or fails. This fallback is local to the plugin and does not
require external search providers.

Explore group:

```python
{
    "sourceId": "qidian",
    "groupId": "rank_all",
    "title": "总排行榜",
    "url": "https://...",
    "kind": "rank",          # rank/category/full/new/other
    "pageable": True,
    "profile": "mobile",     # optional domain profile id
    "extra": {},
}
```

Explore item:

```python
{
    "sourceId": "qidian",
    "sourceName": "起点中文网",
    "name": "书名",
    "author": "作者",
    "bookUrl": "https://...",
    "coverUrl": "https://...",
    "intro": "简介",
    "kind": "分类/状态/榜单",
    "lastChapter": "最新章节",
    "wordCount": "123万字",
    "rank": 1,
    "groupId": "rank_all",
    "groupTitle": "总排行榜",
    "extra": {},
}
```

Explore items intentionally reuse the search-result shape where possible so the
same detail/toc/chapter reader verification can run after either discovery or
search.

Book detail:

```python
{
    "sourceId": "qidian",
    "name": "书名",
    "author": "作者",
    "bookUrl": "https://...",
    "coverUrl": "https://...",
    "intro": "简介",
    "kind": "分类/状态/标签",
    "lastChapter": "最新章节",
    "wordCount": "123万字",
    "updateTime": "2026-06-08",
    "tocUrl": "https://...",
    "authRequired": False,
    "extra": {},
}
```

Book detail should be as complete as the page allows because Reading/Legado maps
these fields directly through `ruleBookInfo`. `name`, `author`, `bookUrl`, and
`tocUrl` are the baseline. `coverUrl`, `intro`, `kind`, `lastChapter`,
`wordCount`, and `updateTime` should be populated whenever visible in HTML/JSON
or stable meta tags. Source-specific useful values such as `status`, raw tags, or
rating should go under `extra` instead of replacing the standard fields.
Plugins should clean SEO keyword tails, duplicate labels, and site chrome from
`intro`.

TOC item:

```python
{
    "sourceId": "qidian",
    "index": 1,
    "title": "第1章",
    "chapterUrl": "https://...",
    "updateTime": "",
    "isVip": False,
    "isLocked": False,
    "extra": {},
}
```

`toc()` must return chapters in **normal reading order** (chapter 1 first, latest
chapter last). If a site lists chapters in reverse order (latest first), the
plugin is responsible for reversing the list before returning. The runtime and
reading client do not attempt to auto-detect or fix chapter ordering.

`toc()` must return the complete readable catalog that the site exposes. Many
novel sites render a short static catalog containing only early chapters plus
recent chapters, then load the complete catalog through an AJAX/API endpoint,
pagination, or a "load more" script. Plugins must prefer the complete catalog
endpoint when present and treat the static head/tail preview as a fallback only.
If no complete catalog is available, the plugin should return the best available
list and trace the limitation. Do not report a preview list as a successful full
catalog.

Catalog parsers should deduplicate by chapter URL, remove `#` and navigation
links, and sort by explicit chapter number when the site can toggle between
normal and reverse order. A valid parser should preserve preface/extra chapters
when they are part of the actual reading order, but should exclude separate
"latest updates", recommendations, and related-book blocks.

Chapter content:

```python
{
    "sourceId": "qidian",
    "title": "第1章",
    "chapterUrl": "https://...",
    "content": "正文",
    "format": "text",
    "authRequired": False,
    "isPaid": False,
    "extra": {},
}
```

`format` is `"text"` for plain-text paragraphs separated by `\n\n`.
Plugins should pass raw chapter HTML through `ctx.clean_html()` to remove
scripts, ads, and site chrome before returning. Do not return raw HTML
with `format: "html"` unless the downstream consumer explicitly requires it.

Media chapters (sources whose `content.kind` is `audio` / `video`):

```python
{
    "sourceId": "demo_audio_books",
    "title": "第1集",
    "chapterUrl": "https://...",
    "content": "",                                    # always empty for media
    "format": "audio",                                # or "video"
    "mediaUrl": "https://cdn.example.com/ep1.m3u8",   # playable direct link or HLS playlist
    "mediaType": "application/vnd.apple.mpegurl",     # optional MIME hint
    "durationSeconds": 1820.5,                        # optional, seconds
    "authRequired": False,
    "isPaid": False,
    "extra": {},
}
```

Media chapter rules:

- `content` must be an empty string; put the playable reference in `mediaUrl`
  (direct mp3/mp4/m4a etc., or an m3u8 playlist).
- `mediaUrl` is served through the server-side media proxy
  (`/api/media/stream`) with Range passthrough; HLS playlists are rewritten
  automatically. Plugins never handle hotlink protection or proxying themselves.
- Only return media URLs whose host is covered by `domains`, `baseUrls`, or
  `content.streamDomains`; other hosts are rejected by the proxy.
- Audiobooks organize episodes as chapters; video sources likewise organize
  episodes/segments. `toc` still returns the complete ordered catalog.

Chapter content best practices:

1. **Preserve paragraphs** — convert `<br>` to `\n` and extract `<p>` tags as paragraphs joined by `\n\n`. If the site uses bare text nodes with `<br>`, split on blank lines after replacing `<br>`.
2. **Merge pagination** — many chapter pages are split into `_1.html`, `_2.html`, etc. Follow `下一章` / `下一页` / `#next_url` only while the URL stem matches the original chapter; stop when it points to the next chapter.
3. **Remove page markers** — strip `(1/3)`, `(2/3)`, `（第2页）` from the title.
4. **Clean ads aggressively** — remove `script/style/iframe/ins/nav/header/footer`, ad containers (`.contentadv`, `.bottom-ad`, `#txtright`), and short text nodes containing slogans such as "最新网址", "加入书签", "返回目录", "本章结束", or site names.
5. **Treat empty/polluted content as invalid** — do not return or cache obviously broken chapters. Return an empty string and trace the failure instead.
6. **Prefer API/AJAX/get endpoints** when available. Common signals are script variables, network paths containing `api`, `ajax`, `chapterlist`, `chapters`, `content`, `reader`, or mobile/AMP endpoints. Document any successful or failed probe in `ctx.trace()` so the next adapter pass does not repeat blind guesswork.

Auth status:

```python
{
    "sourceId": "qidian",
    "authenticated": False,
    "accountName": "",
    "expiresAt": "",
    "message": "未登录",
    "requiredActions": ["manual_login"],
}
```

Login preparation:

```python
{
    "sourceId": "qidian",
    "mode": "manual_browser",
    "loginUrl": "https://www.qidian.com",
    "instructions": "在打开的浏览器中完成登录，然后回到后台点击检测登录状态。",
    "cookieDomains": ["qidian.com"],
}
```

## Runtime Context API

Network (all access goes through ``ctx.access`` sub-facades):

```python
# Direct HTTP (httpx / curl_cffi)
await ctx.access.http.fetch_text(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.http.fetch_json(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.http.fetch_bytes(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)

# Stealth HTTP — browser-like headers + TLS impersonation (default chrome120)
await ctx.access.stealth.fetch_text(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.stealth.fetch_json(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.stealth.fetch_bytes(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)

# Browser rendering — Playwright-backed headless Chromium
await ctx.access.browser.fetch_text(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)
await ctx.access.browser.fetch_json(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)
await ctx.access.browser.fetch_bytes(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)

# Search provider — DDGS / Bing / Google
await ctx.access.search_provider(keyword, target_domain=..., url_patterns=..., provider_order=...)
```

`ctx.access.http` is the default path for ordinary requests.

`ctx.access.stealth` adds browser fingerprint headers and TLS impersonation
(uses `curl_cffi`). Plugins should use this for sites that block bare HTTP
clients.

`ctx.access.browser` uses Playwright Chromium for full browser rendering.
Use this only when JavaScript execution or complex challenge handling is
required. Runtime owns process lifecycle, proxy, timeout, profile cleanup,
and challenge classification. Plugins must not launch their own Playwright
instances.

There is **no automatic fallback** between layers. Plugins must explicitly
choose the access layer that matches their need.

Parsing:

```python
ctx.select(html_or_node, selector)
ctx.text(html_or_node, selector=None)
ctx.html(html_or_node, selector=None)
ctx.attr(html_or_node, selector, name)
ctx.json_path(data, path)
ctx.regex(text, pattern, group=1, default="")
```

Utilities:

```python
ctx.urljoin(base, href)
ctx.clean_html(html)
ctx.clean_text(text)
ctx.decode_text(content_bytes, charset=None)
ctx.trace(stage, url="", message="", data=None)
ctx.cache_get(key)
ctx.cache_set(key, value, ttl_seconds)
```

Text conversion (Traditional ↔ Simplified Chinese):

```python
ctx.to_simplified(value)
ctx.to_traditional(value)
```

For sites that serve Traditional Chinese (e.g. `69shuba.tw`, `twkan.com`),
plugins **must** apply bidirectional conversion:

1. **Input side** — convert the user keyword to Traditional before searching:
   ```python
   search_keyword = ctx.to_traditional(keyword)
   html = await ctx.access.http.fetch_text(
       f"{self.base_url}/search?kw={search_keyword}"
   )
   ```
2. **Output side** — convert every text field returned by the site to Simplified:
   ```python
   return {
       "name": ctx.to_simplified(name),
       "author": ctx.to_simplified(author),
       "intro": ctx.to_simplified(intro),
       "kind": ctx.to_simplified(kind),
       "lastChapter": ctx.to_simplified(last),
       # ...
   }
   ```

All user-facing string fields (`name`, `author`, `intro`, `kind`,
`lastChapter`, `title`, `content`, `groupTitle`, etc.) must go through
`ctx.to_simplified()`.  Internal values such as URLs, IDs, and timestamps
should not be converted.

Auth/session:

```python
ctx.cookies.get(domain, name=None)
ctx.cookies.set(domain, cookie)
ctx.cookies.clear(domain=None)
await ctx.auth_status()
await ctx.request_manual_login(login_url, cookie_domains, message="")
```

`ctx.cookies` is a host-provided Cookie payload abstraction; the host persists the payload to `backend/config/cookies/<plugin_id>.json`. Plugins must not read or write `Cookie.json` inside the plugin directory.

`auth_status` is probed in real time by the plugin; login/auth state is not written to the database. The host may cache the Cookie file, but the plugin decides whether the Cookie is valid.

Browser/manual-login support is a controlled runtime feature. Plugins may request it, but the console/backend decides how to present and execute it.

## Error Codes

Plugins should raise or return structured failures that core normalizes to:

- `PLUGIN_VALIDATION_ERROR`
- `PLUGIN_RUNTIME_ERROR`
- `PLUGIN_TIMEOUT`
- `FETCH_NETWORK_ERROR`
- `FETCH_HTTP_4XX`
- `FETCH_HTTP_5XX`
- `PARSE_EMPTY`
- `PARSE_ERROR`
- `AUTH_REQUIRED`
- `LOGIN_REQUIRED`
- `PAID_CONTENT_REQUIRED`
- `BROWSER_REQUIRED`
- `CLOUDFLARE_REQUIRED`
- `RATE_LIMITED`
- `UNSUPPORTED_SOURCE`

Do not hide auth or payment requirements as parse failures.

## Official And Login-Based Sources

Official sources such as 起点、番茄、七猫、QQ 阅读 may require login, cookies, anti-bot checks, paid-chapter access, or mobile/API signatures.

The plugin contract supports them through:

- `auth` metadata.
- `auth_status`.
- `prepare_login`.
- `after_login`.
- `ctx.cookies`.
- `ctx.request_manual_login`.
- `authRequired`, `isVip`, `isLocked`, and `isPaid` output fields.
- console UI actions for "打开登录", "检测登录状态", "清除 Cookie", "重试章节".

### Browser Challenge Bypass

When `ctx.access.http.fetch_text/json/bytes` explicitly identifies and raises
`CLOUDFLARE_REQUIRED`, the host starts one browser session through the existing
Access Bridge. The browser reuses a stable profile keyed by plugin, domain, and
proxy identity and uses the same UA and proxy path as HTTP. After its cookies
are injected into the current plugin context, the original HTTP request is
retried once. If the browser is unavailable, the challenge remains, or the
retry fails, the scheduler records the normalized failure and skips that source.

Runtime does not create user-facing verification pages, Reading callback URLs,
or Cookie round-trip APIs. `BROWSER_REQUIRED`, captchas, and persistent
challenges are not retried indefinitely. A plugin may declare `browser.mode:
required` and may raise the structured error, but it must not own browser
process control, retry scheduling, concurrency, timeout, proxy, cache, or cookie
persistence policy. Browser cookies are always available to the current request
and are written to the host CookieStore only when `auth.cookieDomains` is declared.

Browser simulation remains a backend-owned capability for maintainable bypass
strategies, stealth HTTP, search-provider access, and controlled rendering. It
is not a user-facing manual verification environment.

Some sites require browser context for normal reads. For these, metadata may
declare `browser.mode: required`, and plugin code may use
`ctx.access.browser.fetch_text(...)`. Runtime gives these sources a separate
`browser_source_timeout_seconds` budget while ordinary HTTP sources keep the
normal `source_timeout_seconds` budget.

For search specifically, source plugins that encounter challenge pages or
unstable direct HTML should degrade in this order unless a source documents a
better site-specific route:

1. Normal source search through the declared access layer.
2. Browser-rendered search for the same site search URL when a challenge or
   JavaScript-rendered result page is detected.
3. Source-local discovery fallback such as the site's own ranking, category,
   recent-update, or complete-book pages.
4. External search provider fallback only as a final source-owned bypass, and
   only when the plugin declares and maps that provider explicitly.

HTTP 200 responses can still be challenge pages. Plugins should check returned
HTML for Cloudflare/Turnstile/browser-challenge markers before treating a parse
miss as an empty result.

Stage 1 requirement:

- Implement the contract and console UI hooks.
- Do not require working paid-content extraction from official sources.
- If an official source plugin is created, it may support search/detail/toc for free metadata first and return `AUTH_REQUIRED` or `PAID_CONTENT_REQUIRED` for locked chapters.

## Multi-Domain And Proxy Source Rules

A plugin represents one site family, not necessarily one hostname. It may keep
multiple mirror or historical domains in one plugin when the parsing rules are
substantially identical and the plugin can safely fall back from one base URL to
another.

Rules:

- If a single domain/profile cannot be reached, the plugin may try another
  declared domain profile for the same lifecycle method.
- Fallback is local to site-specific URL construction and parsing. Global
  concurrency, timeout, retry, proxy, cache, and health scoring remain runtime
  responsibilities.
- If mobile and desktop domains have substantially different DOM/API shapes,
  prefer separate plugins or clearly separated internal profiles. Do not mix two
  unrelated parser shapes in one hard-to-maintain method.
- If two domains share a brand but have different verification behavior and DOM,
  such as `www.69shuba.com` and `69shuba.tw`, they should be separate plugins
  rather than one multi-domain plugin.
- `metadata.proxy.required: true` marks sources that should normally use the
  runtime proxy path. Plugins still call only `ctx.fetch_*`; they must not create
  their own proxy clients.
- `metadata.proxy.mode` may be `auto`, `always`, or `never`. Runtime code decides
  how to honor it with the configured proxy URL.

The live acceptance path for sources with `explore` is:

1. `explore_groups`
2. `explore` to pick a discovered/ranked book
3. `detail`
4. `toc`
5. `chapter`
6. `search` using the discovered book name
7. repeat `detail -> toc -> chapter` from the search candidate

## Compatibility Strategy

The contract is versioned.

- Stage 1 accepts only `contractVersion: "1.0"`.
- Future engines can add optional fields.
- Existing fields must not be renamed without a migration.
- New lifecycle methods must be optional.
- Old plugins should keep working when new capabilities are added.

The aggregate Reading/Legado source remains a compatibility shell. It should not expose plugin internals directly.

## Dependency Policy

This is a private project. If a plugin or frontend task needs a dependency, install it and record it.

Rules:

- Backend Python dependencies go in `backend/requirements.txt`.
- Frontend dependencies go in `frontend/package.json`.
- Prefer shared runtime dependencies over per-plugin duplication.
- If a plugin needs a special dependency, add `requirements.txt` in the plugin directory and document it in the plugin README.
- Report dependency installation failures with the exact command and error.
