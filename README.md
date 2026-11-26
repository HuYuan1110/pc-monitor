# PCMonitor Pro

> **进阶版（Pro）· 当前版本 v3.1.7** · 实时监控电脑状态，手机随时查看

一个局域网实时电脑状态监测方案：在电脑上运行一个轻量服务，手机通过同一 WiFi 打开网页（或安装 Android APK），即可**实时**查看 CPU、GPU、内存、磁盘、网络、温度、功耗、进程等全套运行指标，支持历史曲线、阈值告警、驾驶舱横屏大屏、系统媒体播放信息、快捷控制等。

> ⚠️ **重要：电脑端服务需要以「管理员身份运行」**
>
> 温度读取、硬件传感器访问、部分系统级操作需要管理员权限。请以管理员身份运行启动脚本（右键 → 以管理员身份运行），并在弹出的「用户账户控制（UAC）」窗口点击「是」。非管理员模式下大多数指标仍可用，但温度等依赖底层权限的数据会显示为 N/A。

```
手机 (Web / Android APK)
        │  同一局域网 · HTTP + SSE 实时推送
        ▼
电脑 agent.py  ──► psutil / WMI / LibreHardwareMonitor / WinRT
        │
        └──► SQLite 历史数据
```

---

## 功能特性

- **实时刷新**：基于 SSE 服务器推送，无轮询延迟。刷新间隔可选 实时(0.5s) / 1s / 2s / 3s / 5s / 10s，全部档位实测精确生效
- **全方位指标**：CPU（占用/频率/每核心/温度）、GPU（占用/显存/温度/功耗）、内存（占用/提交/内存条）、磁盘（速度/容量/SMART 温度）、网络（上下行速度/连接数/Ping）、电池、主板温度、进程统计
- **驾驶舱大屏**：横屏沉浸布局，2×2 指标卡 + 12 个快捷操作键（终端/资源管理器/任务管理器/静音/播放控制/截图/睡眠/锁屏等）
- **在播信息**：通过 Windows 系统媒体接口读取歌名、艺人、封面，并可联网反查歌曲时长显示进度
- **阈值告警**：CPU/GPU/内存/磁盘/温度等超限自动高亮提醒
- **历史曲线**：近 60 秒迷你趋势图 + 更长时段历史查询
- **天气显示**、**多语言**（中/英）、**PWA 添加到主屏幕**

> 注：游戏帧率采集功能已在 v3.1.4 中**彻底移除**。

---

## 目录结构

| 路径 | 说明 |
|---|---|
| `pc-monitor/agent.py` | 电脑端服务（单文件，Python） |
| `pc-monitor/启动服务_稳定版.bat` | **唯一日常入口**：提权启动，可见窗口，关窗口即停 |
| `pc-monitor/start_visible.vbs` | 可见窗口启动器（被上面的 bat 调用） |
| `pc-monitor/config.json` | 端口、刷新间隔、告警阈值配置 |
| `pc-monitor/requirements.txt` | Python 依赖 |
| `pc-monitor/tools/LibreHardwareMonitor/` | 温度采集工具（可选，缺失时温度显示 N/A） |
| `pc-monitor-app/www/index.html` | 手机端页面（单文件 Web APP） |
| `pc-monitor-app/android/` | Capacitor Android 工程（打包 APK 用） |
| `pc-monitor-app/build_apk.sh` | APK 打包脚本 |

---

## 快速开始

### 电脑端

1. 安装 Python 3.9+（[python.org](https://www.python.org/downloads/)），安装时勾选 "Add Python to PATH"
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. **右键 `启动服务_稳定版.bat` → 以管理员身份运行**，UAC 弹窗点「是」
4. 启动后会显示标题为 `PCMonitor-Agent-RUNNING` 的黑色窗口（这就是服务本体，**关闭该窗口即停止服务**），窗口里会打印手机访问地址，例如 `http://192.168.1.10:8888`

> 温度显示需要 LibreHardwareMonitor：把该工具放在 `tools/LibreHardwareMonitor/`（已随项目提供）。出于体积考虑部分二进制可能未纳入版本库，缺失时不影响其他指标。

### 手机端

**方式 A · 网页（最简单）**：手机连同一 WiFi，浏览器打开电脑端打印的地址（如 `http://192.168.1.10:8888`），浏览器菜单选「添加到主屏幕」即可当 APP 用。

**方式 B · Android APK**：安装 `电脑监测_v3.1.7.apk`，在 APP 内填入电脑 IP 和端口（默认 8888）连接。或从源码打包（见 `pc-monitor-app/build_apk.sh`）。

> 首次连接若防火墙拦截，请放行 TCP 8888 端口。

---

## 刷新间隔说明

刷新采用 **SSE（Server-Sent Events）服务器主动推送**，不是轮询：电脑每次采样完立即推给手机，无请求往返延迟。间隔在「设置」页选择，会实时下发给电脑端生效。

| 档位 | 实测推送间隔 |
|---|---|
| 实时 | 0.50s |
| 1s / 2s / 3s / 5s / 10s | 精确匹配设定值 |

实现上采用**分层采集**：需要实时刷新的指标（CPU/内存/网络/磁盘速度）走毫秒级快路径；变化缓慢的汇总数据（进程列表/句柄数）在独立进程低频采集，避免阻塞实时推送。详见 `agent.py` 中的 `slow_loop` / `heavy_loop` / `store_loop`。

---

## 权限说明

| 功能 | 是否需管理员 |
|---|---|
| CPU / 内存 / 网络 / 磁盘 / GPU 基本指标 | 否 |
| 温度（LibreHardwareMonitor / WMI 传感器） | **是** |
| 部分系统级快捷操作 | **是** |
| 防火墙放行 8888 端口 | **是**（仅首次） |

请以管理员身份运行 `启动服务_稳定版.bat`。

---

## 技术栈

- **后端**：Python 标准库 `http.server` + 多线程，psutil / WMI / pywin32 采集，SQLite 存历史，SSE 推送
- **手机端**：单文件 HTML/CSS/JS，Capacitor 8 打包 Android，Screen Orientation + Status Bar 插件
- **在播信息**：WinRT `GlobalSystemMediaTransportControls`
- **无前端框架、无构建步骤、无外部 CDN**，可完全离线运行

---

## 许可

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。

---

## 致谢

- [psutil](https://github.com/giampaolo/psutil) — 跨平台系统信息
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — 硬件传感器
- [Capacitor](https://capacitorjs.com/) — 跨平台 APP 打包
