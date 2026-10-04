# LegadoHub 书源插件协议

> **给 AI / 开发者：** 本文档定义了书源插件 API。实现时请不要自行发明接口。如果某个站点需要协议未覆盖的行为，先向本文档添加向后兼容的扩展，再实现。

**协议版本：** `1.0`

**运行时归属：** LegadoHub 核心。

**插件归属：** 站点特定的适配代码。

---

## 核心原则

书源插件描述一个站点或同族站点的适配方式，而不是控制引擎。

## 宿主层边界

重构后，以下职责明确归属宿主层，插件不得绕过或重复实现：

1. **配置集中化**：运行时配置统一由宿主从 `backend/config/app_config.json` 加载。旧的 `backend/config/source_pool.json`、`backend/config/aggregate_source.json` 和 `backend/data/ai_provider.json` 已废弃。
2. **Cookie 文件归属宿主**：Cookie 由宿主统一保存到 `backend/config/cookies/<plugin_id>.json`。插件不再读取或写入自身目录内的 `Cookie.json`。宿主只负责不透明的加载、保存和清除；JSON 载荷结构由插件自行定义。
3. **认证状态实时探测**：登录/认证状态不持久化到数据库。`auth_status` 由插件实时探测。宿主可以缓存 Cookie 载荷文件，但是否有效由插件判定。
4. **搜索事件/插件健康/运行时代理状态不持久化**：搜索过程事件由 `SearchCoordinator` 在内存中维护；进程日志与调试数据写入 `backend/runtime/logs/`，不进入数据库。
5. **代理策略收紧**：默认直连。仅当插件声明 `proxy.mode: always`，或 `proxy.mode: auto` 且 `proxy.required: true` 且宿主配置 `proxy.allowAutoRetry: true` 时才使用代理。`auto` 不再表示“每个源都先尝试代理”。
6. **搜索调度归属宿主**：插件不应假设搜索过程事件会被持久化，也不应自行调度搜索重试。

插件可以：

- 构造站点特定的 URL。
- 解析 HTML/JSON/API 响应。
- 生成站点特定的签名或 token。
- 解密章节载荷。
- 描述认证要求。
- 描述站点族域名配置，并在解析时动态选择。
- 通过 `ctx` 请求受控的浏览器/手动登录协助。

插件不可以：

- 控制书源级并发。
- 创建不受管理的后台线程。
- 创建绕过 `ctx` 的全局 HTTP 会话。
- 拥有自己的重试/代理/缓存策略。
- 塑造 Reading/Legado 聚合书源 JSON。
- 导入或依赖 `engine-jvm`、`app.legado_engine` 或 `app.engine`。

## 目录结构

```text
plugins/sources/<plugin_id>/
  metadata.yaml
  source.py
  README.md
  requirements.txt       # 可选；私有项目，按需安装
  smoke/
    smoke.yaml
    fixtures/
      search.html
      detail.html
      toc.html
      chapter.html
```

`smoke/` 是正式布局。运行时仍兼容历史 `tests/smoke.yaml`，但新插件和发生解析变更的旧插件不得继续使用旧布局。新增的后端依赖记录在 `backend/requirements.txt`；只有无法由宿主共享的插件依赖才放入插件自身的 `requirements.txt`。

## metadata.yaml

必填字段：

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

必填字段规则：

- `contractVersion`：第一阶段必须为 `"1.0"`。
- `id`：稳定的 ASCII 标识符，在所有插件中唯一。
- `name`：中文显示名，尽可能使用中文。
- `version`：插件版本，采用类 SemVer 格式（如 `0.1.0`）。解析规则、域名配置或访问策略变更时都需要升级版本。
- `type`：必须为 `source`。
- `domains`：插件允许或预期访问的域名。
- `baseUrls`：站点或站点族的入口 URL。
- `capabilities`：`search`、`detail`、`toc`、`chapter`、`chapter_reviews`、`book_reviews`、`explore`、`auth` 的子集。
- `auth.mode`：`none`、`optional`、`required`、`manual` 之一。
- `content.access`：`free`、`paid`、`mixed`、`unknown` 之一。
- `content.kind`：`text`（默认）、`audio`、`video` 之一。声明本书源章节内容的媒体类型：有声书源设为 `audio`，影视/短剧源设为 `video`。媒体书源的 `search`/`detail`/`toc`/`chapter` 生命周期与文字书源完全一致，仅章节载荷不同（见下文"章节内容"）。
- `content.streamDomains`（可选，媒体书源建议填写）：音频/视频文件实际所在的 CDN/流媒体域名列表。播放走本站媒体代理，代理只允许指向 `domains`、`baseUrls` 主机与该列表内的地址。
- `tags`：运营提示标签。

`explore` 涵盖排行榜、分类、热榜、完本榜等发现入口。仅允许官方/授权书源使用。当书源带有 `official` 标签或 `content.sourceRole: official` 时被视为官方书源。普通镜像/爬虫书源只能暴露 `search`、`detail`、`toc`、`chapter`，即使站点有这些页面也不得声明排行榜或分类能力。

