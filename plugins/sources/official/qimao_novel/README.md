# 七猫小说书源（qimao_novel）

基于七猫免费小说 App（com.kmxs.reader 8.8，versionCode 80800）的
HTTP API 逆向实现，签名与解密算法来自 `E:\Reverse Engineering\projects\qimao`
的逆向成果（已线上实测验证）。

## 实现要点

- **游客登录**：`POST xiaoshuo.wtzw.com/api/v1/login/tourist`，JWT 约 1 小时有效，
  过期自动重登；设备指纹（uuid/device-id/mac/...）本地生成，无需真机。
- **签名**：GET query 与 9 字段 header 串均为 `md5(排序 k=v 直连 + SECRET)`；
  设备指纹经自定义 base64 字母表置换后放 `qm-params` header。
- **正文解密**：`base64 → 前 16 字节 IV + AES-128-CBC(key)`，PKCS7 去填充；
  `reader_type=3`（epub 书）解密后为标准 epub zip，解包提取 xhtml 去标签。
- **伪 URL**：书/章状态编码在 `api-ks.wtzw.com/qm/book|chapter/...` 路径中。

## 支持范围

- 搜索（tab=3 小说）、详情（reader/detail）、目录（chapter-list）、正文。
- 普通文本书（reader_type=0）与 epub 书（reader_type=3）。
- 付费章节走游客 token 可能返回空内容，此时章节报错（PARSE_EMPTY 语义）。

## 禁止事项

- 不得高频请求（metadata 已设 perHostConcurrency=2 / minIntervalMs=300）。
- 本插件仅用于自托管平台的个人阅读。
