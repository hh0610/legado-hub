# 番茄小说书源（fanqie_novel）

LegadoHub 书源插件，协议版本 `contractVersion: 1.0`。能力：`search` / `detail` / `toc` / `chapter` / `chapter_reviews`。

> **零 legado-hub 改动**：本插件是纯插件（`source.py` + `metadata.yaml` + `smoke/`），不修改 legado-hub 任何源码，也不要求宿主安装额外依赖——可直接跑在原版 legado-hub 上。

## 全离线架构（不依赖真机 / Frida / Oracle）

1. **签名**：所有请求先找离线六神签名器取 6 个签名头（X-Argus / X-Gorgon / X-Helios / X-Khronos / X-Ladon / X-Medusa），再本地直发番茄官方 API。
   - 签名器地址：环境变量 `FANQIE_SIGNER_BASE`，默认 `http://192.168.31.8:8787`。
   - 签名仅绑定 URL（GET/POST 通用），不绑定 body。
2. **解密**：章节正文（`reader/full` 返回的 AES 密文）由插件内联算法本地解密，不调用任何外部进程。
   - 算法：`body_key = AES-128-ECB(K2, K[16:32]) XOR K[0:16]`，再 `AES-128-CBC(body_key, ct[16:], iv=ct[:16])` → 去 PKCS7 → gunzip → XHTML。
3. **AES 无外部依赖**：优先用宿主已装的 `pycryptodome`（更快）；若未安装，自动回退到插件内联的纯 Python AES-128 实现（已与 pycryptodome 对拍验证一致）。**不装任何包也能跑。**
4. **解密密钥（kmskey）按 `key_version` 缓存**：
   - `reader/full` 响应只返回密文 + `key_version`（用哪版 key 的索引），真正的 kmskey 不在响应里。
   - 启动时从环境变量注入并缓存（`_KMSKEY_STORE: {key_version: kmskey}`），同版本长期有效、跨书通用。
   - 缓存未命中或遇到新 `key_version` 时，才回退到 `registerkey` 拉取并写回缓存。

## 章评 / 段评（chapter_reviews 能力）

`chapter_reviews(chapter_url)` 聚合返回：章评 `chapterEnd`、段评气泡 `paragraphs`、热段评论 `hotParagraphReviews` 等；`paragraph_say(chapter_url, paragraph_id)` 拉取某段的段评说。全部走同一套离线六神签名 + 本地直发，实测章评/段评/段评说均正常出数。

## 运行前必须配置的环境变量

- `FANQIE_SIGNER_BASE`：六神签名器地址（默认 `http://192.168.31.8:8787`）。
- `FANQIE_KMSKEY`：当前 `key_version` 对应的 kmskey（64 位 base64）。
- `FANQIE_KMSKEY_VERSION`：上述 kmskey 对应的 `key_version`（如 `1557978139`）。
- （可选）`FANQIE_KMSKEY_MAP`：JSON 多版本映射 `{version: key}`，优先级最高。

## 设备参数

内置设备身份 2609（`iid=2609602404592746` / `device_id=2609602404588650` / `version_code=73733`），与签名器内置身份一致。

## Smoke

`smoke/` 为插件自带回归：`smoke/smoke.yaml` + `smoke_test.py` + `smoke/fixtures/dz1_pairs_s1.jsonl`、`dz1_pairs_s2.jsonl`（解密黄金对）。