`ui`（可选，声明式源设置）：插件在 metadata 中声明配置项与动作，控制台
「书源管理 → 源设置」自动渲染（仅管理员），值持久化在服务端并通过
`ctx.settings.get(key, default)` 注入插件运行时；动作调用时还通过
`payload["values"]` 携带当前生效值。

```yaml
ui:
  - title: 游客身份
    description: 设备指纹与 token 均保存在服务端。
    items:
      - { type: button, id: status, label: 查看状态, action: ui_status }
      - { type: text,   id: api_key, label: API 密钥, help: 可选 }
      - { type: toggle, id: verbose, label: 详细日志, default: false }
      - { type: number, id: limit, label: 单页条数, default: 20 }
      - { type: select, id: mode, label: 模式, choices: [fast, full], default: fast }
      - { type: color,  id: accent, label: 主题色, default: "#E34B4B" }
      - { type: hint,   label: 设置仅管理员可见可改 }
```

- 条目类型：`button`、`toggle`、`text`、`number`、`select`、`color`、`hint`；同一 `ui` 内非 hint 条目的 `id` 必须唯一，`label` 必填。
- `button.action` 必须是 Source 上可调用的同名 async 方法：
  `async def ui_status(self, ctx, payload) -> dict`，返回
  `{"ok": bool, "message": str, "data": {...}}`（返回字符串按 message 处理）；
  `payload` 含 `values`（当前生效设置）、`action` 与 `input`（控制台附加输入）。
- 动作只能由管理员从控制台触发（走插件限流与超时），读取端（读者）无感知；
  插件不得在 `ui` 中承载面向读者的内容配置。

可选字段：

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
```

`author` 是控制台展示的插件维护者署名，不是 `search()` 或 `detail()` 返回的书籍作者。仓库内随镜像发布的第三方插件统一写 `Yunwei`。

正式插件的 metadata 还必须满足：

- 每个键只允许出现一次；重复的 `enabled`、`proxy` 等键必须由校验器拒绝，不能依赖 YAML 的后写覆盖。
- `enabled`、`rateLimit.perHostConcurrency`、`rateLimit.minIntervalMs`、`proxy.mode`、`proxy.required` 必须明确声明。
- `rateLimit` 必须来自有记录的逐级探测；未探测时使用保守值 `1 / 1200ms`，不得凭感觉提高。
- 使用繁体查询或输出时声明 `language: zh-TW` 或 `traditional-chinese` 标签，并实现输入转繁、输出转简。
- 访问策略、域名、解析规则、编码策略或正文净化发生行为变化时必须升级 `version`；只改 README 不升级。

`enabled`：加载后默认是否启用。控制台可以按书源单独开关。

`proxy.mode`：
- `never`：永不使用运行时配置的代理（默认行为）。
- `auto`：仅当 `proxy.required: true` 且宿主配置 `proxy.allowAutoRetry: true` 时才允许代理；不满足条件时保持直连。
- `always`：始终通过配置的代理路由。

`proxy.required`：若为 `true`，表示该书源通常需要代理，但是否启用仍由 `proxy.mode` 与宿主 `proxy.allowAutoRetry` 共同决定。

`browser.mode`：
- `none`：不使用浏览器渲染。
- `optional`：先尝试 HTTP/stealth，遇到挑战或 JS 渲染页面时再 fallback 到 `ctx.access.browser`。
- `required`：始终使用 `ctx.access.browser.fetch_*`。

`browser.reason`：简短诊断标签，如 `cloudflare`、`js_rendered`、`login`。

`accessStrategy` 是每个生命周期阶段可选的最终运行路由。合法路由包括 `http`、`stealth_http`、`tls_impersonate`、`search_provider`、`headless_browser`、`remote_browser`、`api`、`feed`、`local_file`。完成的书源应声明预期路由，而不是保留隐藏的永久 fallback 链。

`searchProvider` 是可选的搜索引擎代理配置。DuckDuckGo 使用 DDGS 库提供方，Bing 和 Google 使用 HTTP 结果页提供方。声明的提供方并行执行，运行时不添加隐式 fallback。单个书源插件只需将返回的命中结果映射为书源结果对象。

## source.py 类

每个插件导出一个 `Source` 类。

必填类字段：

```python
class Source:
    id = "qidian"
    name = "起点中文网"
    contract_version = "1.0"
    last_modified = "2026-06-10"
```

- `id`：稳定的 ASCII 标识符，必须与目录名和 `metadata.yaml` 一致。
- `name`：中文显示名。
- `contract_version`：第一阶段必须为 `"1.0"`。
- `last_modified`：ISO-8601 日期（`YYYY-MM-DD`），表示插件逻辑最后被验证或更新的时间。控制台 UI 会显示此字段以便运营人员发现陈旧书源。

生命周期方法在 metadata capabilities 中声明时必须为 async：

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

`explore` 是书源发现生命周期，覆盖 Reading/Legado 的 `enabledExplore`、`exploreUrl` 和 `ruleExplore`，例如排行榜、分类页、完本榜、新书架。声明 `explore` 的插件必须同时提供 `explore_groups` 和 `explore`，加载器会验证这对方法。

可选的认证方法：

```python
async def auth_status(self, ctx) -> dict:
    ...

