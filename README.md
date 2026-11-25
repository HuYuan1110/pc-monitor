# 电脑监测工具（PC Monitor）

一个**单文件**的电脑运行状态监测代理：在电脑上运行后，手机在**同一局域网**内打开一个网页（可"添加到主屏幕"当 APP 用），即可实时查看电脑的 CPU、内存、固态/硬盘、GPU、频率、温度、功耗、网络等全套运行状态，并支持历史曲线与阈值告警。

```
手机 ──(局域网 HTTP + SSE 实时推送)──> 电脑上的 agent.py
```

---

## 一、功能清单

| 分类 | 展示项（获取不到的自动显示 N/A） |
|------|-------------------------------|
| **CPU** | 总使用率、每核心使用率、用户态/内核态/中断占比、当前主频/基频/睿频上限、处理器队列长度、中断/秒、每核+封装温度、封装功耗(W) |
| **内存** | 已用/可用/总量/使用率、提交量/提交上限、页面交换 Pages/sec、缓存/分页池/非分页池、内存频率、模块数/容量 |
| **硬盘** | 读/写速度、读/写延迟、磁盘忙碌%、队列长度、各分区剩余容量、SMART 温度、SSD 磨损/寿命、通电时长 |
| **GPU** | 使用率%、显存已用/总量、核心温度、实时功耗+功耗上限、核心/显存频率、风扇转速%、P-State、编/解码占用、占用进程列表 |
| **网络** | 上传/下载速度、累计收发流量、输出队列长度、TCP 连接数（按状态）、Ping 延迟/丢包 |
| **温度/风扇** | CPU/GPU/硬盘/主板温度、风扇转速 RPM、各路电压 |
| **功耗/电池** | 电量%、充放电状态、充放电功率、剩余续航、电池健康度、CPU/GPU 实时功耗 |
| **系统/进程** | 运行时间、启动时间、系统负载、进程/线程/句柄数、每进程 CPU/内存、进程列表、各分区空间 |
| **其他** | 采样间隔可配置、历史曲线、阈值告警、卡片式布局（点击卡片展开二级详情） |

---

## 二、电脑端运行（新手只需 3 步）

### 方式 A：双击运行（推荐，最简单）

**第 1 步**：双击 `start.bat`。

**第 2 步**：首次会自动安装依赖（约 10~60 秒），随后自动启动，窗口里会打印手机访问地址。

**第 3 步**：手机浏览器打开窗口里打印的地址（例如 `http://192.168.1.5:8888`）。

> 想验证环境是否正常：双击 `start.bat` 后它会自动装依赖并启动；也可以命令行运行
> `python agent.py --selfcheck` 单独做一次环境自检。

### 方式 B：命令行运行

```bash
pip install -r requirements.txt
python agent.py
```

启动后会打印手机访问地址，例如：

```
手机浏览器访问:  http://192.168.1.100:8888
本机访问:        http://127.0.0.1:8888
```

> 可选参数：`python agent.py --port 9000 --interval 0.5`
> - `--port` 端口（默认 8888）
> - `--interval` 采样间隔秒（默认 1，越小越实时）

---

## 三、手机端使用（当 APP 用）

1. 手机连接与电脑**同一个 WiFi/局域网**。
2. 手机浏览器打开 `http://<电脑局域网IP>:8888`（启动时打印的那个地址）。
3. 打开后是一个深色卡片式控制台，**点击任意卡片**展开该分类的完整二级详情。
4. 浏览器菜单里点 **"添加到主屏幕 / Add to Home Screen"**（iOS Safari 点分享→添加到主屏幕；Android Chrome 点 ⋮ → 添加到主屏幕）。
5. 回到桌面即可像 APP 一样全屏打开，无需再输网址。

> 原理：`agent.py` 内置了 PWA（manifest + service worker + 图标），添加到主屏幕后就是一个本地应用。

---

## 四、联动原理（手机如何看到电脑数据）

