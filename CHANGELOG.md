# 更新日志（CHANGELOG）

> **当前版本 v3.1.7（PCMonitor Pro）**。下方为完整历史记录。
> 注意：游戏帧率采集（PresentMon）已在 **v3.1.4 彻底移除**，v3.1.0~v3.1.3 中涉及帧率的条目均为历史存档，不代表当前功能。
> v3.1.7 重写了采集调度（分层采集 + 异步写库 + 独立进程重采集），刷新间隔现在精确生效（实时档 0.5s）。

## v3.1.0 — 2026-11-26（正式发布）

汇集修复（帧率 + 驾驶舱），统一升 3.1.0：

- **帧率自愈**：`_is_zombie_proc()` 检测 PresentMon 活着但 CPU 不增/无 CSV（孤儿 ETW 会话卡死），连续 3 次自动 kill + 清全部会话 + 重建。启动时与每次拉起前都清理全部 PCMon/PresentMon 会话。
- **音乐卡片根因**：实测用户 Python(`AppData\Local\Python`) 没装 winrt 包，导致 WinRT 子进程 import 失败、playing=null、卡片不显示歌名。已给该 Python 装 winrt 系列，并写入 requirements.txt（start.bat 自动装）。WinRT 实测拿到真实标题/艺人/封面。
- **截图按键**：`_screenshot()` 改 Pillow ImageGrab 优先、GDI 兜底，存图片目录并自动打开文件夹；pillow 加入 requirements。
- **稳定启动**：新增 `start_hidden.vbs`（隐藏脱离控制台启动 agent，关窗口不停）+ `启动服务_稳定版.bat`（提权清理+后台启动+验证）。
- 版本 3.1.0（agent / 前端 / build.gradle versionCode 36 / build_apk.sh）。

## v3.1.0 补记 · 帧率诊断端点

**帧率不显示的真正根因找到并修复（实测 evidence 驱动，非猜测）**

- **根因**：孤儿 ETW 会话。PresentMon 被 kill（agent 重启/探针超时）后，它建立的 ETW 会话不会自动释放——进程没了但会话永久"正在运行"。实测同时残留 3 个孤儿会话（PCMonitorProbeV1 / PCMonProbe_v1 / PCMonitorProbeV2）且无任何 PresentMon 进程。后续任何 PresentMon 启动都卡死：CPU 4 秒增量 0.000s、stderr 只有 2 字节 BOM、CSV 不生成。
- **排除项**：A/B 探针证明 `--v1_metrics` 不是原因（带/不带都超时 CSV=0）；非游戏识别问题（SpeedBall.exe 已识别）；非 agent 权限问题（admin=true）。stderr 乱码是 UTF-16LE(PresentMon) 与 UTF-8(agent 标记) 混写同一文件所致。
- **修复**：`_cleanup_stale_session()` 从只 stop PCMonitorFPS 改为遍历 `logman query -ets` 所有含 PCMon/PresentMon 的会话并全部 stop，启动时和每次拉起 PresentMon 前都执行。
- 新增 `fps_full_reset.bat`：提权后 kill 全部 agent/PresentMon → 管理员 logman stop 清全部孤儿会话 → 启动新 agent → 等待验证 fps。一次操作完成彻底重置。
- 版本 3.1.0。前端/其他功能未动。

## v3.1.0 补记 · 帧率诊断端点

帧率根因定位工具（agent 端，前端不动）。