async def prepare_login(self, ctx) -> dict:
    ...

async def after_login(self, ctx) -> dict:
    ...
```

第一阶段不要要求官方书源登录，除非用户明确选择该书源支持登录。第一阶段应定义协议和控制台 UI 挂钩。

## Smoke Fixture 协议

每个正式插件必须提供 `smoke/smoke.yaml`。常规测试和控制台 smoke 运行默认使用本地 fixture 文件；实时网络检查必须显式选择加入。

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
    complete: true
    expectedCount: 438
    minChapters: 1
    firstTitleContains: 第
    lastTitleContains: 后记
    requireUniqueChapterUrls: true
    requireSequentialIndexes: true
  chapter:
    minContentLength: 20
    titleContains: 第
```

Fixture 文件放在插件目录下的 `smoke/fixtures/` 中。`fixtures.*.url` 中的 URL 必须与插件解析器请求的 URL 完全匹配。Fixture 运行器会替换 HTTP、stealth、browser 与搜索提供器背后的宿主访问门面，但插件代码仍只能调用 `ctx.access.*`。

仅依赖结构化搜索提供器的插件可在顶层保存 `searchProviderHits`，字段与宿主 `SearchProviderHit` 一致。该列表必须来自一次真实搜索提供器响应，并在插件 README 记录抓取时间；fixture 模式不会调用外部搜索引擎。

若目录首项是公告、引子或其他合法短章，可在 `expect.chapter.sampleIndex` 指定从 1 开始的正文抽样序号；默认值为 1。该字段只改变正文抽样，不得用于绕过目录首尾和完整数量校验。

目录或正文需要继续请求分页时，可在顶层增加 `extraFixtures` 列表。每项同样包含 `url` 和 `file`，用于映射标准四阶段之外的后续页面；它不增加新的 smoke 阶段。

### 完整目录校验（正式第三方书源必需）

`toc()` 的“能返回章节”不等于“返回完整目录”。正式第三方书源的 fixture smoke 必须在
`expect.toc` 声明 `complete: true`，并同时提供：

- `expectedCount`：当前 fixture 中完整目录的精确章节数；不能只写下限。
- `firstTitleContains` 与 `lastTitleContains`：首章、尾章锚点，防止只取到开头或最新预览区。
- `requireUniqueChapterUrls: true`：重复章节 URL 直接失败。
- `requireSequentialIndexes: true`：返回序号必须从 1 连续递增。

分页/AJAX/加载更多目录必须将**所有实际访问的目录页**加入 `extraFixtures`。只保存第一页的
fixture 不得标记为 `complete`。旧插件在下一次维护时必须迁移到此规则；新插件和目录逻辑变更
必须立即采用。实时验收还要将 `detail.lastChapter` 与 `toc()` 尾章对照；若站点自身落后官方源，
应标记为候补源滞后，不能伪称为插件解析完整。

Smoke 结果格式：

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

Fixture smoke 错误使用常规插件错误码外加：

- `SMOKE_CONTRACT_ERROR`：fixture 输出与声明的预期不匹配。
- `SMOKE_FIXTURE_MISSING`：smoke 规范或 fixture 文件缺失或格式错误。

## 标准数据结构

搜索结果：

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

搜索结果的完整性是书源插件的责任，不是调度器的责任。插件应尽可能直接从搜索页面解析标准字段。如果搜索页面不暴露 `lastChapter`、`author`、`coverUrl`、`intro`、`kind`、`wordCount` 或 `updateTime`，插件自身的 `search()` 方法必须使用同一书源的 `detail()` 解析器为前几个精确或高置信度匹配补全缺失字段。运行时调度器不得静默补全行业字段，因为单源测试需要暴露书源特定的解析器缺陷。

`kind` 保留给 Reading 在完整搜索页上渲染为徽章的书籍元数据：分类、状态、标签、受众、评分或其他稳定的图书属性。不要把书源显示名放进 `kind`，也不要返回通用提供方标签如 `搜索提供器`。LegadoHub 的聚合书源通过 Reading 面向的字段（如 `readingSourceName` 和 `readingLastChapter`）暴露书源显示，同时保留 `kind` 用于分类/状态/字数类元数据。

当聚合书源返回结果时，运行时可能会将原始 `sourceName` 写入 `kind`（例如 `"笔趣阁22 / 玄幻"`），以便 Reading 显示每条结果的来源书源。书源插件不应预先用书源名填充 `kind`；请将其留给运行时，或仅用于真实的分类/状态元数据。

Reading 搜索页应在不先打开详情的情况下获得足够数据：`name`、`author`、`coverUrl`、`intro`、`kind`、`lastChapter` 和 `wordCount` 应在书源暴露它们时被填充。如果搜索页缺少最新章节、分类、字数或连载/完本状态，但详情页包含它们，插件必须通过调用自身的 `detail()` 解析器来丰富返回的搜索候选。这种丰富属于书源插件，而不是调度器。