```
┌──────────┐   HTTP GET  /            ┌─────────────────────┐
│  手机     │ ───────────────────────▶ │  agent.py（电脑）    │
│ 浏览器    │   返回内嵌 Web 控制台     │                     │
│ (PWA)    │ ◀─────────────────────── │  • 采集线程（1s 一次） │
│          │   SSE 长连接 /api/stream  │  • SQLite 历史        │
│          │ ◀── data: {...} 实时推送 ─│  • 告警判断           │
└──────────┘                          └─────────────────────┘
```

- **HTTP**：手机首次打开时，电脑把整个控制台页面（HTML/CSS/JS）发给手机。
- **SSE（Server-Sent Events）**：手机与电脑之间保持一条单向长连接，电脑每采集一次就把最新快照实时推给手机，无需手机轮询，延迟极低。
- **历史**：采集数据同时写入 `monitor.db`（SQLite），点开详情时手机请求 `/api/history?metric=...` 拉取趋势曲线。
- **无需公网**：全程局域网内通信，不需要互联网、不需要注册账号、数据不出内网。

### 关键接口

| 接口 | 说明 |
|------|------|
| `GET /` | 返回内嵌控制台页面 |
| `GET /api/snapshot` | 一次性获取当前完整快照 |
| `GET /api/stream` | SSE 实时推送（默认 1 秒一条） |
| `GET /api/history?metric=cpu.total&seconds=300` | 历史趋势数据 |
| `GET /api/config` / `POST /api/config` | 读取 / 修改采样间隔、阈值等 |
| `GET /manifest.json`、`/sw.js`、`/icon.png` | PWA 资源 |

---

## 五、各指标的数据来源（哪些需要额外准备）

> 原则：**获取不到就显示 N/A，不影响其它指标**。下表说明每个指标从哪来、需要什么。

| 数据 | 来源 | 是否需要额外准备 |
|------|------|----------------|
| CPU 使用率/频率、内存、硬盘读写、网络、进程 | `psutil` | 已随依赖安装 ✅ |
| CPU 队列长度、中断/秒、提交量、页交换、缓存池、磁盘延迟/忙碌、SMART、电池健康度 | Windows `WMI`（`wmi`+`pywin32`） | 已随依赖安装 ✅ |
| **GPU**（使用率/显存/温度/功耗/频率/风扇/P-State/进程） | `nvidia-smi` | NVIDIA 显卡驱动自带，无需额外操作 ✅ |
| **CPU 温度/封装功耗、主板温度、风扇 RPM、各路电压** | LibreHardwareMonitor | ⚠️ 需要额外装（见下） |
| 内存时序 (CL 等) | — | 无法通过系统接口获取，需 CPU-Z 等工具，恒显 N/A |

### 可选增强：LibreHardwareMonitor（补齐温度/风扇/电压/功耗）

Windows 系统本身不直接暴露 CPU 温度/风扇/电压，推荐用免费开源工具 **LibreHardwareMonitor** 提供：

1. 下载：<https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases>，解压后运行 `LibreHardwareMonitor.exe`。
2. 它启动后会在后台开启 WMI 数据源（`root\LibreHardwareMonitor`），`agent.py` 会自动读取，无需任何配置。
3. 想开机自启：把它的快捷方式放进 `shell:startup`（`Win+R` 输入 `shell:startup` 回车，放入快捷方式），并右键该 exe 勾选"以管理员身份运行"以获得更高权限读取传感器。

装上后，温度/风扇/电压/封装功耗等卡片就会显示真实数据。

---

## 六、电脑部署（开机自启 + 防火墙）

### 6.1 开机自启（推荐：双击脚本，一键搞定）

右键 `install_autostart.bat` → **"以管理员身份运行"**，即可注册开机自启任务（任务名 `PCMonitor`），下次开机自动启动监测。

想取消：右键 `uninstall_autostart.bat` → 以管理员身份运行。

> 也可以手动用 PowerShell 注册（等效）：
>
> ```powershell
> $action  = New-ScheduledTaskAction -Execute "python" -Argument "D:\AI\MyDigitalSpace\pc-monitor\agent.py"
> $trigger = New-ScheduledTaskTrigger -AtStartup
> Register-ScheduledTask -TaskName "PCMonitor" -Action $action -Trigger $trigger -RunLevel Highest -Description "电脑监测代理"
> ```
>
> 若已用 PyInstaller 打包成 `agent.exe`（见第八节），把 `-Execute` 改为 exe 路径、去掉 Argument 即可。