- **实测状态**：服务正常(version 3.0.8)、`admin=true`、`presentmon=true`、游戏 `SpeedBall.exe` 被识别，但 `fps.reason=no_frames`、CSV 始终不生成、无任何 CSV 产物。
- **决定性发现**：PresentMon 进程存活但 **CPU 4 秒增量为 0.000s**（空转/卡死），ETW 会话 PCMonitorFPS 显示"正在运行"，线程 5 个、Responding=True，但完全不采集。进程约每几分钟被 agent 重启（PID 28724→34716→40924）。
- **新增诊断端点** `GET /api/fps_probe?v1=1|0&sec=5`：以 agent 管理员权限独立跑一次 PresentMon（带/不带 `--v1_metrics`），返回 CSV 是否生成、大小、表头、stderr，用于 A/B 定位空转根因。
- **新增脚本**：`tools/PresentMon/probe_ab.py` + `run_probe.bat`（独立提权 A/B 探针）、`restart_and_probe.bat`（重启 agent + 自动跑两种探针并存结果）。
- 版本 3.0.9；本版只加诊断，不在没有证据前盲改 `--v1_metrics` 等参数。

## v3.1.0 补记 · 帧率健壮性

帧率不显示问题排查与健壮性修复（只改 agent.py，前端/其他功能不动）。

**根因（实测确认）**：
- 用户关闭 agent 窗口后，PresentMon 子进程没跟随退出，成为孤儿进程（实测 PID 31556）霸占 `PCMonitorFPS` ETW 会话；下次启动的新 PresentMon 因会话被占无法抓帧。
- 非提权运行 PresentMon 时，只能列出 `<unknown>` 进程（stderr 实测：`requires elevated privilege in order to query processes`），进程名识别层拿不到游戏。
- `warming` 状态无超时：识别到游戏但 CSV 无帧时永远卡在 warming（幽灵状态），前端干等。
- `_gpu_process_pids()` 的 `universal_newlines=True` 在 Python 3.13 以 UTF-8 解码 nvidia-smi 的 GBK 输出，刷了上万条 `UnicodeDecodeError` 日志。

**修复**：
- 启动时清理孤儿 PresentMon + 残留 ETW 会话（`_cleanup_stale_session` 提前到 main 启动流程）。
- `warming` 加 8 秒超时，超时后按 CSV 是否存在降级为 `no_frames`/`no_game`，不再卡幽灵状态。
- `/api/status.fps` 新增 `diag` 人类可读诊断字段，可直接看出"未捕获帧/无游戏/非管理员"等。
- `_gpu_process_pids()` 改用 `_run_cmd` 多编码回退，消除 UnicodeDecodeError 刷屏。
- 新增 `tools/PresentMon/diag_fps.bat`：提权运行的独立采集诊断脚本，采集 8 秒并报告 CSV 结果。

## v3.0.7 — 2026-11-26

只修三件事，其余（方块键交互、天气、温度六格/电池/网络/进程卡、4 主 tab、自动横屏、帧率单实例）一律不动。

- **真实在播采集**：安装 winsdk（WinRT 投影），`_playing_winrt()` 用 `GlobalSystemMediaTransportControlsSessionManager` 取 title/artist/album/thumbnail(封面 base64)/播放状态/position/duration，填入 `/api/status.playing`；封面限 3MB、在播 2 秒刷新使进度实时走动；失败回退原 PowerShell 路径（只有标题/艺人/状态）。无播放才返回 None → 前端"暂无播放"。
- **NOW PLAYING 卡片**：前端用真实值渲染歌名/艺人/封面方图/进度条/播控图标；进度条按 position/duration 计算、CSS 1.9s 线性过渡平滑；封面 data URL 仅在变化时更新避免重绘。
- **返回键**：从右下角移到驾驶舱底部中央、页码点右侧空白区，小胶囊 52×28px、字号 11px、圆角、半透明底；absolute 限定容器内，一点即回主界面，右侧不再放任何元素（躲开系统电量/信号）。
- 版本全量同步 3.0.7（versionCode 35）。服务由用户桌面双击 start.bat + UAC 手动启动。

## v3.0.6 — 2026-11-26

只做一件事：让驾驶舱右侧功能区（顶部4状态块 + 12方块键）真正可交互、点击电脑端真实执行。其余一律不动。