详情丰富应保持书源本地化和有界：

- 仅丰富已有稳定 `bookUrl` 的候选。
- 优先精确标题匹配，再考虑宽泛搜索结果。
- 使用短超时或捕获失败，避免详情页失败抹掉有效搜索结果。
- 只填充空字段；不要覆盖更好的搜索页值，除非书源有明确理由。
- 在有用时通过 `extra.detailEnriched = true` 标记书源本地丰富。

对于不稳定的搜索，书源插件应按以下顺序降级：先正常 HTTP 搜索，然后在配置和可用时使用浏览器支持的获取，再使用站点本地排行榜/分类 fallback 来定位请求标题。如果所有稳定路由都失败，返回空列表并附带诊断，而不是伪造元数据。

常见的实现模式是提供一个私有的 `_search_from_explore(ctx, keyword)` 方法，扫描站点的排行榜、分类或最近更新页面，查找书名包含关键词的书籍。主 `search()` 方法先尝试主搜索端点，当初级端点返回无结果或失败时回退到此辅助方法。此 fallback 是插件本地的，不需要外部搜索引擎代理。

发现分组：

```python
{
    "sourceId": "qidian",
    "groupId": "rank_all",
    "title": "总排行榜",
    "url": "https://...",
    "kind": "rank",          # rank/category/full/new/other
    "pageable": True,
    "profile": "mobile",     # 可选域名配置 id
    "extra": {},
}
```

发现项：

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

发现项故意复用搜索结果形状，以便相同的 detail/toc/chapter 读取器验证可以在发现或搜索后运行。

书籍详情：

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

书籍详情应尽可能完整，因为 Reading/Legado 直接通过 `ruleBookInfo` 映射这些字段。`name`、`author`、`bookUrl` 和 `tocUrl` 是基线。`coverUrl`、`intro`、`kind`、`lastChapter`、`wordCount` 和 `updateTime` 在 HTML/JSON 或稳定 meta 标签中可见时都应填充。书源特定但可能有用的值（如 `status`、原始标签或评分）应放在 `extra` 下，而不是替换标准字段。插件应清理 `intro` 中的 SEO 关键词尾巴、重复标签和站点导航杂项。

目录项：

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

`toc()` 必须按**正常阅读顺序**返回章节（第1章在前，最新章节在后）。如果站点按倒序排列（最新在前），插件必须在返回前反转列表。运行时和阅读客户端不会尝试自动检测或修复章节顺序。

`toc()` 必须返回站点暴露的完整可读目录。许多小说站点只渲染一个短的静态目录（包含开头几章和最近几章），然后通过 AJAX/API 端点、分页或"加载更多"脚本加载完整目录。插件必须优先使用完整目录端点，并将静态头/尾预览仅作为 fallback。如果没有完整目录，插件应返回最佳可用列表并跟踪限制。不要将预览列表报告为成功的完整目录。

目录解析器应按章节 URL 去重，移除 `#` 和导航链接，并在站点可在正常/倒序之间切换时按显式章号排序。有效的解析器应保留作为实际阅读顺序一部分的序言/额外章节，但应排除独立的"最新更新"、推荐和相关书籍块。

在编写 `toc()` 或 `chapter()` 的选择器之前，主动检查页面是否有 JSON/AJAX/get 端点。常见信号包括脚本变量、包含 `api`、`ajax`、`chapterlist`、`chapters`、`content`、`reader` 的网络路径，或移动/AMP 端点。优先使用稳定的 API/AJAX 响应而不是 HTML 解析获取完整目录和正文内容；HTML 选择器仅作为文档化的 fallback。如果端点探测返回 404/500/挑战页面，将探测结果保留在验证笔记或 `ctx.trace()` 中，以便下一次适配迭代不会重复盲目猜测。

重复镜像框架常出现相同的陷阱：

- "最新章节"预览出现在真实目录之前。按区块边界解析，而不是仅使用宽泛选择器如 `#list a`。
- 某些 HTML 结构为 `<a><dd>title</dd></a>` 而不是 `<dd><a>title</a></dd>`。在假设框架形状之前，针对实时 DOM 验证。
- 分页目录可能使用兄弟 URL 如 `/book/123-2.html`，而不是子 URL 如 `/book/123/123-2.html`。跟随实际的 `下一页` 链接。
- 不要仅按章号去重。`番外`、`完结感言` 等无编号章节必须保留。
- 章节页面可能包含短的广告 `<p>` 加真实文本节点。不要让"第一个 `<p>` 获胜"的逻辑丢弃正文。
- 如果来源页面本身返回缺失、串章、混章正文，插件应跟踪或为该章节返回空，而不是将明显被污染的内容缓存为有效文本。解析器不得伪造缺失正文。

章节内容：

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

`format` 为 `"text"` 时表示以 `\n\n` 分隔的纯文本段落。插件应通过 `ctx.clean_html()` 传递原始章节 HTML 以移除脚本、广告和站点导航，然后再返回。除非下游消费者明确要求，否则不要返回原始 HTML 并使用 `format: "html"`。