### 6.2 防火墙放行（手机访问的关键一步）

手机访问不到，99% 是 Windows 防火墙拦截。**以管理员身份**执行：

```powershell
netsh advfirewall firewall add rule name="PCMonitor" dir=in action=allow protocol=TCP localport=8888
```

### 6.3 查看局域网 IP

```bash
ipconfig
```
找 `IPv4 地址`（形如 192.168.x.x），就是手机要访问的地址。

---

## 七、手机 APP 自动打包

手机端本质是一个网页，有两种"打包成 APP"的方式：

### 方式 A：PWA（零成本，推荐）

`agent.py` 已内置 PWA，手机浏览器"添加到主屏幕"即可，**无需任何打包步骤**。

### 方式 B：打包成真正的 APK（Android 安装包）

用 Capacitor 把网页包成 Android App（需要电脑装 Node.js + Android Studio）：

```bash
# 1. 建项目
mkdir pcm-apk && cd pcm-apk
npm init -y
npm install @capacitor/core @capacitor/cli @capacitor/android

# 2. 生成 www 目录，放入页面
mkdir www
curl -o www/index.html http://127.0.0.1:8888/

# 3. 初始化 Capacitor
npx cap init "PCMonitor" "com.example.pcmonitor" --web-dir www
npx cap add android

# 4. 打包 APK
npx cap sync
cd android && ./gradlew assembleDebug
```

产物在 `android/app/build/outputs/apk/debug/app-debug.apk`，拷到手机安装即可。
（首次需在 `android/app/src/main/AndroidManifest.xml` 添加 `<uses-permission android:name="android.permission.INTERNET"/>` 并允许明文 HTTP，或直接用 PWA 方式更省事。）

> 说明：APK 只是把"访问固定 IP 的网页"包成壳，运行时仍要求手机与电脑在同一局域网。若要**跨互联网远程访问**（不在同一 WiFi），需要内网穿透（如 frp、Tailscale、ZeroTier），不属于本工具内置范围。

---

## 八、电脑端打包成单个 exe（PyInstaller）

把 `agent.py` 打包成免装 Python 的 `.exe`，双击即用：

```bash
pip install pyinstaller
pyinstaller --onefile --name PCMonitor agent.py
```

打包后 `dist/PCMonitor.exe` 即为单文件可执行程序，拷到任意 Windows 电脑双击运行。

> 打包后的 exe 里已包含内嵌的网页控制台，无需携带其它文件。`monitor.db` 会自动在 exe 同目录生成。

---

## 九、常见问题

| 问题 | 解决 |
|------|------|
| 手机打不开 / 超时 | 电脑防火墙未放行，执行 6.2 的命令；确认手机与电脑同一 WiFi |
| CPU 温度显示 N/A | 未安装 LibreHardwareMonitor，见第五节 |
| GPU 显示 N/A | 非 NVIDIA 显卡（AMD/Intel 需靠 LibreHardwareMonitor）；或未装 NVIDIA 驱动 |
| 电池健康度 N/A | 台式机无电池；或系统 WMI 未暴露该字段（属正常） |
| 内存时序恒 N/A | 系统接口不提供时序，需 CPU-Z 等工具，属正常 |
| 首次打开 CPU 显示 0% | 首秒正在建立采样基线，1~2 秒后正常 |
| 端口被占用 | 换端口 `python agent.py --port 9000` |

---

## 十、项目结构

```
pc-monitor/
├── agent.py               # 监控代理（单文件，内嵌 Web 控制台 + PWA）
├── start.bat              # ★ 双击启动（自动找 Python + 装依赖 + 启动）
├── install_autostart.bat  # ★ 一键开机自启（右键以管理员运行）
├── uninstall_autostart.bat# 取消开机自启
├── requirements.txt       # Python 依赖
├── README.md              # 本文档
└── build/
    ├── build_exe.bat      # 一键打包 Windows exe
    └── build_apk.md       # APK 打包详细步骤
```
