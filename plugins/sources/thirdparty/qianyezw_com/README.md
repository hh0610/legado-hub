# 新御书屋

- 插件 ID：`qianyezw_com`
- 站点：`https://www.qianyew.com/`（原 `qianyezw.com`，301 迁移）
- 取页方式：站点前置浏览器指纹挑战（`client_fingerprint.js`），全部页面经 `ctx.access.browser` 渲染。
- 搜索：`POST /search/`（`searchkey` + `action=login`）
- 详情与完整目录：`/book/{id}/`（`#list-chapterAll` 为完整目录）
- 正文：`/read/{book_id}/{chapter_id}.html`，自动合并同章分页。
- smoke 固定样本：凡人修仙传（book 17948），目录首条可能是上架公告，章节采样用 `sampleIndex`。
