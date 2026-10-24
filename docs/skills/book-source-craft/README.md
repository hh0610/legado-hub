# Book Source Craft

这里是 LegadoHub 第三方书源插件的开发入口。权威契约只有一份：

- [书源插件协议](../../architecture/source-plugin-contract.zh-CN.md)

本文只说明执行顺序，不重复定义接口。

## 开发流程

1. 阅读协议，确认宿主与插件边界。
2. 从 [插件模板](references/source-plugin-template.zh-CN.md) 建立 `metadata.yaml`、`source.py`、`README.md` 和 `smoke/`。
3. 用真实书名、作者和真实 URL 验证 `search -> detail -> toc -> chapter`，先确认站点 API/AJAX/分页，再写选择器。
4. 保存完整 fixture；分页目录必须保存所有被访问的页面，并声明精确章节数、首尾章节、URL 唯一和连续 index。
5. 抽查首部、中部、尾部正文，检查乱码、分页、串章、挑战页和水印；繁体站同时验证输入转繁和输出转简。
6. 逐级探测并发和请求间隔，失败时再分别验证 stealth、代理和 browser，最后把实测结果写入 metadata。
7. 阶段结束后集中运行结构校验、fixture smoke 和真实链路；正式提交前运行仓库完整门禁。

## 有声 / 视频书源（媒体源）

有声书与视频短剧源与文字书源走同一套 `search -> detail -> toc -> chapter` 生命周期，差异集中在章节载荷与元数据声明。基准实现：`plugins/sources/thirdparty/qimao_audio`（有声）、`qimao_video`（短剧）、`fanqie_audio`（带签名服务的有声源）。

1. **元数据声明**：`metadata.yaml` 里设 `content.kind`（`audio` 或 `video`）并填写 `content.streamDomains`（媒体文件实际所在的 CDN 域名列表；播放走媒体代理，代理只放行 `domains`/`baseUrls`/`streamDomains` 内的地址）。七猫基准：音频 CDN 在 `qimao.com` 下、短剧 CDN 在 `wtzw.com` 下，与 API 域名同域即可覆盖；番茄听书的音频 CDN 是独立的 `fqnovelvod.com`，必须显式声明。
2. **章节载荷**：`content` 恒为空字符串；`format` 设 `audio`/`video`；可播地址放 `mediaUrl`（直链 mp3/mp4 或 m3u8 播放列表均可，同剧集内可混用）；有声与视频都应返回 `durationSeconds`。防盗链、HLS 重写和过期直链全部由媒体代理处理，插件每次 `chapter()` 重新解析当前可播地址即可，不得缓存直链。
3. **按集组织**：`toc` 返回完整顺序目录，一章即一集；剧集列表从详情/剧集 API 一次性取得后可短期缓存（七猫短剧缓存 `play_list` 按 playlet_id）。
4. **下架条目**：搜索索引可能包含音频/视频版已下架的书。目录 API 报"不存在/已停止合作"类错误时静默返回空目录并 `ctx.trace()` 记录，不得抛未捕获异常。
5. **签名服务**：需要设备签名的源（如番茄的 unidbg 六神签名）把签名服务地址做成 `ui` 声明式设置项（见 `fanqie_audio` 的 `signer_base`），默认值指向部署约定地址，插件内通过 `ctx.settings` 读取。
6. **阅读端行为**：媒体源由宿主聚合进统一书源，阅读端书型（audio/video）自动标记、听书/视频播放器路由和 HLS 兼容全部由生成书源与媒体代理处理，权威规则见[契约的"统一书源在阅读端的媒体规则"](../../architecture/source-plugin-contract.zh-CN.md)。插件作者只需保证 `chapter()` 载荷符合契约。

测试要点：至少验证一本下架音频版的书返回空目录不报错；直链与 m3u8 两种 `mediaUrl` 都取到过；`streamDomains` 覆盖了 `mediaUrl` 的全部主机（否则代理 403）。

## 订阅就绪门禁

插件通过 `search -> detail -> toc -> chapter` 不代表已经满足订阅要求。正式化前还必须把搜索结果送入宿主订阅映射，确认下列字段不会在订阅时丢失：