媒体章节（`content.kind` 为 `audio` / `video` 的书源）：

```python
{
    "sourceId": "demo_audio_books",
    "title": "第1集",
    "chapterUrl": "https://...",
    "content": "",                      # 媒体章节正文恒为空
    "format": "audio",                  # 或 "video"
    "mediaUrl": "https://cdn.example.com/ep1.m3u8",   # 可播放的直链或 HLS 播放列表
    "mediaType": "application/vnd.apple.mpegurl",     # 可选，MIME 提示
    "durationSeconds": 1820.5,          # 可选，时长（秒）
    "authRequired": False,
    "isPaid": False,
    "extra": {},
}
```

媒体章节规则：

- `content` 必须为空字符串；可播放地址放在 `mediaUrl`（直链 mp3/mp4/m4a 等，或 m3u8 播放列表）。
- `mediaUrl` 会被服务端媒体代理（`/api/media/stream`）按 Range 续传转发，HLS 播放列表会被自动重写；插件无需自行处理防盗链或代理。
- 仅返回 `domains`、`baseUrls` 或 `content.streamDomains` 声明域名内的媒体地址，其他地址会被代理拒绝。
- 有声书按"集"组织为章节；视频源同理按"集/话"组织，`toc` 仍返回完整顺序目录。

书评能力（可选，`book_reviews`）：

```python
{
    "summary": {"peopleCount": "585"},   # 评分概览（番茄无评分则只回总量）
    "items": [
        {
            "id": "1", "userName": "书友", "avatar": "https://...",
            "content": "评论文本", "likeCount": 20819, "replyCount": 12,
            "time": "2024-02 四川", "rating": "10",
        }
    ],
    "extra": {}, "debug": {},
}
```

- `book_reviews(ctx, book_url)` 按书聚合返回书评列表；`summary.peopleCount` 为平台展示的总量（字符串原样透传）。
- 平台在聚合书源正文末尾追加「书评区」入口卡片（阅读C/Max 客户端 showBrowser 打开
  `/api/legado/book/{id}/reviews/view` 渲染页）；未声明该能力的书源自动跳过。

章节正文最佳实践：

1. **保留段落** — 将 `<br>` 转换为 `\n`，将 `<p>` 标签提取为以 `\n\n` 连接的段落。如果站点使用带 `<br>` 的裸文本节点，在替换 `<br>` 后按空行分割。
2. **合并分页** — 许多章节页面被拆分为 `_1.html`、`_2.html` 等。仅在 URL stem 匹配原始章节时跟随 `下一章` / `下一页` / `#next_url`；指向下一章时停止。
3. **移除页码标记** — 从标题中去掉 `(1/3)`、`(2/3)`、`（第2页）`。
4. **积极清洗广告** — 移除 `script/style/iframe/ins/nav/header/footer`、广告容器（`.contentadv`、`.bottom-ad`、 `#txtright`）以及包含"最新网址"、"加入书签"、"返回目录"、"本章结束"或站点名的短文本节点。
5. **将空/污染内容视为无效** — 不要返回或缓存明显损坏的章节。返回空字符串并跟踪失败。
6. **优先使用 API/AJAX/get 端点**。常见信号是脚本变量、包含 `api`、`ajax`、`chapterlist`、`chapters`、`content`、`reader` 的网络路径，或移动/AMP 端点。在 `ctx.trace()` 中记录任何成功或失败的探测，以便下一次适配迭代不会重复盲目猜测。

认证状态：

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

登录准备：

```python
{
    "sourceId": "qidian",
    "mode": "manual_browser",
    "loginUrl": "https://www.qidian.com",
    "instructions": "在打开的浏览器中完成登录，然后回到后台点击检测登录状态。",
    "cookieDomains": ["qidian.com"],
}
```

## 运行时上下文 API

网络（所有访问都通过 `ctx.access` 子门面）：

```python
# 直接 HTTP（httpx / curl_cffi）
await ctx.access.http.fetch_text(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.http.fetch_json(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.http.fetch_bytes(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)

# Stealth HTTP — 浏览器级 headers + TLS 指纹伪装（默认 chrome120）
await ctx.access.stealth.fetch_text(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.stealth.fetch_json(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)
await ctx.access.stealth.fetch_bytes(url, method="GET", params=None, data=None, json=None, headers=None, timeout=None, impersonate=None, proxy=True)

# 浏览器渲染 — Playwright 无头 Chromium
await ctx.access.browser.fetch_text(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)
await ctx.access.browser.fetch_json(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)
await ctx.access.browser.fetch_bytes(url, method="GET", headers=None, data=None, wait_ms=2500, timeout_ms=90000)

# 搜索引擎代理 — DDGS / Bing / Google
await ctx.access.search_provider(keyword, target_domain=..., url_patterns=..., provider_order=...)
```

`ctx.access.http` 是普通请求的默认路径。

`ctx.access.stealth` 添加浏览器指纹 headers 和 TLS 伪装（使用 `curl_cffi`）。插件应在站点阻止裸 HTTP 客户端时使用此层。

