# 猫眼看书（lfdapengu_com）

猫眼看书安卓 App 的纯 JSON API 源，支持搜索、详情、完整目录和免费正文。全书章节均为免费可读（实测《斗破苍穹》1900+ 章 `isFree=1`、`canRead=1`），无需登录和代理。

## 接口

- 搜索：`GET /search?keyword={key}&page={page}`，列表 `$.data[*]`
- 详情：`GET /novel/{novelId}?isSearch=1`，数据根 `$.data`
- 目录：`GET /novel/{novelId}/chapters`，列表 `$.data.list[*]`
- 正文：目录项 `path` 解密后得到的绝对 URL，返回 `{"content": "..."}`

业务 API 主机会轮换，书源注释中记录的备用主机为 `api.myweipin.com`、`api.jmlldsc.com`、`api.lemiyigou.com`；插件对传入的详情/目录 URL 按原主机访问，仅搜索使用 `base_url`。章节正文托管在独立 CDN 主机 `api.jxgtzxc.com`（与详情中的 `downloadUrls`、JWT 签发方一致）。

## 加密

目录项 `path` 为 Base64 密文，算法 `AES/CBC/PKCS5Padding`，key `f041c49714d39908`，iv `0123456789abcdef`，解密后是正文 JSON 的完整 URL（如 `http://api.jxgtzxc.com/0/152/221121.json`）。加密库（pycryptodome）在解密路径内延迟导入，单条解密失败记为 trace 并跳过，整体失败时抛出结构化 `ParseError`。

## 请求头与令牌

接口要求携带 App 级请求头（`client-*` 与 `Authorization`）。该 JWT 是阅读书源公开携带的 App 匿名令牌（exp 2028，非用户账号凭证），令牌失效后需要从新版书源更新。

## 正文净化

去除行首缩进与控制字符，按空行恢复段落，并执行书源正则 `一秒记住.*精彩阅读。|7017k`。正文 JSON 不含标题，章节标题由目录侧补齐。

## 验证记录

2026-09-11 以《斗破苍穹》（novelId `zbqYrb`）实网复核：搜索、详情、目录、首章正文四阶段均 200；首章 path 解密为 `http://api.jxgtzxc.com/0/152/221121.json`，正文约 8.5KB。

```powershell
cd backend
python scripts/validate_source_plugin.py --plugin ../plugins/sources/thirdparty/lfdapengu_com
```
