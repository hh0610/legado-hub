# 七猫短剧书源（qimao_video）

基于七猫免费小说 App 的短剧/漫剧（playlet）API 逆向实现。该链路在
`E:\Reverse Engineering\projects\qimao` 的逆向成果中只有端点与书源 jsLib
实现，**从未被实测过**；本插件于 2026-10-01 首次线上验证通过：
`api-gw.wtzw.com` 接受与 api-bc/api-ks 相同的 sign/header 方案，
`playlet/api/info` 返回全集 `play_list[]`，播放地址为
`cdn-vod-playlet.wtzw.com` 的 m3u8（或 mp4）直链，无需额外签名。

## 链路

- 搜索：`GET api-bc.wtzw.com/search/v1/playlet?wd=&page=`
- 详情/剧集：`GET api-gw.wtzw.com/playlet/api/detail?playlet_id=`（简介/标签/全集列表）
- 取集：`play_list[].video_url`（按 sort 定位），HLS 由平台媒体代理自动重写。

## 伪 URL 编码

- 书：`https://api-gw.wtzw.com/qm/playlet/{playlet_id}`
- 集：`https://api-gw.wtzw.com/qm/ep/{playlet_id}/{sort}`

## 注意

- `play_list` 在插件实例内缓存 10 分钟，避免每集重复拉取全量列表。
- 部分剧集带 `unlock_sorts`（付费解锁）概念，游客身份下未遇到，遇到时会
  表现为对应集无 video_url 而报错。