`ctx.access.browser` 使用 Playwright Chromium 进行完整浏览器渲染。仅在需要 JavaScript 执行或复杂挑战处理时使用。运行时拥有进程生命周期、代理、超时、配置清理和挑战分类。插件不得启动自己的 Playwright 实例。

各层之间**没有自动 fallback**。插件必须显式选择与需求匹配的访问层。

解析：

```python
ctx.select(html_or_node, selector)
ctx.text(html_or_node, selector=None)
ctx.html(html_or_node, selector=None)
ctx.attr(html_or_node, selector, name)
ctx.json_path(data, path)
ctx.regex(text, pattern, group=1, default="")
```

工具：

```python
ctx.urljoin(base, href)
ctx.clean_html(html)
ctx.clean_text(text)
ctx.decode_text(content_bytes, charset=None)
ctx.trace(stage, url="", message="", data=None)
ctx.cache_get(key)
ctx.cache_set(key, value, ttl_seconds)
```

文本转换（繁简中文互转）：

```python
ctx.to_simplified(value)
ctx.to_traditional(value)
```

对于提供繁体中文的站点（如 `69shuba.tw`、`twkan.com`），插件**必须**应用双向转换：

1. **输入侧** — 搜索前将用户关键词转为繁体：
   ```python
   search_keyword = ctx.to_traditional(keyword)
   html = await ctx.access.http.fetch_text(
       f"{self.base_url}/search?kw={search_keyword}"
   )
   ```
2. **输出侧** — 将站点返回的每个文本字段转为简体：
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

所有面向用户的字符串字段（`name`、`author`、`intro`、`kind`、`lastChapter`、`title`、`content`、`groupTitle` 等）都必须经过 `ctx.to_simplified()`。URL、ID、时间戳等内部值不应转换。

认证/会话：

```python
ctx.cookies.get(domain, name=None)
ctx.cookies.set(domain, cookie)
ctx.cookies.clear(domain=None)
await ctx.auth_status()
await ctx.request_manual_login(login_url, cookie_domains, message="")
```

`ctx.cookies` 是宿主提供的 Cookie 载荷抽象，宿主负责将载荷持久化到 `backend/config/cookies/<plugin_id>.json`。插件不应直接读写插件目录内的 `Cookie.json`。

`auth_status` 由插件实时探测；登录/认证状态不写入数据库。宿主可以缓存 Cookie 文件，但 Cookie 是否有效由插件自行判断。

浏览器/手动登录支持是受控的运行时功能。插件可以请求它，但控制台/后端决定如何呈现和执行。

## 错误码

插件应抛出或返回结构化失败，核心会规范化为：

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

不要将认证或付费需求隐藏为解析失败。

## 官方与登录型书源

官方书源如起点、番茄、七猫、QQ 阅读可能需要登录、cookie、反 bot 检查、付费章节访问或移动/API 签名。

插件协议通过以下方式支持它们：

- `auth` metadata。
- `auth_status`。
- `prepare_login`。
- `after_login`。
- `ctx.cookies`。
- `ctx.request_manual_login`。
- `authRequired`、`isVip`、`isLocked`、`isPaid` 输出字段。
- 控制台 UI 动作："打开登录"、"检测登录状态"、"清除 Cookie"、"重试章节"。

### 浏览器挑战绕过

当 `ctx.access.http.fetch_text/json/bytes` 明确识别并抛出 `CLOUDFLARE_REQUIRED` 时，宿主会用现有 Access Bridge 启动一次浏览器会话。浏览器按插件、域名和代理身份复用稳定 profile，并与 HTTP 使用相同 UA 和代理出口；Cookie 注入当前插件上下文后，原 HTTP 请求只重试一次。若浏览器不可用、挑战仍存在或重试仍失败，调度器记录规范化失败并在当前请求中跳过该书源。

运行时不创建面向用户的验证页面、Reading 回调 URL 或 Cookie 往返 API。`BROWSER_REQUIRED`、验证码和持续挑战不会无限重试。插件可以声明 `browser.mode: required` 并可以抛出结构化错误，但它不得拥有浏览器进程控制、重试调度、并发、超时、代理、缓存或 cookie 持久化策略。浏览器 Cookie 始终可用于当前请求；只有插件声明 `auth.cookieDomains` 时才写入宿主 CookieStore。

浏览器模拟仍然是后端拥有的能力，用于可维护的绕过策略、stealth HTTP、搜索引擎代理和受控渲染。它不是面向用户的手动验证环境。

有些站点正常阅读就需要浏览器上下文。对于这些站点，metadata 可以声明 `browser.mode: required`，插件代码可以使用 `ctx.access.browser.fetch_text(...)`。运行时给这些书源单独的 `browser_source_timeout_seconds` 预算，而普通 HTTP 书源保持正常的 `source_timeout_seconds` 预算。

对于搜索，遇到挑战页面或不稳定的直接 HTML 时，书源插件应按以下顺序降级，除非书源文档化了更好的站点特定路由：