- **方块键 → 字符串动作**：12 键点击 POST `/api/action {action:"prev"/"play_pause"/.../"lock"}`；按压 `:active/.press` 缩放 0.96 + 高亮。
- **agent 动作执行**：`run_action` 支持字符串动作名（兼容数字 1-12）。prev/play_pause/next/mute 用 VK 媒体键；terminal→wt/cmd、vscode→Code、browser→默认浏览器、files→explorer、taskmgr；截图 Pillow/GDI 存 Pictures 并返回路径；sleep→SetSuspendState（失败回退息屏）、lock→LockWorkStation。每动作 try/except，成功 `{ok:true,detail}`、失败 `{ok:false,reason}`。
- **状态回显**：静音键静音时高亮"已静音"、播放键反映播放/暂停；顶部音量/电源计划/亮度/时长每 2 秒从 `/api/status` 刷新。
- **兜底**：动作失败该键短暂闪红 + 副信息"不可用"、顶部红条提示，不弹窗不崩溃；截图/睡眠/锁屏成功轻提示。
- 版本全量同步 3.0.6（agent / 前端 / build.gradle versionCode 34 / build_apk.sh / CHANGELOG）。

## v3.0.4 — 2026-11-26

只修三个尾巴，其余（帧率单实例、温度六格/电池/网络/进程卡、4 个主 tab 的竖屏布局与功能）一律不动。用户已授权安装任意插件，故本版解除"不能新增插件"限制。

- **自动横屏**：安装并注册 `@capacitor/screen-orientation@8.0.2`（capacitor.plugins.json / capacitor.settings.gradle 自动注册，Capacitor 8 无需改 MainActivity）；`AndroidManifest` 的 MainActivity 加 `android:screenOrientation="portrait"` 作为默认竖屏。进入驾驶舱调 `ScreenOrientation.lock({orientation:'landscape'})` 自动横屏、退出调 `unlock()`+`lock(portrait)` 恢复竖屏；Web 预览端无此能力时用 CSS `.pb-web-landscape` 旋转兜底，不影响 APK。
- **天气 + 正在播放**：驾驶舱仪表盘顶部补回两项。天气优先读 `/api/status.weather`（agent 的 wttr.in），缺失时前端 Open-Meteo（ip-api.com 粗定位）兜底；取不到则整块隐藏不留 `--`。正在播放卡片始终显示，有播放渲染 agent 的标题/艺人/播放状态，无播放显示"暂无播放"占位。
- **安全区适配**：驾驶舱 `.page` 用 `env(safe-area-inset-top/right/bottom/left)` padding，顶部内容下移避开系统状态栏；返回键内收并叠加安全区、放在页码点右侧空白处，避免被电量/信号图标或页码点遮挡，保证可点。
- 版本全量同步到 3.0.4（versionCode 33）。

## v3.0.3 — 2026-11-26

只修一个严重回归：**横屏被做成了全局**。v3.0.2 替换驾驶舱样式时丢失了覆盖层的 `position:fixed / display:none` 基础定位，导致横屏两屏布局直接暴露在主界面文档流里，把 性能/存储/状态/设置 4 页全撑成横屏、布局压坏、返回键遮挡。

- **撤销全局横屏**：`#page-cockpit` 恢复为默认隐藏的全屏覆盖层（`position:fixed;inset:0;z-index:100;display:none`，仅 `.show` 显示）；主界面恢复改动前的竖屏布局（顶部栏 → 竖排小卡 → 大卡 → 底部 5 tab）。没有任何挂到 body/全局的 rotate 或 orientation 锁。
- **横屏只作用于驾驶舱页**：进入底部第 5 tab「驾驶舱」才显示覆盖层并启用横屏两屏布局；竖屏提示（`#rot`）改为 `#page-cockpit.show #rot` 限定，只在驾驶舱页出现；退出时 `close()` 清除 `show`/`pb-open`/`show-deck` 状态，立即恢复竖屏主界面。
- **返回键**：改为容器内 `position:absolute` 右下角（不再 fixed 浮出遮挡其他页），加大到约 67×38px 保证可点，避开顶部状态栏与底部页码点；点击回此前 tab（默认性能页）。
- 温度六格/电池/网络/进程卡、状态页、性能页、存储页、设置页的竖屏布局与功能全部还原到改动前；帧率单实例逻辑不动。

