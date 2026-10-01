# 示例有声书源（sample_audio_books）

`content.kind: audio` 媒体书源的参考实现，配套平台的有声书支持一起演进。

## 设计

- **零依赖目录**：书目与集数写在 `source.py` 的内置目录里，站点 JSON API
  （`api.sample-audio-books.test`，RFC 2606 保留测试域名，线上不可解析）
  拉取失败时自动回退，因此本插件在任何环境都能完整跑通
  search → detail → toc → chapter。
- **真实可播媒体**：每集指向 SoundHelix 公开示例 mp3
  （`www.soundhelix.com`），在书库订阅后可直接试听，验证媒体代理、
  Range 续传与播放器链路。
- **媒体章节载荷**：`chapter()` 返回 `format: "audio"`、`content: ""`、
  `mediaUrl`/`mediaType`/`durationSeconds`，契约见
  `docs/architecture/source-plugin-contract.md` 的"媒体章节"一节。

## 用途

1. 离线烟测（`smoke/` fixtures 覆盖站点 API 的四个阶段）。
2. 平台有声书能力的端到端验收：订阅 → 共享书库 → 读者入口播放。
3. 新书源作者的参考模板：把目录拉取换成真实站点解析即可开始适配有声站点。