1. 通过声明的访问层进行正常书源搜索。
2. 当检测到挑战或 JavaScript 渲染的结果页时，对同一站点搜索 URL 进行浏览器渲染搜索。
3. 站点本地发现 fallback，如站点自身的排行榜、分类、最近更新或完本页面。
4. 仅作为最终书源拥有的 bypass 使用外部搜索引擎代理，且仅在插件显式声明并映射该提供方时。

HTTP 200 响应仍然可能是挑战页面。插件在将解析失败视为空结果之前，应检查返回的 HTML 中是否有 Cloudflare/Turnstile/浏览器挑战标记。

第一阶段要求：

- 实现协议和控制台 UI 挂钩。
- 不要求官方书源的付费内容提取正常工作。
- 如果创建了官方书源插件，它可以先支持免费元数据的 search/detail/toc，对锁定章节返回 `AUTH_REQUIRED` 或 `PAID_CONTENT_REQUIRED`。

## 多域名与代理书源规则

插件代表一个站点族，而不一定是一个主机名。当解析规则基本相同且插件可以安全地从一个基础 URL fallback 到另一个时，它可以在一个插件中保留多个镜像或历史域名。

规则：

- 如果单个域名/配置无法到达，插件可以尝试同一生命周期方法的其他声明域名配置。
- Fallback 是站点特定的 URL 构造和解析的本地化操作。全局并发、超时、重试、代理、缓存和健康评分仍是运行时的责任。
- 如果移动端和桌面端域名有 substantially 不同的 DOM/API 结构，优先拆分为独立插件或内部清晰分离的配置。不要在一个难以维护的方法中混合两个无关的解析器形状。
- 如果两个域名共享品牌但验证行为和 DOM 不同，如 `www.69shuba.com` 和 `69shuba.tw`，它们应是独立插件而不是一个多域名插件。
- `metadata.proxy.required: true` 标记通常应使用运行时代理路径的书源。插件仍只调用 `ctx.fetch_*`；不得创建自己的代理客户端。
- `metadata.proxy.mode` 可以是 `auto`、`always` 或 `never`。运行时代码决定如何用配置的代理 URL 来遵守它。

带有 `explore` 的书源的实时验收路径为：

1. `explore_groups`
2. `explore` 挑选一本发现/排行榜书籍
3. `detail`
4. `toc`
5. `chapter`
6. 使用发现的书籍名进行 `search`
7. 从搜索候选重复 `detail -> toc -> chapter`

## 正式第三方插件准入与维护门禁

正式放入 `plugins/sources/thirdparty/` 的插件必须同时通过静态、fixture 和真实站点三类验收。任意一类未执行时只能标记为“未评估”，不能写成“可用”或“通过”。

### 1. 静态契约

- 目录名、`metadata.id`、`Source.id` 完全一致。
- 作者为 `Yunwei`，版本、域名、能力、访问策略、代理、浏览器和限流声明无重复键。
- `source.py` 不得直接使用 `requests/httpx/aiohttp/urllib.request`，不得创建线程、任务、信号量或 `asyncio.gather()`。
- 插件不得实现重试、并发、全局超时、代理切换、缓存或后台任务；同族域名选择和目录/正文分页不属于重试。
- 每一个 capability 都有对应的 async 方法；未声明的实验方法不构成正式能力。

### 2. 搜索与详情

- 使用站点真实存在的书名和作者作为基线，不使用 `book/12345` 或人工拼出的假地址。
- 搜索至少验证精确书名、简繁输入和无结果三种情况；搜索失败不得伪造候选。
- 同名书不以“搜索第一条”作为正确性证据，必须同时核对作者或稳定书籍 ID。
- `detail()` 至少返回 `name`、`author`、`bookUrl`、`tocUrl`；站点公开的封面、简介、分类、状态、字数、更新时间和最新章节不得无故丢失。
- `author` 只返回作者名，不保留“作者：”；`kind` 不塞入作者、字数或更新时间。
- `detail.lastChapter` 必须与完整目录尾项对照。站点自身更新较慢可以记录为“站点滞后”，但解析器漏掉尾章属于失败。

### 3. 完整目录

- 目录必须按阅读顺序返回，URL 非空且唯一，`index` 从 1 连续递增。
- 必须探测静态页之外的 AJAX/API、全部章节、移动端目录、分页和加载更多端点。
- 不得写死 50/100/200 页后静默返回。分页使用“已访问页面去重 + 无下一页/无新增章节终止”，最终安全预算由宿主目录超时负责。
- 不能只用首章和尾章预览块冒充完整目录；目录 fixture 必须保存解析器实际访问的所有分页/AJAX 响应。
- 真实验收记录精确章节数、首章、尾章和校验时间。后续章节数变化不自动代表插件失败，应重新抓取并更新 fixture 基线。

### 4. 正文与编码