### 版本同步
`APP_VERSION` / `UI build` / 设置页版本 / `/api/status.version` / `build.gradle`(versionCode 32) / `build_apk.sh` / CHANGELOG → 3.0.3。

## v3.0.2 — 2026-11-26

严格只修两个问题，其余功能（性能/存储/状态/设置页、温度六格/电池/网络/进程卡、权限与游戏识别逻辑）一律不动，不回退此前修复。

### A. 帧率不显示（PresentMon error 4201，日志实锤）
根因：agent 定时重复拉起 PresentMon，停旧启新有竞态，ETW 会话未释放干净就重启，撞 error 4201 + "a trace session named 'PCMonitorFPS' is already running"，本轮进程没起来、CSV 无数据、帧率卡永远 waiting/N/A。
- **单实例常驻**：帧率开关打开后 PresentMon 只拉起一个实例并长期运行，全程追加写同一个 `tools/PresentMon/_fps_live.csv`；不再因"无游戏空闲 15 秒"kill，也不再定时重启。无游戏只返回 `no_game` N/A，游戏一开自动出数 `reason=ok`。
- **启动前清理**：统一由 `_cleanup_stale_session()` 执行——kill 本 agent 残留的 PresentMon 进程（按命令行 `_fps_live.csv`/`PCMonitorFPS` 精确识别，不误杀用户手动实例）+ `logman stop PCMonitorFPS -ets`（忽略报错）；固定 `--session_name PCMonitorFPS`，去掉会触发 warning/4201 的 `--stop_existing_session`；关闭开关时也释放会话。
- **增量 tail**：CSV 从当前末尾开始读，避免历史帧混入近 2 秒滑动窗口；平均 FPS / 1% Low / 帧时间逻辑不变。
- **日志编码**：`PresentMon-stderr.log` 统一 `utf-8-sig`（UTF-8 带 BOM）写入，修掉 Windows 记事本乱码。
- 环境/权限/游戏/进程识别逻辑均未改动。

### B. 驾驶舱：入口 + 横竖屏 + 返回键
- **入口移到主导航**：底部 tabbar 增加第 5 个 tab「驾驶舱」，与 性能/存储/状态/设置 平级；设置页原入口保留为跳转到该 tab。
- **严格还原用户 HTML 布局**：驾驶舱改为用户提供的横屏两屏设计——`#stage` 横向滑动切换，第 1 屏仪表盘（大时钟 + NOW PLAYING + CPU/GPU/RAM/NETWORK 四格指标，含每核竖条 / 显存折线 / 内存与交换区进度 / 网络吞吐折线），第 2 屏 4×3 共 12 个方块键 + 顶部 4 格状态条；底部页码点切换。样式全部加 `#page-cockpit` 作用域，不污染其他 tab。
- **横竖屏**：横屏为正常形态；竖屏整屏只显示「请将手机横屏使用」提示，不硬塞竖排。
- **返回键**：从右上角移到右下角，加大到 ~67×38px 保证可点，避开顶部系统状态栏与底部页码点；点击正常退出驾驶舱并回到此前 tab。
- 数据仍取真实 `/api/status`（未连接显示全部占位，不显示假数据）；12 键动作映射到电脑端编号 1-12。

### 版本同步
- `APP_VERSION` / `UI build` / 设置页版本 / `/api/status.version` / 服务端标识 → 3.0.2。

## v3.0.0 — 2026-11-26

大版本更新，两件事一次完成：**新增驾驶舱页** + **帧率改为动态自动检测**。
严格隔离：性能页、存储页、状态页（含温度六格/电池/网络/进程/帧率卡）、设置页其余功能均未改动，未回退此前修复。

