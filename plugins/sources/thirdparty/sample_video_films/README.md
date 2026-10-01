# 示例视频源（sample_video_films）

`content.kind: video` 媒体书源的参考实现，配套平台的视频书源支持一起演进。

## 设计

- **零依赖目录**：书目与集数写在 `source.py` 的内置目录里，站点 JSON API
  （`api.sample-video-films.test`，RFC 2606 保留测试域名，线上不可解析）
  拉取失败时自动回退，因此本插件在任何环境都能完整跑通
  search → detail → toc → chapter。
- **真实可播媒体**：使用公开的 Big Buck Bunny 测试流——
  `test-streams.mux.dev` 的 HLS（m3u8，验证媒体代理的播放列表重写与
  hls.js 播放路径）与 `test-videos.co.uk` 的直链 mp4（验证 Range 续传）。
- **媒体章节载荷**：`chapter()` 返回 `format: "video"`、`content: ""`、
  `mediaUrl`/`mediaType`/`durationSeconds`，契约见
  `docs/architecture/source-plugin-contract.md` 的"媒体章节"一节。

## 用途

1. 离线烟测（`smoke/` fixtures 覆盖站点 API 的四个阶段）。
2. 平台视频书源能力的端到端验收：订阅 → 共享书库 → 读者入口播放。
3. 新书源作者的参考模板：把目录拉取换成真实站点解析即可开始适配影视站点。
