# 七猫听书书源（qimao_audio）

基于七猫免费小说 App 的听书 API 逆向实现（签名/登录层与 qimao_novel 相同，
来自 `E:\Reverse Engineering\projects\qimao` 的成果，均已线上实测）。

## 听书的两条路线

1. **真人专辑**：`album/chapter-list` 非空时按专辑章走，
   单集音频 = `album/info?album_id&chapter_id` → `voice_list[0].voice_url`。
2. **AI TTS 云端合成**（大多数书）：专辑章列表为 null，回退到小说章目录
   （`chapter-list`，album_id 与小说书 id 同源），正文哈希 `content_md5`
   编码进章节伪 URL；播放时 `listen/preload-chapter-list` 以随书 AI 音色
   （`album/info` 的 voice_type 5/9，如"多角色对话"）合成，
   返回 `cdn-audio.qimao.com` 直链 mp3（带 expire 签名参数）+ 字幕 txt。

媒体 URL 由平台媒体代理（/api/media/stream）转发播放，插件不处理防盗链。

## 伪 URL 编码

- 书：`https://api-ks.wtzw.com/qm/album/{album_id}`
- 专辑章：`https://api-ks.wtzw.com/qm/album-ep/{album_id}/{chapter_id}`
- TTS 章：`https://api-ks.wtzw.com/qm/tts/{book_id}/{chapter_id}/{content_md5}`

## 注意

- TTS 音频 URL 带 `expire` 时效参数，插件每次实时解析，不做长期缓存。
- 付费/会员音频章节游客身份可能拿不到 URL，此时该集报错。