- 搜索结果必须提供非空的 `sourceId`、`name`、`author`、`bookUrl`、`tocUrl`、`lastChapter` 和 `bookStatus`，并提供大于 `0` 的整数 `chapterCount`。
- 详情结果必须提供 `name`、`author`、`bookUrl`、`tocUrl`、`lastChapter`、`bookStatus` 和大于 `0` 的整数 `chapterCount`。搜索页缺少这些值时，只补全精确书名或高可信候选，避免为全部结果无界请求详情或目录。
- `bookStatus` 使用站点明确给出的连载/完结文本，不得只塞进 `kind`；`chapterCount` 必须与完整目录一致，不能使用当前页条数或分页数。
- `coverUrl`、`intro`、`kind`、`wordCount` 和 `updateTime` 应在站点可提供时填写。站点确实不提供的可选字段可以为空，但必须在 README 或验收报告中说明。
- `sourceName` 由宿主调度器补充，`bookId` 由宿主根据 `sourceId + bookUrl` 生成，插件不得自行伪造。

验收测试必须经过 `LibraryBooksService._payload_from_group()` 或等价的公开订阅入口，并至少断言订阅源保留 `bookId`、`sourceId`、`sourceName`、`bookUrl`、`tocUrl`、`lastChapter`、`bookStatus`，聚合订阅结果的 `bookStatus` 非空且 `totalChaptersAtSubscribe` 等于完整目录章节数。

## 水印采样与净化门禁

新增或重新适配的正式插件，默认从站点排行榜选 3 本不同书籍，每本按首部 5 章、中部 40 章、尾部 5 章采集正文。站点受限时保留实际成功量和阻塞阶段，不为凑够 150 章密集重试，也不用其他站点样本替代。

- 先保存样本和候选证据，再修改净化规则。已有插件规则会影响章节输出时，还要保留规则应用前的 HTML 或解析证据。
- 双源同章差异只用于发现候选，不能单独证明哪一方是水印；必须保护两源共有的作者文本和正文片段。
- 混淆水印按稳定 token 顺序、有限长度变量区间、DOM 位置和跨书证据识别，不要求同一完整字符串重复 5 章。
- 稳定的站点专属整行规则写入该插件的 `adPatterns`；句内注入只有在结构边界明确时才在插件解析阶段精确删除。
- 禁止无限 `.*`、常见正文词和无法给出反例的宽泛规则。变量区间必须有上限；不确定候选只保留诊断，不自动删除。
- 每条批准规则必须同时有命中样本和不应命中的反例测试。未发现水印时记录采样覆盖，不添加空规则或兜底规则。

采样方案、证据结构和当前站点结论见 [站点水印采样方案](../../architecture/site-watermark-sampling-plan.zh-CN.md) 与对应阶段报告。采集器和分析器只生成诊断证据，不自动修改插件。

## 必读参考

- [插件模板](references/source-plugin-template.zh-CN.md)：目录骨架和最小实现。
- [真实站点适配流程](references/plugin-source-workflow.md)：抓取、解析和定位问题的顺序。
- [聚合源模式](references/aggregate-source-pattern.md)：仅用于理解宿主聚合边界，插件不得自行聚合。
- [公开书源参考](references/public-source-references.md)：用于发现站点规律，不作为可用性证据。
- [正式化检查](references/stage-2-plugin-production.md)：历史补充材料；与协议冲突时以协议为准。

## 完成定义

一个插件只有在以下结果都明确时才算完成：

- 静态契约通过。
- 完整 fixture smoke 通过。
- 真实搜索与真实读取链路通过，或分别标记具体未评估/环境受阻项。
- 详情字段、完整目录、三处正文、编码、繁简、代理/browser 和限流均有证据。
- 订阅就绪字段及订阅 payload 回归通过。
- 水印采样结论明确；已批准规则有命中和反例测试，未批准候选未进入运行时。
- 版本与 README 已更新。

站点更新慢不等于插件失败；搜索失败也不等于详情、目录和正文全部失败。报告必须把这些情况分开。
