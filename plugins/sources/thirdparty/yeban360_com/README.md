# 夜伴书屋 (yeban360_com)

- 站点：`https://www.yeban360.com`
- 类型：简体中文 HTML 站，无登录、无 Cloudflare 挑战
- 访问层：四段全部 `ctx.access.http`

## 链路

| 阶段 | 请求 | 说明 |
|---|---|---|
| 搜索 | `GET /plus/search.php?q=` | 结果是一张表格，只有书名和作者，其余字段由本插件调用自身 `detail()` 补全 |
| 详情 | `GET /book/{book_id}/` | OpenGraph 元数据齐全；`tocUrl` 就是详情页自身 |
| 目录 | 同详情页 `#all-chapter` | 正序完整目录；上方另有倒序「最新章节」预览块 |
| 正文 | `GET /book/{book_id}/{chapter_id}.html` | `#cont-body` 下的 `<p>`；**同章分页**需要合并 |

## 解析注意

- 搜索结果单元格文本是 `《书名》`，取 `a[title]` 才是干净书名。
- 目录必须只解析 `#all-chapter`。页面顶部的「最新章节」块是倒序的，混进来会破坏阅读顺序。
- 正文分页在 `.pagination` 里：`1279289.html` -> `1279289_2.html`。
  页面上那个写着「上一页」的按钮实际指向下一页，标签不可信，
  因此插件按 `.pagination` 里 URL stem 匹配 `{stem}_N.html` 的链接顺序合并。
- 标题里可能带页码标记，返回前用 `_strip_page_marker()` 去掉。

## 实网取证（2026-07-27）

- 搜索：`GET https://www.yeban360.com/plus/search.php?q=凡人修仙传` -> 1 条精确结果
- 详情：`https://www.yeban360.com/book/8189/`，凡人修仙传 / 忘语 / 女生言情 / 完结
- 目录：同上 URL，**2446 章**，首章「第一卷 七玄门风云 第一章 山边小村」
- 正文：`https://www.yeban360.com/book/8189/1279289.html` + `_2.html` 合并后 **2491 字符**

## 已知限制

- 站点把《凡人修仙传》归类为「女生言情」，分类数据本身不准，插件按站点原值返回。
- 目录里存在两条指向不同 URL、标题同为「第一章 山边小村」的记录（1279289 / 1279290），
  这是站点数据本身的重复，按 URL 去重后两条都保留。
- 未验证镜像域，`metadata.baseUrls` 只写实测通过的 `https://www.yeban360.com`。
