# 小说虎

- Plugin ID: `xiaoshuohu_com`
- Domain: `xiaoshuohu.com`
- Base URL: `https://www.xiaoshuohu.com`
- Source seed: so-novel `xiaoshuohu.com`
- Auth: none
- Content: free

现场补充：

- 站内搜索不稳定，先使用 `search_provider`，未命中时再从首页精确匹配当前展示书籍。
- 书籍地址采用实际的 `/<分类ID>/<书籍ID>/` 结构，不使用不存在的 `/book/<id>/`。
- 该源正文广告清洗规则比普通镜像更重，后续 live 审查时应重点看正文污染。
- 搜索提供器遵循宿主代理策略，不再强制绕过已配置代理。

## Fixture Smoke

Fixtures cover `detail`, `toc`, and `chapter` under `smoke/fixtures/`.

```powershell
cd backend
python scripts/validate_source_plugin.py --plugin ../plugins/sources/thirdparty/xiaoshuohu_com
```