- 至少抽查首部、约 50%、尾部各一章；首项可能是公告或短序章时，再抽查一个普通正文章。
- 每章必须校验标题、段落、有效长度、乱码、挑战页、登录页、导航、推荐、上一章/下一章串入和分页完整性。
- HTTP 响应统一由宿主按响应头、HTML meta、UTF-8、GB18030/GBK 顺序解码。插件只有协议明确返回二进制或特殊编码时才调用 `fetch_bytes + ctx.decode_text`。
- `�`、高比例控制字符、明显 mojibake、Cloudflare/Aegis 页面和“内容不存在”不得作为正文返回。
- 正文分页同样以已访问 URL 和同章 URL/ID 为边界，不依赖固定页数；不得把“下一章”拼进当前章。
- 站点水印的确定性清理可以放在插件；跨源比对和全局净化属于宿主。不得用过宽正则删除无法证明是广告的正文句子。

### 5. 繁简、代理与浏览器

- 繁体站搜索前调用 `ctx.to_traditional()`；所有面向用户的返回文本调用 `ctx.to_simplified()`，URL 和 ID 不转换。
- `proxy.mode: always` 只有在直连失败、代理稳定通过的实测证据下使用；`auto` 不等于自动走代理。
- 403、挑战页或 TLS 指纹拦截应先判断 `stealth` 是否足够；只有确需执行 JavaScript/挑战会话时才声明 browser。
- 浏览器是最后一级访问层。插件不得自行启动浏览器、保存挑战 token 或循环刷新 Cookie。
- 搜索提供器只负责发现真实站内 URL，详情、目录和正文仍必须回到目标站点读取；搜索提供器无结果不是伪造结果的理由。

### 6. 并发和稳定性探测

- 对同一本书以并发 1、3、6、12 逐级抽取不少于 20 章，每档至少重复两轮，记录首次成功率、P95 延迟、403/429/超时和内容完整性。Browser 源受宿主浏览器池上限约束，只探测到池上限。
- 正常质量审计必须保留插件声明的 `minIntervalMs`；并发容量另用独立的受控短突发测量，档位间至少冷却 3 秒，并在首次限流或内容异常后停止。报告同时记录生产启动间隔与容量探针启动间隔，明确区分“请求启动频率”和“在途请求数”。
- 单次网络、TLS 或代理失败只记为该环境不可评估，不得直接推断站点并发上限。
- 某级出现限流、空正文、内容异常或重复复测错误率明显上升时，使用上一级稳定值；无法完成并发 1 基线时标记 `not_evaluated`，不得伪造安全并发结论。
- 插件 metadata 只声明结果，运行时调度器负责执行；插件代码不得再套信号量或 sleep。
- 代理和直连分别探测。只在代理改善稳定性且未返回无关页面时声明代理路径。

### 7. Fixture 和真实验收

阶段门禁顺序：

1. `python backend/scripts/validate_source_plugin.py --plugin plugins/sources/thirdparty/<id>`
2. `python -m app.source_plugins.smoke ../plugins/sources/thirdparty/<id>`（从 `backend/` 运行）
3. 使用插件自己的 smoke 关键词执行真实 `search -> detail -> toc -> chapter`。
4. 对搜索受限的站点，使用已确认的真实 `bookUrl/tocUrl` 单独验证读取链路，并把“搜索失败”和“读取失败”分别记录。
5. 记录章节数、正文样本位置、访问层、是否走代理/浏览器、校验时间和未评估项。

Fixture 通过只证明解析器对保存样本仍工作，不证明目标站今天可访问；真实站点通过也不能替代 fixture 回归。两者都必须保留。

### 8. 版本、归档与交付

- 修复解析、字段、编码、分页、域名、代理、浏览器或限流声明后升级插件版本，并更新插件 README 的真实状态。
- 只有 DNS/域名长期失效、目标内容消失或在受控 browser/stealth/代理路径下仍无法稳定读取，且已有日期与错误证据时，才可归档。
- 站点更新慢、暂时 403、单一网络环境失败或旧书库映射残留，不足以判定插件不可用。
- 归档插件从正式目录移除，并在归档清单记录原因、最后可用域名、最后校验时间和替代源；不得只把 `enabled` 改为 false 后继续随镜像发布。
- 修改完成后集中运行全部 26 个正式插件的结构校验、fixture smoke、相关定向测试和 `git diff --check`；正式提交前再运行 `verify.ps1`。

## 兼容性策略

协议是版本化的。

- 第一阶段只接受 `contractVersion: "1.0"`。
- 未来引擎可以添加可选字段。
- 现有字段不得在没有迁移的情况下重命名。
- 新的生命周期方法必须是可选的。
- 添加新能力时，旧插件应继续工作。

聚合 Reading/Legado 书源仍然是一个兼容性外壳。它可以通过宿主受控路由展示启用的第三方插件搜索与直读结果，但不得暴露插件内部 URL、调试信息、Cookie、路径或官方插件能力；所有直读 URL 都必须经过插件启用状态、官方属性、capability 和声明域名校验。

## 依赖策略

这是私有项目。如果插件或前端任务需要依赖，安装并记录它。

规则：

- 后端 Python 依赖放入 `backend/requirements.txt`。
- 前端依赖放入 `frontend/package.json`。
- 优先共享运行时依赖，避免每个插件重复。
- 如果插件需要特殊依赖，在插件目录中添加 `requirements.txt` 并在插件 README 中记录。
- 报告依赖安装失败时附上确切命令和错误。
