# 小说狂人 (czbooks_net)

- 站点：`https://czbooks.net`（繁体中文，台湾站）
- 类型：Cloudflare 前置，无登录
- 访问层：四段全部 `ctx.access.stealth`（`impersonate=chrome120`）；
  普通 `ctx.access.http` 会拿到 Cloudflare 挑战页

## 链路

| 阶段 | 请求 | 说明 |
|---|---|---|
| 搜索 | `GET /s/{关键词}?q={关键词}` | 关键词**同时出现在路径和查询参数**里，见下 |
| 详情 | `GET /n/{book_id}` | 书名、作者、简介、分类、连载状态、更新时间 |
| 目录 | 同详情页 `#chapter-list` | 单页完整目录，无分页 |
| 正文 | `GET /n/{book_id}/{chapter_id}` | `.chapter-detail .content`，`<br />` 分段 |

## 解析注意（三个坑）

1. **搜索 URL 形态**。站点没有搜索表单，入口由 `/js/massets.js` 里的函数拼：
   ```js
   searchUrl = "//" + window.searchUrl + "/" + encodeURIComponent(e) + "?q=" + encodeURIComponent(e)
   ```
   其中 `window.searchUrl = "//czbooks.net/s"`。也就是
   `https://czbooks.net/s/<关键词>?q=<关键词>`。
   只用 `/s?q=` 会返回 **404**（上一轮就是卡在这里判定搜索不可用）。
2. **必须用 lxml 解析**。正文是一长串 `<br />` 分隔的文本，Python 的 `html.parser`
   在这个页面上会把 `<br />` **嵌套**起来——第一个 `<br>` 之后的所有内容都变成它的子节点，
   于是 `br.replace_with("\n")` 会把整章压成一段（实测只剩 112 字符）。
   换 `BeautifulSoup(html, "lxml")` 后同一页正常解析出 2491 字符。
3. **繁简双向转换**。站点是繁体：搜索关键词先 `ctx.to_traditional()`，
   所有面向用户的字段再 `ctx.to_simplified()`。

其他细节：书链接是协议相对的 `//czbooks.net/n/xxx`；详情页标题是 `《书名》`，
需要去掉书名号；正文首行会重复章节标题，尾部有 `(本章完)`。

## 实网取证（2026-07-27，2026-07-31 复核）

- 搜索：`GET https://czbooks.net/s/凡人修仙傳?q=凡人修仙傳` -> 40 本命中，精确匹配 3 条
- 详情：`https://czbooks.net/n/u81a`，凡人修仙传 / 忘语 / 已完结
- 目录：同上 URL，**2475 章**，首章「第1章 山边小村」
- 正文：`https://czbooks.net/n/u81a/u98f5`，**2491 字符**
- 2026-07-31 完整链路重新通过；首章、中段第 1238 项、尾章正文分别为
  **2491 / 3180 / 320 字符**，均超过 200 个中文字符
- `0.1.1` 补齐订阅消费的 `tocUrl`、`bookStatus`、`chapterCount`；正式调度器
  实网选择的高分版本生成“已完结 / 2562 章”订阅快照

## 已知限制

- 搜索结果里的封面多为站点占位图 `default_no_thumbnail.jpg`。
- 目录里存在站点自身标记的「錯誤章」条目（如该书的 chapterNumber 0-3），
  按 URL 去重后保留，属于站点数据本身。
- 详情页不暴露字数，`wordCount` 返回空；最新章节和章节数直接从页面内的完整目录取得。
- `proxy.mode: auto` + `required: false`：实测直连 stealth 即可，
  经代理同样可用（两条路径都验证过）。