### A. 新增第 5 页：驾驶舱（横屏）
- 入口在设置页二级菜单「驾驶舱 ›」；原 4 个 tab 的顺序、样式、逻辑完全不动。
- 独立全屏页 `#page-cockpit`；样式 `--pb-` 前缀自成一套，脚本以 IIFE 包裹，不污染全局；竖屏显示「请将手机横屏使用」提示（可强制继续）。
- 两屏切换（底部页码点）：
  - **仪表盘屏**：大时钟 + 日期 + 天气、正在播放卡片（WinRT 取不到则隐藏）、CPU / GPU / RAM / NETWORK 四格指标。四格统一结构：标签 → 大数字 → 温度行 → 4 行副信息 → 底部图形区。
    - CPU：8 根（按实际核心数）每核占用竖条 + 占用进度条。
    - GPU：吞吐折线 + 显存占用进度条。
    - RAM：内存占用条 + 交换区占用条。
    - NET：实时吞吐折线（带面积填充，上/行双线）。
  - **控制台屏**：顶部快捷状态条（音量% / 电源计划 / 亮度% / 运行时长）+ 12 方块键（上一曲、播放暂停、下一曲、静音、Terminal、VS Code、浏览器、文件、任务管理器、截图、息屏、锁屏），每键带状态副信息与运行指示灯。
- 新增 `POST /api/action`：action 编号 1–12，在电脑端执行媒体键、启动程序、截图（保存到 Pictures）、关闭显示器、锁屏；失败只影响本次点击，返回 `{ok, action, detail}`。
- 新增 `GET /api/status`：在现有快照基础上扩展返回 `cpu.perCore`、`gpu.memUsed/memTotal/freq/hot/fan`、`mem.swapUsed/swapTotal/speed`、`net.up/down/ping/rxTotal/txTotal`、`audio.volume/muted`、`display.brightness`、`power.plan`、`uptime`、`playing`、`weather`，并带 `version=3.0.0`。取不到一律 `null`，前端占位不报错。`/api/snapshot` 与 SSE 数据保持不变。

### B. 帧率改动态自动检测（去掉锁定依赖）
- agent 常驻拉起全局 PresentMon：`PresentMon.exe --output_file "tools/PresentMon/_fps_live.csv" --stop_existing_session --session_name PCMonitorFPS --no_console_stats --v1_metrics`，不带 `--process_id`；每次启动前删除旧 CSV。
- 后台增量 tail CSV，按表头列名定位 `MsBetweenPresents` + `Application/ProcessName` + `ProcessID`；近 2 秒 / 最多 120 帧滑动窗口计算平均 FPS、1% Low、帧时间。
- 自动识别前台游戏：窗口/前台/全屏作为强信号，GPU 占用只做加权；排除桌面、浏览器、启动器、反作弊与覆盖层（含 ROB RUN.exe、explorer、steam、chrome）。识别到即显示游戏名 + FPS，reason=ok；游戏关闭回 N/A、reason=no_game，全程零手动。
- 手动点选仅作兜底（`/api/processes` 点选 PID 覆盖），可一键恢复自动。
- reason 细分 `disabled / no_game / waiting_game / no_permission / presentmon_missing / no_process / start_failed / warming / ok`，前端映射中文。
- 无游戏 15 秒空闲时暂停全局抓取省资源，检测新游戏后自动恢复。

### 版本同步
- APP 顶部 `UI build 3.0.0`、设置页版本、`/api/status.version`、服务端标识、构建脚本与 `build.gradle`（versionCode 29 / versionName 3.0.0）全部同步。
- APK：`电脑监测_v3.0.0.apk`。

### 升级注意
- 必须双击 `pc-monitor/start.bat` 重启电脑端 agent，否则后台仍在跑旧代码，新接口 `/api/status`、`/api/action` 不存在。
- 驾驶舱动作需要 agent 以管理员权限运行（start.bat 已自动提权）；息屏后移动鼠标唤醒，锁屏后需输入 Windows 密码。
