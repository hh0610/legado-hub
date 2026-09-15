# 死源书站处理记录与复活动态监测

记录三方书源插件中"确认死亡并删除"的站点、删除证据，以及定期检索其复活/换域情况的机制。
本文件由维护者与每周巡检自动化共同维护：巡检发现变化时更新「六、巡检记录」。

配套工具：`dev-assets/probes/dead_site_revival_check.py`（巡检探针）、
`dev-assets/probes/audit_thirdparty_live.py`（全链路体检）、
`dev-assets/probes/capture_plugin_fixtures.py`（smoke fixtures 采集）。

## 一、处理惯例（删除门槛）

站点出现以下任一情况，且**三路取证一致**（本机直连、仓库代理、浏览器桥）后，按仓库惯例直接删除插件目录（含 smoke 资产）：

| 分类 | 判定标准 |
|---|---|
| `dns_dead` | 域名 NXDOMAIN / getaddrinfo failed，直连与代理出口均不解析 |
| `unreachable` | 直连与代理均连接超时/拒绝（多日多时段复测） |
| `cf_hard_block` | Cloudflare "Attention Required" 页，真实浏览器渲染后仍被拦（IP/指纹级封锁，非 JS 挑战） |
| `cf_challenge_unsolvable` | Cloudflare/Turnstile 挑战在受控 Chromium 中无法通过（连续多轮） |
| `waf_interactive_captcha` | 站点自研 WAF 要求交互式验证码，无法自动通过 |
| `site_moved_incompatible` | 站点 301 迁移后新站关闭核心入口（搜索/目录）或改版至解析器完全不兼容 |
| `mirror_dead_primary_alive` | 同站镜像域死亡而主域插件存活（无独立价值） |

删除前必须留存：死亡分类、取证日期、关键证据（HTTP 码/页面标题/DNS 结果）、
以及该站家族的域名轮换线索（写入下表），供日后复活监测。

不满足删除门槛的（如仅搜索后端暂时故障、单点超时），修复或保留并在 README 标注。

## 二、2026-09-15 删除清单

| 插件 ID | 站点名 | 原域名 | 死因分类 | 证据摘要 |
|---|---|---|---|---|
| `czbooks_net` | 小说狂人 | czbooks.net | `cf_hard_block` | 直连 curl 与浏览器桥均返回 "Attention Required!"（2026-09-15）；07-31 曾实网通过 |
| `uuread_tw` | UU阅读 | uuread.tw | `cf_hard_block` | 同上，首页即硬阻断（2026-09-15）；07-28 曾通过 |
| `twkan_com` | 台灣小說網 | twkan.com | `cf_challenge_unsolvable` | 浏览器桥渲染后 challenge 仍在；07-28 复测同样失败，连续多轮 |
| `xiaoshuohu_com` | 小说虎 | xiaoshuohu.com | `waf_interactive_captcha` | 全站 307 → `/WAF/VERIFY/CAPTCHA`，浏览器 12s 长等待仍停在 "Verify Yourself" |
| `yeban360_com` | 夜伴书屋 | yeban360.com | `site_moved_incompatible` | 301 → yybsw.com；新站 `search.asp` 返回"搜索君跑路咯"（搜索关闭），`/book/*` 页面 403 |
| `zhswx_tw` | 宙斯小说网 | tw.zhswx.com | `unreachable` | 直连与代理均 connection refused/超时 |
| `shumilou_co` | 书迷楼 | shumilou.co | `mirror_dead_primary_alive` | 403 空壳 JS 跳转页；同站 `shumilou_top` 插件实网通过 |
| `kks101_com` | 101看书网 | 101kks.com | `dns_dead` | `www.kks101.com` NXDOMAIN（07-28 已 empty_search，后彻底死亡） |

## 三、复活巡检机制

- **脚本**：`.venv/Scripts/python.exe dev-assets/probes/dead_site_revival_check.py`（仓库根目录执行）
- **判定**：对每个站点做两层检查——
  1. 域名直探：候选域名（原域名 + 常见 TLD 变体 + 家族已知新域）逐一 GET，分类
     `alive`（页面含站点签名）/ `cf_block` / `captcha` / `moved` / `dead`；
  2. 搜索引擎检索：用站点名 +「最新域名 / 小说」等关键词经 DDGS 检索，
     从结果中提取陌生域名并做签名验证。
- **代理**：自动读取 `backend/config/app_config.json` 的 `proxy.url`（与宿主同策略）。
- **报告**：`artifacts/dead-site-watch/<日期>.json`（全量）与 `latest.md`（摘要）。
- **计划任务**：每周一 09:00 自动运行（ZCode 自动化「每周定期搜索引擎搜索死源书站复活/换域名」）。
- **发现复活后的动作**：巡检只报告与更新本文件，不自动重建插件；重接需人工取证
  （按 `docs/architecture/source-plugin-contract.zh-CN.md` 完成适配 + smoke 资产 + 全链路验证）。

## 四、各站观察要点

| 站点 | 域名轨迹与线索 | 搜索关键词 |
|---|---|---|
| 小说狂人 | czbooks.net（CF 阻断）；繁中站，注意 czbooks.com/.tw 变体 | 小說狂人 czbooks 最新域名 |
| UU阅读 | uuread.tw（CF 阻断）；繁中站 | UU閱讀 uuread 最新网址 |
| 台灣小說網 | twkan.com（CF 挑战） | 台灣小說網 twkan |
| 小说虎 | xiaoshuohu.com（自研 WAF）；WAF 通过即可能整体复活 | 小说虎 xiaoshuohu |
| 夜伴书屋 | yeban360.com → yybsw.com（壳化）；留意 yybsw2/yybsw3 等家族轮换 | 夜伴书屋 yybsw 最新域名 |
| 宙斯小说网 | tw.zhswx.com / zhswx.tw（不可达） | 宙斯小说网 zhswx |
| 书迷楼 | shumilou.co 死，主域 shumilou.top 由 `shumilou_top` 插件覆盖；.co 复活意义低 | （随主域覆盖，低优先） |
| 101看书网 | 101kks.com / kks101.com 均死 | 101看书网 kks101 |

## 五、已复活/重接站点登记

（空。巡检发现站点**稳定**复活后，先移入「复活观察」并人工取证；确认可重接时在此登记：
站点、新域名、重接插件 ID、验证日期。）

## 六、巡检记录

- **2026-09-15**：初建。8 站删除当日取证：czbooks/uuread CF 硬阻断、twkan CF 挑战、
  xiaoshuohu WAF 验证码、yeban360→yybsw 壳化、zhswx 不可达、shumilou_co 空壳、kks101 DNS 注销。
  首轮巡检补充观察：
  - 走代理出口时 czbooks/uuread/kks101(101kks.com) 呈现 "Just a moment..."（CF JS 挑战），
    与直连的 "Attention Required" 硬阻断不同——若未来浏览器桥+代理能过挑战，存在重接空间。
  - `zhswx_tw` 与 `shumilou_co` 经代理**间歇性**返回真实站点（宙斯小说 56KB 真页 /
    书迷楼真首页），但同日多次复测又在真实站、验证码页、连接失败之间抖动
    （代理出口轮换所致）。二者列为「复活观察」对象，待出现稳定可达窗口再评估重接。
