#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电脑监测代理（单文件版）
============================================================
功能：
  1. 采集本机运行状态：CPU / 内存 / 硬盘 / GPU / 网络 / 温度风扇 / 功耗电池 / 系统进程
  2. 通过局域网 HTTP + SSE 把数据实时推送给手机浏览器（PWA，可"添加到主屏幕"当 APP 用）
  3. SQLite 保存历史数据，支持趋势曲线查询
  4. 阈值告警（温度过高、磁盘将满、内存过高等）
  5. 不支持的数据源自动降级为 N/A（不影响整体运行）

依赖（尽量少，缺失项自动降级）：
  - psutil           必装：CPU/内存/硬盘/网络/进程/电池基础数据   pip install psutil
  - wmi + pywin32    推荐：中断/队列/提交量/页交换/SMART/电池健康  pip install wmi pywin32
  - nvidia-smi       可选：NVIDIA GPU 详细数据（驱动自带）
  - LibreHardwareMonitor  可选：CPU/主板/风扇/电压/功耗等温度数据（见 README）
  - Intel PresentMon    已内置（tools/PresentMon/PresentMon.exe），游戏帧率采集；缺失时首次启用自动下载

运行：  python agent.py [--port 8888] [--interval 1.0]
"""
import os
import sys
import json
import time
import math
import queue
import socket
import struct
import zlib
import sqlite3
import threading
import subprocess
import argparse
import platform
import re
import urllib.parse
import tempfile
from collections import deque

# ---------------------------------------------------------------------------
# 可选依赖
# ---------------------------------------------------------------------------
try:
    import psutil
except Exception:
    psutil = None

HAS_WMI = False
try:
    import pythoncom
    import wmi as _wmi_mod
    HAS_WMI = True
except Exception:
    _wmi_mod = None

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "port": 8888,
    "interval": 1.0,          # 采样间隔（秒）
    "ping_interval": 5.0,     # Ping 检测间隔（秒）
    "ping_target": "auto",    # auto=默认网关；也可填 IP / 域名
    "history_keep": 3600,     # SQLite 历史保留秒数
    "alerts_enabled": True,
    "fps_enabled": False,     # 游戏帧率采集（需 Intel PresentMon + 管理员权限），默认关闭
    "fps_process_name": "",   # 点选锁定的进程名（如 tlou-ii.exe）；留空=自动识别/全局采集兜底
    "fps_pid": 0,             # 点选锁定的 PID：PresentMon 优先用 --process_id 抓，避免重名/多实例
    "thresholds": {
        "cpu_temp": 85.0,
        "gpu_temp": 85.0,
        "disk_temp": 55.0,
        "cpu_percent": 90.0,
        "gpu_percent": 90.0,
        "mem_percent": 90.0,
        "disk_free_percent": 10.0,
        "battery_low": 15.0,
    },
}

CONFIG = dict(DEFAULT_CONFIG)
CONFIG_LOCK = threading.Lock()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor.db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# v3.1.0：与 APP 的 UI build / 设置页版本号保持一致
APP_VERSION = "3.1.7"


def load_saved_config():
    """恢复上次保存的用户配置（帧率开关/手动进程名等），损坏则静默回退默认值。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return
        with CONFIG_LOCK:
            for k in ("port", "interval", "ping_target", "alerts_enabled",
                      "fps_enabled", "fps_process_name", "fps_pid"):
                if k in saved:
                    CONFIG[k] = saved[k]
            # 兼容 v2.5.7 手输框导致的重复名（如 "tlou-ii.exe tlou-ii.exe"）
            nm = str(CONFIG.get("fps_process_name") or "").strip()
            toks = nm.split()
            if len(toks) > 1 and all(t.lower() == toks[0].lower() for t in toks):
                CONFIG["fps_process_name"] = toks[0]
            # v2.5.10：重启时清理失效锁定（旧 PID 已退出/被别的进程复用），
            # 否则会一直锁着 ROB 这类已退出进程报 no_process，直到用户手动重选。
            try:
                locked_pid = int(CONFIG.get("fps_pid") or 0)
            except Exception:
                locked_pid = 0
            if psutil and locked_pid > 0:
                stale = not psutil.pid_exists(locked_pid)
                if not stale:
                    try:
                        real = (psutil.Process(locked_pid).name() or "").lower()
                        want = str(CONFIG.get("fps_process_name") or "").lower()
                        stale = bool(want) and real != want
                    except Exception:
                        stale = True
                if stale:
                    CONFIG["fps_pid"] = 0
                    CONFIG["fps_process_name"] = ""
            if isinstance(saved.get("thresholds"), dict):
                for k, v in saved["thresholds"].items():
                    if k in CONFIG["thresholds"]:
                        CONFIG["thresholds"][k] = v
    except Exception:
        pass


def save_config_file():
    """持久化用户配置，保证 APP 设置页与状态页、agent 重启后都是同一份状态。"""
    try:
        with CONFIG_LOCK:
            data = {k: CONFIG.get(k) for k in (
                "port", "interval", "ping_target", "alerts_enabled",
                "fps_enabled", "fps_process_name", "fps_pid", "thresholds")}
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


load_saved_config()

# ---------------------------------------------------------------------------
# 全局共享
# ---------------------------------------------------------------------------
SNAPSHOT = {}
SNAPSHOT_LOCK = threading.Lock()

SSE_CLIENTS = []          # 元素为 queue.Queue
SSE_LOCK = threading.Lock()

PING_RESULT = {"latency_ms": None, "loss": None, "target": None, "ok": False}
PING_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _num(v):
    """把字符串/数字转 float，无法解析返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "[N/A]", "N/A", "[Not Supported]", "Not Supported", "Unknown", "N\\A"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _now():
    return time.time()


def _run_cmd(cmd, timeout=5):
    """运行命令并返回 (stdout, stderr)。依次尝试常见编码解码，兼容中文 Windows 的 GBK 输出。"""
    import locale
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        raw = out.stdout
        for enc in (locale.getpreferredencoding(False), "gbk", "utf-8", "cp1252"):
            try:
                return raw.decode(enc), out.stderr.decode(enc, errors="replace")
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace"), ""
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# WMI 助手（线程内 COM）
# ---------------------------------------------------------------------------
_wmi_conn = None
_wmi_thread = None


def _get_wmi():
    global _wmi_conn, _wmi_thread
    if not HAS_WMI:
        return None
    tid = threading.get_ident()
    if _wmi_conn is not None and _wmi_thread == tid:
        return _wmi_conn
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
        _wmi_conn = _wmi_mod.WMI()
        _wmi_thread = tid
        return _wmi_conn
    except Exception:
        return None


def wmi_first(query_result):
    """取 WMI 结果集第一条。"""
    try:
        for r in query_result:
            return r
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# NVIDIA GPU
# ---------------------------------------------------------------------------
_GPU_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,fan.speed,pstate,utilization.encoder,utilization.decoder"
_GPU_PROC = "pid,process_name,used_memory"


def collect_gpu():
    gpus = []
    if not shutil_which("nvidia-smi"):
        return gpus
    try:
        stdout, _ = _run_cmd(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"], timeout=5)
        for line in stdout.strip().splitlines():
            if not line.strip():
                continue
            p = [x.strip() for x in line.split(",")]
            while len(p) < 13:
                p.append("")
            name = p[0]
            # nvidia-smi 返回：memory=MiB、clocks=MHz，统一转成字节/Hz
            _vram_used = _num(p[2])
            _vram_total = _num(p[3])
            _core = _num(p[7])
            _mem = _num(p[8])
            g = {
                "name": name,
                "util": _num(p[1]),
                "vram_used": _vram_used * 1048576 if _vram_used is not None else None,
                "vram_total": _vram_total * 1048576 if _vram_total is not None else None,
                "temp": _num(p[4]),
                "power": _num(p[5]),
                "power_limit": _num(p[6]),
                "core_clock": _core * 1e6 if _core is not None else None,
                "mem_clock": _mem * 1e6 if _mem is not None else None,
                "fan": _num(p[9]),
                "pstate": p[10] if p[10] else None,
                "encode": _int(p[11]),
                "decode": _int(p[12]),
                "processes": [],
            }
            gpus.append(g)
    except Exception:
        pass

    # 每个 GPU 的占用进程列表（只查第一个，避免过重）
    try:
        stdout, _ = _run_cmd(
            ["nvidia-smi", "--query-compute-apps=" + _GPU_PROC, "--format=csv,noheader,nounits"], timeout=5)
        procs = []
        for line in stdout.strip().splitlines():
            if not line.strip():
                continue
            p = [x.strip() for x in line.split(",")]
            while len(p) < 3:
                p.append("")
            _v = _num(p[2])
            procs.append({"pid": _int(p[0]), "name": p[1],
                          "vram": _v * 1048576 if _v is not None else None})
        if gpus:
            gpus[0]["processes"] = procs
    except Exception:
        pass
    return gpus


def shutil_which(name):
    from shutil import which
    return which(name)


# ---------------------------------------------------------------------------
# LibreHardwareMonitor（可选：温度/风扇/电压/功耗）
# ---------------------------------------------------------------------------
def collect_lhm():
    """查询 root\\LibreHardwareMonitor 命名空间，返回分类后的传感器数据。"""
    result = {
        "cpu_temp_sensors": [], "gpu_temp": None, "disk_temp": None,
        "motherboard_temp": None, "fans": [], "voltages": [],
        "cpu_power": None, "battery_power": None, "connected": False,
    }
    w = _get_wmi()
    if w is None:
        return result
    try:
        ns = _wmi_mod.WMI(namespace=r"root\LibreHardwareMonitor")
        sensors = ns.Sensor()
        result["connected"] = True   # 能枚举 Sensor = LibreHardwareMonitor 已注册 WMI 数据源
        for s in sensors:
            try:
                stype = str(getattr(s, "SensorType", "") or "")
                name = str(getattr(s, "Name", "") or "")
                hw = str(getattr(s, "HardwareType", "") or "")
                val = _num(getattr(s, "Value", None))
                if val is None:
                    continue
                if stype == "Temperature":
                    if hw in ("CPU", "Cpu", "CpuAmd", "CpuIntel"):
                        result["cpu_temp_sensors"].append({"name": name, "value": val})
                    elif hw.startswith("Gpu") and result["gpu_temp"] is None:
                        result["gpu_temp"] = val
                    elif hw in ("Storage", "HDD", "SSD", "NVMe") and result["disk_temp"] is None:
                        result["disk_temp"] = val
                    elif hw in ("Motherboard", "Mainboard") and result["motherboard_temp"] is None:
                        result["motherboard_temp"] = val
                elif stype == "Fan":
                    result["fans"].append({"name": name, "value": val})
                elif stype == "Voltage":
                    result["voltages"].append({"name": name, "value": val})
                elif stype == "Power":
                    if hw in ("CPU", "Cpu", "CpuAmd", "CpuIntel") and result["cpu_power"] is None and "Package" in name:
                        result["cpu_power"] = val
                    elif hw in ("CPU", "Cpu", "CpuAmd", "CpuIntel") and result["cpu_power"] is None:
                        result["cpu_power"] = val
                    elif hw in ("Battery",) and result["battery_power"] is None:
                        result["battery_power"] = val
            except Exception:
                continue
    except Exception:
        pass
    return result


def collect_cpu_temp_acpi():
    """通过 WMI root\\WMI 的 MSAcpi_ThermalZoneTemperature 读取 CPU 热区温度（无第三方依赖的备用方案）。

    返回摄氏度，读不到返回 None。该接口在部分 AMD/较新 Intel 平台不可用。
    """
    w = _get_wmi()
    if w is None:
        return None
    try:
        ns = _wmi_mod.WMI(namespace=r"root\WMI")
        rows = ns.MSAcpi_ThermalZoneTemperature()
        for r in rows:
            raw = _num(getattr(r, "CurrentTemperature", None))
            if raw is None:
                continue
            # 单位：0.1 K，即 raw 除以 10 得到开尔文
            kelvin = raw / 10.0
            celsius = kelvin - 273.15
            # 过滤异常值（<0 或 >125 视为无效）
            if 0 < celsius < 125:
                return round(celsius, 1)
    except Exception:
        pass
    return None


def _pick_cpu_temp_sensor(sensors):
    """从 LHM 的 CPU 温度传感器中挑出「封装/整体」温度，不钉在单个核心。

    AMD 平台（如 Ryzen 7 7840H）LHM 常见命名：
      - "Core (Tctl/Tdie)" / "CPU Tctl" / "Tdie"  → 封装温度（优先）
      - "CPU Package"                             → Intel 封装温度（优先）
      - "Core Max" / "Core Average"               → 整体均温（次优）
      - "Core #0" ... "Core #N"                    → 单核心温度（避免，最后才用）
    返回 (value, source_key)，其中 source_key 取值：
      lhm_package / lhm_tctl / lhm_cpu / None
    """
    if not sensors:
        return None, None
    lowered = [str(s.get("name") or "").lower() for s in sensors]

    def find(substrs):
        for i, n in enumerate(lowered):
            if any(k in n for k in substrs):
                return i
        return -1

    # 1) CPU Package（Intel）
    idx = find(("package",))
    if idx >= 0:
        return sensors[idx].get("value"), "lhm_package"
    # 2) Tctl / Tdie（AMD 封装）
    idx = find(("tctl", "tdie"))
    if idx >= 0:
        return sensors[idx].get("value"), "lhm_tctl"
    # 3) Core Max / Average / Temperature 等整体读数
    idx = find(("core max", "core average", "core (avg)", "core temperature", "cpu temperature"))
    if idx >= 0:
        return sensors[idx].get("value"), "lhm_cpu"
    # 4) 退而求其次：不含 "core #"/"core "（单核心）的任意传感器
    for i, n in enumerate(lowered):
        if "core #" not in n and "core " not in n:
            return sensors[i].get("value"), "lhm_cpu"
    # 5) 实在没有才取第一个（可能为单核心，属兜底）
    if sensors:
        return sensors[0].get("value"), "lhm_cpu"
    return None, None


def _parse_smart_vendor(data):
    """解析 SATA SMART VendorSpecific 字节数组，返回 {temp, hours, wear}。"""
    info = {}
    try:
        b = list(data)
        if len(b) < 2:
            return info
        # 每 12 字节一条属性：id, flags, value, worst, raw(6), reserved(2)
        for off in range(2, len(b) - 11, 12):
            aid = b[off]
            raw = b[off + 5:off + 11]  # 6 字节，小端
            val = 0
            for i in range(6):
                val |= raw[i] << (8 * i)
            if aid in (190, 194):       # 温度
                info.setdefault("temp", raw[0] if raw[0] > 0 else (b[off + 2]))
            elif aid == 9:              # 通电时间
                info["hours"] = val
            elif aid in (231, 177, 202, 233):  # SSD 寿命/磨损
                info["wear"] = b[off + 2]
    except Exception:
        pass
    return info


def _parse_nvme_smart(data):
    info = {}
    try:
        b = list(data)
        if len(b) < 16:
            return info
        temp = b[1]
        if 0 < temp < 128:
            info["temp"] = temp
        info["percent_used"] = b[5]
        hrs = 0
        for i in range(8):
            hrs |= b[14 + i] << (8 * i)
        if hrs > 0:
            info["hours"] = hrs
    except Exception:
        pass
    return info


def collect_smart():
    """返回 {'temp','hours','wear','model'} 或 None。"""
    w = _get_wmi()
    if w is None:
        return None
    info = {}
    try:
        ns = _wmi_mod.WMI(namespace=r"root\WMI")
        rows = ns.MSStorageDriver_FailurePredictData()
        for r in rows:
            vs = getattr(r, "VendorSpecific", None)
            if vs:
                d = _parse_nvme_smart(vs)
                info.update({k: v for k, v in d.items() if v is not None})
                info.setdefault("model", getattr(r, "InstanceName", None))
        if not info.get("temp") and not info.get("hours"):
            rows2 = ns.MSStorageDriver_ATAPISmartData()
            for r in rows2:
                vs = getattr(r, "VendorSpecific", None)
                if vs:
                    d = _parse_smart_vendor(vs)
                    info.update({k: v for k, v in d.items() if v is not None})
                    info.setdefault("model", getattr(r, "InstanceName", None))
    except Exception:
        pass
    return info if info else None


# ---------------------------------------------------------------------------
# 电池健康度 / 充放电功率（WMI root\WMI）
# ---------------------------------------------------------------------------
def collect_battery_extra():
    info = {"health": None, "cycle": None, "power": None, "plugged": None}
    w = _get_wmi()
    if w is None:
        return info
    try:
        ns = _wmi_mod.WMI(namespace=r"root\WMI")
        des = None
        full = None
        for r in ns.BatteryStaticData():
            des = _num(getattr(r, "DesignedCapacity", None))
            break
        for r in ns.BatteryFullChargedCapacity():
            full = _num(getattr(r, "FullChargedCapacity", None))
            break
        if des and full and des > 0:
            info["health"] = round(full / des * 100.0, 1)
        for r in ns.BatteryCycleCount():
            cyc = _int(getattr(r, "CycleCount", None))
            if cyc is not None:
                info["cycle"] = cyc
            break
        for r in ns.BatteryStatus():
            chg = _num(getattr(r, "ChargeRate", None)) or 0.0
            dis = _num(getattr(r, "DischargeRate", None)) or 0.0
            if chg > 0:
                info["power"] = round(chg / 1000.0, 2)
                info["plugged"] = True
            elif dis > 0:
                info["power"] = round(-dis / 1000.0, 2)
                info["plugged"] = False
            break
    except Exception:
        pass
    return info


def collect_battery_wmi():
    """WMI 兜底读取笔记本电池（root\\cimv2 的 Win32_Battery）。

    部分笔记本/驱动组合下 psutil.sensors_battery() 会返回 None，此时走本函数兜底，
    避免笔记本电池卡错误地整卡 N/A。返回 dict：{percent, status, plugged, health, source}；
    完全读不到（Win32_Battery 无行）返回 None。

    字段说明：
      EstimatedChargeRemaining  剩余电量百分比（255=未知）
      BatteryStatus            1/8=放电 2/6=交流充电 3/7=已充满 4=低 5=临界
      DesignCapacity            设计容量(mWh)
      FullChargeCapacity        满充容量(mWh)，健康度 = Full/Design*100
    """
    w = _get_wmi()
    if w is None:
        return None
    try:
        rows = list(w.Win32_Battery())
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]  # 笔记本通常只有一块电池
    pct = _num(getattr(r, "EstimatedChargeRemaining", None))
    if pct is not None and pct >= 255:
        pct = None
    code = _int(getattr(r, "BatteryStatus", None))
    des = _num(getattr(r, "DesignCapacity", None))
    full = _num(getattr(r, "FullChargeCapacity", None))
    health = None
    if des and full and des > 0:
        health = round(full / des * 100.0, 1)
    status = None
    plugged = None
    if code in (1, 8):            # 放电
        plugged = False
        status = "放电中"
    elif code in (2, 6):          # 交流供电 / 充电中
        plugged = True
        status = "充电中"
    elif code in (3, 7):          # 充满 / 充电完成
        plugged = True
        status = "已充满"
    elif code == 4:
        status = "电量低"
    elif code == 5:
        status = "电量临界"
    return {
        "percent": pct,
        "status": status,
        "plugged": plugged,
        "health": health,
        "source": "wmi",
    }


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------
def _default_gateway():
    try:
        stdout, _ = _run_cmd(["route", "print", "-4"], timeout=3)
        for line in stdout.splitlines():
            if line.strip().startswith("0.0.0.0"):
                parts = line.split()
                if len(parts) >= 4:
                    return parts[2]
    except Exception:
        pass
    return None


def _do_ping(target):
    try:
        stdout, _ = _run_cmd(["ping", "-n", "1", "-w", "1000", target], timeout=3)
        # 匹配中文/英文的延时字段：时间=xxms / time=xxms / time<1ms
        import re
        m = re.search(r"(?:时间|time)[=<]\s*(\d+)\s*ms", stdout, re.IGNORECASE)
        if m:
            return {"latency_ms": float(m.group(1)), "loss": 0.0, "target": target, "ok": True}
        # 丢包判断
        m2 = re.search(r"(\d+)%", stdout)
        if m2:
            return {"latency_ms": None, "loss": float(m2.group(1)), "target": target, "ok": float(m2.group(1)) == 0}
        return {"latency_ms": None, "loss": None, "target": target, "ok": False}
    except Exception:
        return {"latency_ms": None, "loss": None, "target": target, "ok": False}


def ping_loop():
    while True:
        tgt = CONFIG.get("ping_target", "auto")
        if tgt == "auto":
            tgt = _default_gateway() or "223.5.5.5"
        res = _do_ping(tgt)
        with PING_LOCK:
            PING_RESULT.update(res)
        time.sleep(max(1.0, CONFIG.get("ping_interval", 5.0)))


# ---------------------------------------------------------------------------
# 游戏帧率（Intel PresentMon，MIT 开源，零注入、不挂覆盖层、不触发反作弊）
# ---------------------------------------------------------------------------
# PresentMon 通过 ETW 会话采集帧时间，需要管理员权限。采集流程：
#   1. 识别前台有独立窗口的进程（不再要求全屏；识别不到则全局采集兜底）
#   2. 定位 PresentMon.exe（内置 tools/PresentMon/ → 项目根 → PATH → 常见安装目录）
#   3. 拉起 PresentMon.exe（按进程名过滤，持续写 CSV）
#   4. 每秒增量读取 CSV 新增行的 MsBetweenPresents，计算 fps = 1000 / 帧时间
# 无权限 / 未找到 PresentMon / 无游戏运行时均降级为 N/A，绝不影响其它指标。
# 未找到 exe 时，首次启用会自动下载到 tools/PresentMon/（失败仍降级 N/A）。
_PRESENTMON_URL = "https://github.com/GameTechDev/PresentMon/releases"
_PRESENTMON_API = "https://api.github.com/repos/GameTechDev/PresentMon/releases/latest"
_PRESENTMON_DIR = os.path.join(BASE_DIR, "tools", "PresentMon")
_PRESENTMON_EXE_PATH = os.path.join(_PRESENTMON_DIR, "PresentMon.exe")


def _log(msg):
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [FPS] {msg}")
    except Exception:
        pass


def _presentmon_exe():
    """v3.1.4：帧率功能已永久移除，不再定位/下载/启动 PresentMon。"""
    return None

    """按顺序定位 PresentMon.exe：
      1) 项目内置 tools/PresentMon/PresentMon.exe（随项目分发，相对路径，不写死盘符）
      2) 项目根目录 PresentMon.exe / PresentMon/PresentMon.exe（旧版兼容）
      3) 系统 PATH
      4) 常见安装目录 C:\\Program Files\\PresentMon\\ 等
      找不到返回 None（调用方降级 N/A）。"""
    for c in (
        os.path.join(BASE_DIR, "tools", "PresentMon", "PresentMon.exe"),
        os.path.join(BASE_DIR, "PresentMon", "PresentMon.exe"),
        os.path.join(BASE_DIR, "PresentMon.exe"),
    ):
        if os.path.isfile(c):
            return c
    p = shutil_which("PresentMon.exe") or shutil_which("PresentMon")
    if p:
        return p
    for d in (
        r"C:\Program Files\PresentMon",
        r"C:\Program Files (x86)\PresentMon",
        r"C:\Program Files\Intel\PresentMon",
    ):
        exact = os.path.join(d, "PresentMon.exe")
        if os.path.isfile(exact):
            return exact
        if os.path.isdir(d):
            try:
                for fn in sorted(os.listdir(d)):
                    low = fn.lower()
                    if low.startswith("presentmon") and low.endswith(".exe"):
                        return os.path.join(d, fn)
            except Exception:
                pass
    return None


def _download_presentmon():
    """v3.1.4：帧率已移除，不再下载 PresentMon。"""
    return None

    """自动下载 Intel PresentMon（控制台版 exe）到 tools/PresentMon/PresentMon.exe。

    返回 exe 路径；失败返回 None（调用方降级 N/A，绝不影响其他指标）。
    使用 GitHub API 查询最新 Release 的控制台 exe（排除 .msi 与符号包）。
    """
    import urllib.request
    try:
        os.makedirs(_PRESENTMON_DIR, exist_ok=True)
        req = urllib.request.Request(
            _PRESENTMON_API,
            headers={"User-Agent": "PCMonitor", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8", "replace"))
        asset = None
        for a in meta.get("assets", []):
            name = a.get("name", "")
            low = name.lower()
            # 选控制台版：PresentMon-x.y.z-x64.exe，排除 .msi 和符号压缩包
            if low.startswith("presentmon") and low.endswith(".exe") and "x64" in low:
                asset = a
                break
        if asset is None:
            _log("未在 GitHub Release 中找到 PresentMon 控制台 exe，请手动放入 tools/PresentMon/")
            return None
        url = asset.get("browser_download_url") or asset.get("url")
        if not url:
            return None
        tmp = _PRESENTMON_EXE_PATH + ".part"
        _log(f"正在自动下载 PresentMon（{asset.get('name')}）...")
        req2 = urllib.request.Request(
            url, headers={"User-Agent": "PCMonitor", "Accept": "application/octet-stream"})
        with urllib.request.urlopen(req2, timeout=300) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        size = os.path.getsize(tmp)
        if size < 100000:
            try:
                os.remove(tmp)
            except Exception:
                pass
            _log("下载的 PresentMon 文件过小，疑似失败")
            return None
        if os.path.exists(_PRESENTMON_EXE_PATH):
            try:
                os.remove(_PRESENTMON_EXE_PATH)
            except Exception:
                pass
        os.replace(tmp, _PRESENTMON_EXE_PATH)
        _log(f"PresentMon 已自动下载到 {_PRESENTMON_EXE_PATH}（{size} 字节）")
        return _PRESENTMON_EXE_PATH
    except Exception as e:
        _log(f"自动下载 PresentMon 失败：{e}（可手动放入 tools/PresentMon/PresentMon.exe）")
        return None


# 非游戏进程黑名单：即使在前台也不作为帧率采集目标（避免浏览器/桌面产生假帧）
_FPS_EXCLUDE = {
    "explorer.exe", "dwm.exe", "cmd.exe", "conhost.exe", "powershell.exe",
    "pwsh.exe", "python.exe", "pythonw.exe", "windowsterminal.exe",
    "taskmgr.exe", "svchost.exe", "lsass.exe", "system", "registry",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "applicationframehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "runtimebroker.exe",
    "settingsynchost.exe", "ctfmon.exe", "spdysvc.exe", "yourphone.exe",
    "phoneexperiencehost.exe", "chrome.exe", "msedge.exe", "firefox.exe",
}

# 启动器/平台客户端：不是游戏本体，抓到时提示"请等游戏本体启动"
_FPS_LAUNCHERS = {
    "steam.exe", "steamwebhelper.exe", "steamcmd.exe",
    "epicgameslauncher.exe", "origin.exe", "eadesktop.exe",
    "battlenet.exe", "battle.net.exe", "galaxyclient.exe", "goggalaxy.exe",
    "riotclientservices.exe", "leagueclient.exe", "riotclient.exe",
    "ubisoftconnect.exe", "upc.exe", "xboxapp.exe", "xboxgamebar.exe",
    "gamingservices.exe", "discord.exe", "wegame.exe", "tencentdl.exe",
    "qq.exe", "wechat.exe", "psn.exe", "playstation.exe",
    "nclauncher.exe", "nclauncher2.exe", "bnet.exe",
}

# v2.5.10 非游戏后台：GPU 覆盖层/助手/聊天/系统设置等有窗口且占 GPU，
# 但不是游戏本体。仅用于自动识别与点选列表降权，不影响帧计算。
_FPS_BACKGROUND = {
    "nvidia overlay.exe", "nvidia geforce experience.exe",
    "systemsettings.exe", "game-assistant.exe", "gameassistant.exe",
    "weixin.exe", "wechat.exe", "workbuddy.exe", "hiddify.exe",
    "wetype_update.exe", "wallpaper64.exe", "wallpaper.exe",
    "steamwebhelper.exe", "applicationframehost.exe", "textinputhost.exe",
    "androidstudio.exe", "code.exe", "devenv.exe", "msedge.exe", "chrome.exe",
    "rob run.exe",  # 反作弊/后台服务，不是游戏本体（v2.6.0 动态识别排除）
}

# v2.6.0：全局 CSV 里常见的非游戏帧来源（服务/覆盖层/反作弊/工具）
_FPS_NON_GAME_HINTS = (
    "anticheat", "overlay", "webview", "launcher", "updater", "assistant",
    "service.exe", "helper.exe", "broker.exe", "runtime", "shipper",
    "crashhandler", "installer", "setup.exe", "rob run", "gameoverlay",
)

_GPU_PID_CACHE = {"time": 0.0, "pids": set()}


def _gpu_process_pids_cached(ttl=5.0):
    """5 秒缓存版 GPU PID 列表；全局动态识别每轮都要用，避免每秒起 nvidia-smi。"""
    now = time.time()
    if now - _GPU_PID_CACHE["time"] > ttl:
        _GPU_PID_CACHE["time"] = now
        try:
            _GPU_PID_CACHE["pids"] = set(_gpu_process_pids())
        except Exception:
            _GPU_PID_CACHE["pids"] = set()
    return set(_GPU_PID_CACHE["pids"])


def _fullscreen_pids():
    """返回当前占据整屏（或接近整屏）的可见窗口所属 PID 集合。"""
    if platform.system() != "Windows":
        return set()
    try:
        import ctypes
        u32 = ctypes.windll.user32
        sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        if sw <= 0 or sh <= 0:
            return set()
        out = set()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            try:
                if not u32.IsWindowVisible(hwnd):
                    return True
                rect = ctypes.wintypes.RECT()
                if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w >= sw * 0.9 and h >= sh * 0.9:
                    pid = ctypes.c_ulong()
                    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        out.add(int(pid.value))
            except Exception:
                pass
            return True

        u32.EnumWindows(cb, 0)
        return out
    except Exception:
        return set()


def _is_admin():
    """Windows 下是否管理员（PresentMon 的 ETW 会话硬性要求）；非 Windows 视为可用。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _gpu_process_pids():
    """用 nvidia-smi pmon 取正在使用 GPU 的进程 PID（含图形进程），失败返回空集合。
    仅用于自动识别时的候选加权，不参与帧率计算，失败完全不影响采集。"""
    if not shutil_which("nvidia-smi"):
        return set()
    try:
        # v3.0.8：用 _run_cmd（多编码回退）读取，避免 universal_newlines 在
        # Python 3.13 以 UTF-8 解码 nvidia-smi 的本地化(GBK)输出时刷
        # UnicodeDecodeError（曾刷上万条日志）。
        out, _ = _run_cmd(["nvidia-smi", "pmon", "-c", "1"], timeout=3)
        out = out or ""
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            # 数据行格式：gpu pid type sm mem enc dec command
            if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids
    except Exception:
        return set()


def _window_proc_info(hwnd):
    """取窗口对应的 (进程名, PID)；失败返回 (None, None)。"""
    try:
        import ctypes
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or not psutil:
            return None, None
        return (psutil.Process(pid.value).name() or ""), int(pid.value)
    except Exception:
        return None, None


def _window_titles_by_pid():
    """枚举可见顶层窗口，返回 {PID: 窗口标题}。供帧率点选列表展示"哪个是游戏"。
    失败返回空字典；只取非空标题、可见窗口，成本极低。"""
    if platform.system() != "Windows":
        return {}
    out = {}
    try:
        import ctypes
        u32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            try:
                if not u32.IsWindowVisible(hwnd):
                    return True
                n = u32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if not title:
                    return True
                pid = ctypes.c_ulong()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value and pid.value not in out:
                    out[int(pid.value)] = title[:80]
            except Exception:
                pass
            return True

        u32.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


def _foreground_game_process():
    """识别前台游戏进程，返回 (进程名, PID, 是否启动器)。

    v2.5.6：不写死白名单——前台有独立可见窗口、且不在黑名单中的进程即为候选；
    若前台是启动器（Steam/EA/Epic 等）单独标记，前端提示"请等游戏本体启动"。
    识别不到返回 (None, None, False)，由 collect() 走候选遍历/全局采集兜底。"""
    if platform.system() != "Windows":
        return None, None, False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return None, None, False
        name, pid = _window_proc_info(hwnd)
        low = (name or "").lower()
        if not low or low in _FPS_EXCLUDE or low in _FPS_BACKGROUND:
            return None, None, False
        rect = ctypes.wintypes.RECT()
        if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            # v3.1.1：过滤小工具窗口，避免误判为游戏
            if (rect.right - rect.left) < 400 or (rect.bottom - rect.top) < 300:
                return None, None, False
        return name, pid, (low in _FPS_LAUNCHERS)
    except Exception:
        return None, None, False


def _enumerate_game_candidates():
    """遍历所有可见顶层窗口，返回游戏候选 [(进程名, PID), ...]。

    排序策略：使用 GPU 的进程（nvidia-smi pmon）优先 > 窗口面积大者优先；
    排除黑名单与启动器。不写死任何游戏 exe 名。"""
    if platform.system() != "Windows":
        return []
    try:
        import ctypes
        u32 = ctypes.windll.user32
        gpu_pids = _gpu_process_pids_cached()
        results = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            try:
                if not u32.IsWindowVisible(hwnd):
                    return True
                rect = ctypes.wintypes.RECT()
                if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                w, h = rect.right - rect.left, rect.bottom - rect.top
                # v3.1.1：小工具窗口(如 SpeedBall 212x150、Hidden Window)不是游戏，
                # 即便占 GPU 也无呈现帧意义；提高门槛避免误识别成游戏导致假 warming。
                if w < 400 or h < 300:
                    return True
                name, pid = _window_proc_info(hwnd)
                low = (name or "").lower()
                if (not low or low in _FPS_EXCLUDE or low in _FPS_LAUNCHERS
                        or low in _FPS_BACKGROUND):
                    return True
                score = (1 if pid in gpu_pids else 0) * 10000000 + w * h
                results.append((score, name, pid))
            except Exception:
                pass
            return True

        u32.EnumWindows(cb, 0)
        results.sort(key=lambda x: x[0], reverse=True)
        seen, out = set(), []
        for _, name, pid in results:
            if pid in seen:
                continue
            seen.add(pid)
            out.append((name, pid))
        return out[:6]
    except Exception:
        return []


class FpsMonitor:
    """管理 PresentMon 进程生命周期，并按帧时间计算实时/平均/1% Low FPS。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None           # PresentMon subprocess
        self._csv = None            # CSV 输出路径
        self._game = None           # 当前监控的游戏进程名
        self._col = None            # MsBetweenPresents 列索引
        self._file_off = 0          # CSV 已读字节偏移
        self._sess_count = 0        # 本会话累计帧数
        self._sess_ms = 0.0         # 本会话累计帧时间
        self._last_ms = None        # 最近一帧时间
        self._last_try = 0.0        # 上次启动尝试时间（限流重试）
        self._last_download = 0.0   # 上次自动下载尝试时间（限流）
        self._downloading = False   # 是否正在后台下载 PresentMon
        self._reason = ""
        self._target_key = None     # 当前采集目标 (进程名或"*", PID)
        self._pid = None            # 目标进程 PID
        self._detected = None       # 全局采集时从 CSV 识别到的游戏进程名
        self._proc_col = None       # CSV 进程名列索引（Application / ProcessName）
        self._pid_col = None        # CSV ProcessID 列索引
        self._errf = None           # PresentMon stderr 日志句柄
        self._last_err_log = 0.0    # stderr error 日志节流
        self._seen_stderr_errors = set()  # 已记录的 stderr error，避免刷屏
        self._capture_start = 0.0   # 本次采集起始时间（区分 warming / no_process / no_game）
        # v3.1.0 僵尸自愈：检测 PresentMon 活着但不采集（孤儿 ETW 会话导致卡死）
        self._zombie_check = 0.0    # 上次僵尸检查时间
        self._last_cpu = 0.0        # 上次 PresentMon CPU 时间
        self._zombie_hits = 0       # 连续空转计数
        self._mode = "global"       # global（默认）/ manual（点选兜底）
        self._target_proc = None    # 手动点选的进程名；全局模式为 None
        # v2.6.0 动态识别状态
        self._frames = deque(maxlen=2000)   # (ts, 帧时间ms, 进程名小写)
        self._key_stats = {}                # 进程名 -> [帧数, 帧时间和]，按进程累计平均
        self._pid_names = {}                # PID -> 进程名（解析 CSV 的 pid:<n> 标识）
        self._detected_key = None           # 当前识别到的游戏（进程名小写）
        self._idle_since = 0.0              # 无游戏开始时间（超过阈值暂停全局抓取省资源）
        self._idle_timeout = 15.0           # 无游戏 15 秒后暂停 PresentMon
        # v3.1.1：手动锁定抓不到帧但别的进程在出帧时，自动放弃锁定回全局。
        self._manual_no_frame_since = 0.0   # 手动锁定持续无帧的起始时间
        self._other_frames_seen = False     # 本轮是否看到非锁定进程的帧

    # ---- 会话清理（v3.0.2：单实例常驻，避免 ETW 会话竞态 error 4201） ----
    @staticmethod
    def _cleanup_stale_session():
        """启动唯一实例前清理残留：kill 本 agent 旧 PresentMon 进程 + logman stop PCMonitorFPS -ets。

        只 kill 命令行带本项目标记（_fps_live.csv / PCMonitorFPS）的进程，
        避免误杀用户手动开启的 PresentMon 实例。全部忽略报错（会话可能本就不存在）。
        """
        # 1) kill 本 agent 遗留的 PresentMon 进程
        try:
            if psutil:
                for p in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        nm = (p.info.get("name") or "").lower()
                        if "presentmon" not in nm:
                            continue
                        cmd = " ".join(p.info.get("cmdline") or [])
                        if ("_fps_live.csv" in cmd) or ("PCMonitorFPS" in cmd):
                            p.terminate()
                            try:
                                p.wait(timeout=2)
                            except Exception:
                                p.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        # 2) 释放残留 ETW 会话（忽略报错）
        # v3.1.0 关键修复：不只 stop PCMonitorFPS，还要清理所有本项目可能创建的
        # 孤儿会话（诊断探针 Probe 系列、历史命名）。这些会话在 PresentMon 被
        # kill 后不会自动释放，进程没了但会话永久"正在运行"，导致后续任何
        # PresentMon 启动都卡死（CPU 不增 / stderr 空 / CSV 不生成）——实测根因。
        stale_sessions = ["PCMonitorFPS", "PCMonitorProbeV1", "PCMonProbe_v1",
                          "PCMonitorProbeV2", "PCMonitorProbe", "PresentMon"]
        try:
            # 先从 logman query 里找出所有名字含 PCMon/PresentMon 的会话，一并纳入
            q = subprocess.run(
                ["logman", "query", "-ets"],
                capture_output=True, timeout=8,
                creationflags=(0x08000000 if os.name == "nt" else 0))
            text = (q.stdout or b"").decode("utf-8", errors="replace") + \
                   (q.stderr or b"").decode("utf-8", errors="replace")
            for line in text.splitlines():
                name = line.split()[0] if line.split() else ""
                low = name.lower()
                if ("pcmon" in low or "presentmon" in low) and name not in stale_sessions:
                    stale_sessions.append(name)
        except Exception:
            pass
        for sess in stale_sessions:
            try:
                subprocess.run(
                    ["logman", "stop", sess, "-ets"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=(0x08000000 if os.name == "nt" else 0), timeout=8,
                )
            except Exception:
                pass

    # ---- 生命周期 ----
    def _stop(self):
        p = self._proc
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
            except Exception:
                pass
        self._proc = None
        self._csv = None
        self._game = None
        self._col = None
        self._file_off = 0
        self._frames.clear()
        self._sess_count = 0
        self._sess_ms = 0.0
        self._last_ms = None
        self._target_key = None
        self._pid = None
        self._detected = None
        self._proc_col = None
        self._pid_col = None
        self._capture_start = 0.0
        self._target_proc = None
        self._key_stats.clear()
        self._pid_names.clear()
        self._detected_key = None
        self._idle_since = 0.0
        if self._errf is not None:
            try:
                self._errf.close()
            except Exception:
                pass
            self._errf = None

    def reset_target(self):
        """采集目标变化时调用：下一轮采样立即重建会话（全局或手动）。"""
        with self._lock:
            self._target_key = None
            self._last_try = 0.0
            self._idle_since = 0.0
            self._reason = ""

    def select_target(self, pid, name, enabled=True):
        """手动点选兜底：保存 PID+名字并以 --process_id 抓帧；传空名则恢复全局自动。"""
        name = str(name or "").strip()
        if name and not name.lower().endswith(".exe"):
            name += ".exe"
        try:
            pid = int(pid)
        except Exception:
            pid = 0
        pid = pid if pid > 0 else 0
        with CONFIG_LOCK:
            CONFIG["fps_process_name"] = name
            CONFIG["fps_pid"] = pid
            CONFIG["fps_enabled"] = bool(enabled)
        with self._lock:
            self._stop()
            self._target_key = None
            self._last_try = 0.0
            self._reason = ""
            if not name or not pid:
                # v2.6.0：清空手动覆盖，交回全局抓取 + 自动识别
                self._mode = "global"
                self._target_proc = None
                self._pid = None
                return self._na("warming", "waiting", mode="global")
            self._mode = "manual"
            self._target_proc = name
            self._pid = pid
            if not enabled:
                return self._na("disabled", "off", game=name, mode="manual")
            if not _is_admin():
                return self._na("no_permission", "off", game=name, mode="manual")
            if not _presentmon_exe():
                return self._na("presentmon_missing", "waiting", game=name, mode="manual")
            if not self._start(name, pid, admin=True, mode="manual"):
                return self._na("start_failed", "waiting", game=name, mode="manual")
            time.sleep(0.25)
            if self._proc is None or self._proc.poll() is not None:
                return self._na(self._diag_failure(name), "waiting", game=name, mode="manual")
            self._reason = "warming"
            return self._na("warming", "waiting", game=name, mode="manual")

    def set_enabled(self, on):
        with self._lock:
            if not on:
                if self._proc is not None:
                    self._stop()
                    _log("游戏帧率采集已关闭，PresentMon 进程已释放")
                # v3.1.4：帧率已移除，不再触碰 ETW 会话（避免系统级卡顿）
            else:
                # 重新开启时让全局抓取立即重建，不等旧会话超时
                self._target_key = None
                self._idle_since = 0.0
                self._last_try = 0.0

    def state(self):
        """供 /api/fps_target 返回：开关、锁定进程与采集状态的唯一真相源。"""
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            return {
                "enabled": bool(CONFIG.get("fps_enabled", False)),
                "process_name": str(CONFIG.get("fps_process_name") or "").strip(),
                "pid": int(CONFIG.get("fps_pid") or 0) or self._pid or 0,
                "process": (self._target_proc
                            or str(CONFIG.get("fps_process_name") or "").strip() or None),
                "admin": _is_admin(),
                "presentmon": bool(_presentmon_exe()),
                "mode": self._mode,
                "reason": self._reason or None,
                "status": "capturing" if alive else "waiting",
            }

    def _bg_download(self):
        # v3.1.4：帧率已移除，不做任何下载
        self._downloading = False

    def _start(self, game, pid=None, admin=True, mode=None):
        """真正拉起 PresentMon.exe（官方参数）。

        v2.6.0：默认全局采集（game=None → 不带 --process_id，抓所有呈现中的帧）；
        仅手动点选兜底时带 --process_id。两种模式都写固定 CSV，按列解析。
        """
        exe = _presentmon_exe()
        if not exe:
            now = time.time()
            if not self._downloading and now - self._last_download >= 300.0:
                self._last_download = now
                self._downloading = True
                threading.Thread(target=self._bg_download, daemon=True).start()
            self._reason = "未找到 PresentMon.exe（正在尝试自动下载）"
            _log(self._reason)
            self._game = game
            return False
        self._stop()
        # v3.0.2：启动唯一实例前先清理残留会话（kill 本 agent 旧 PresentMon + logman stop），
        # 避免 ETW 会话未释放干净就重启撞出 error 4201。
        self._cleanup_stale_session()
        try:
            # 固定 CSV 路径（工具目录内），agent 明确知道读哪个文件。
            os.makedirs(_PRESENTMON_DIR, exist_ok=True)
            csv_path = os.path.join(_PRESENTMON_DIR, "_fps_live.csv")
            # v3.0.2：全程追加写同一个 _fps_live.csv，不再每次删除重建。
            # 单实例常驻后 PresentMon 自己持续追加，agent 只增量 tail。
            # 若手动验证遗留了超大 CSV（>256MB），仅在这种情况下轮转，避免磁盘爆满。
            try:
                if os.path.exists(csv_path) and os.path.getsize(csv_path) > 256 * 1024 * 1024:
                    os.remove(csv_path)
            except Exception:
                pass
            # v2.5.10：顺手清理过期的大 CSV（手动验证遗留的 PresentMon-*.csv，
            # 单个可达 150MB）。只删超过 1 小时的，避免误删用户正在用的文件。
            try:
                import glob as _glob
                stale_cut = time.time() - 3600
                for old in _glob.glob(os.path.join(_PRESENTMON_DIR, "PresentMon-*.csv")):
                    try:
                        if os.path.getmtime(old) < stale_cut:
                            os.remove(old)
                            _log(f"已清理过期 CSV：{os.path.basename(old)}")
                    except Exception:
                        pass
            except Exception:
                pass
            # v3.0.2：固定 --session_name PCMonitorFPS。不再带 --stop_existing_session
            # （该参数会刷 "a trace session named ... is already running and it will be stopped"
            #  并撞 error 4201）；会话清理统一交给 _cleanup_stale_session() 在启动前完成。
            cmd = [exe,
                   "--output_file", csv_path,
                   "--session_name", "PCMonitorFPS",
                   "--no_console_stats",
                   "--v1_metrics"]   # 经典列名 MsBetweenPresents，确保解析兼容
            # 不加 --restart_as_admin：agent 已由提权的 start.bat 拉起，PresentMon 继承管理员权限，
            # 再加该参数反而可能触发额外的 UAC 弹窗。
            if game:
                if pid:
                    cmd += ["--process_id", str(pid)]
                else:
                    cmd += ["--process_name", game]
                # PresentMon 2.6.0 官方参数名是 --terminate_on_proc_exit
                # （不存在 --stop_on_process_exit，已用 --help 核实）。
                cmd.append("--terminate_on_proc_exit")
            # 全局采集：不加 --terminate_on_proc_exit（无目标进程会立即退出）
            err_path = os.path.join(_PRESENTMON_DIR, "PresentMon-stderr.log")
            try:
                os.makedirs(_PRESENTMON_DIR, exist_ok=True)
                # v3.0.2：stderr 统一 UTF-8 with BOM（utf-8-sig），修掉 Windows 记事本乱码。
                # 追加写保留历史 error 便于排查；每次拉起写入一条分隔标记。
                self._errf = open(err_path, "a", encoding="utf-8-sig", errors="replace")
                self._errf.write(f"\n===== PresentMon start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                self._errf.flush()
            except Exception:
                self._errf = None
            creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._errf if self._errf else subprocess.DEVNULL,
                creationflags=creationflags,
                cwd=os.path.dirname(exe) or None,
            )
            self._csv = csv_path
            self._game = game
            self._target_proc = game
            self._pid = pid
            self._col = None
            self._proc_col = None
            self._pid_col = None
            # v3.0.2：CSV 追加写、不重建，从当前末尾开始 tail，避免历史帧混入滑动窗口
            try:
                self._file_off = os.path.getsize(csv_path) if os.path.exists(csv_path) else 0
            except Exception:
                self._file_off = 0
            self._capture_start = time.time()
            self._reason = ""
            self._mode = mode or ("manual" if game else "global")
            self._detected = None
            self._detected_key = None
            self._idle_since = 0.0
            self._target_key = (game or "*", pid)
            _log(f"已拉起 PresentMon：{exe}")
            _log(f"命令: {' '.join(cmd)}")
            return True
        except Exception as e:
            self._reason = f"启动 PresentMon 失败：{e}"
            self._game = game
            _log(self._reason)
            return False

    def _diag_failure(self, game=None):
        """PresentMon 进程异常退出时，读 stderr 判断真实原因（不再一律 no_permission）。"""
        err = ""
        try:
            with open(os.path.join(_PRESENTMON_DIR, "PresentMon-stderr.log"),
                      encoding="utf-8", errors="replace") as ef:
                err = ef.read(4000)
        except Exception:
            pass
        low = err.lower()
        if any(k in low for k in ("denied", "privilege", "administrator", "elevat", "access")):
            return "no_permission"
        if any(k in low for k in ("no process", "process name", "process id",
                                  "invalid process", "not found")):
            return "no_process" if game else "waiting_game"
        if err.strip():
            _log(f"PresentMon stderr: {err.strip()[:400]}")
            return "start_failed"
        return "no_process" if game else "waiting_game"

    # ---- 读取 CSV ----
    def _parse_header(self):
        """按列名定位 MsBetweenPresents / 进程名 / PID 列，不写死列号。
        PresentMon 各版本列序不同：v1 用 Application/ProcessID，其他版本用 ProcessName。"""
        if not self._csv or not os.path.exists(self._csv):
            return
        try:
            first = ""
            # v3.1.1：共享读表头，避免 PresentMon 独占 CSV 时 open() 失败。
            if os.name == "nt":
                try:
                    import ctypes
                    from ctypes import wintypes
                    k32 = ctypes.windll.kernel32
                    k32.CreateFileW.restype = ctypes.c_void_p
                    h = k32.CreateFileW(self._csv, 0x80000000,
                                        0x1 | 0x2 | 0x4, None, 3,
                                        0x80000000, None)
                    if h and h != ctypes.c_void_p(-1).value:
                        try:
                            buf = ctypes.create_string_buffer(8192)
                            got = wintypes.DWORD()
                            if k32.ReadFile(h, buf, 8191, ctypes.byref(got), None):
                                head = buf.raw[:got.value].decode(
                                    "utf-8-sig", errors="replace")
                                first = head.splitlines()[0] if head.splitlines() else ""
                        finally:
                            k32.CloseHandle(h)
                except Exception:
                    first = ""
            if not first:
                with open(self._csv, "r", encoding="utf-8-sig", errors="replace") as f:
                    first = f.readline()
            if not first.strip():
                return   # 表头还没刷盘，下一秒重试
            cols = [c.strip().lstrip("\ufeff") for c in first.rstrip("\r\n").split(",")]
            self._col = None
            self._proc_col = None
            self._pid_col = None
            for i, c in enumerate(cols):
                lc = c.lower().strip()
                if lc in ("processname", "process_name", "process", "application"):
                    self._proc_col = i
                if lc in ("processid", "process_id", "pid"):
                    self._pid_col = i
                # 兼容各版本帧时间列：MsBetweenPresents / msBetweenPresents / FrameTime
                if lc in ("frametime", "msbetweenpresents") or lc.endswith("betweenpresents"):
                    self._col = i
        except Exception:
            pass

    def _log_stderr_errors(self):
        """把 PresentMon-stderr.log 里的 error/failed/denied 记录到 agent 日志（节流去重）。"""
        try:
            p = os.path.join(_PRESENTMON_DIR, "PresentMon-stderr.log")
            if not os.path.exists(p):
                return
            now = time.time()
            if now - self._last_err_log < 1.0:
                return
            with open(p, encoding="utf-8", errors="replace") as ef:
                err = ef.read(8000)
            low = err.lower()
            if not any(k in low for k in ("error", "failed", "denied", "access denied")):
                return
            self._last_err_log = now
            for line in err.splitlines():
                s = line.strip()
                ls = s.lower()
                if s and any(k in ls for k in ("error", "failed", "denied")) \
                        and s not in self._seen_stderr_errors:
                    self._seen_stderr_errors.add(s)
                    if len(self._seen_stderr_errors) > 50:
                        self._seen_stderr_errors.clear()
                    _log(f"PresentMon stderr error: {s[:300]}")
        except Exception:
            pass

    def _read_new_frames(self):
        """增量 tail 固定 CSV，返回 [(帧时间ms, 进程名或PID), ...]；按列名定位，跳过空行/不完整行。"""
        out = []
        if not self._csv or not os.path.exists(self._csv):
            return out
        try:
            if self._col is None:
                self._parse_header()
            if self._col is None:
                return out
            # v3.1.1：Windows 上 PresentMon 写 CSV 时若未开放共享读，普通 open(rb)
            # 会 PermissionError 被静默吞掉→永远 0 帧→前端卡"等待游戏窗口"。
            # 改用 Win32 CreateFileW 以 FILE_SHARE_READ|WRITE|DELETE 打开，绕过独占。
            raw = b""
            if os.name == "nt":
                try:
                    import ctypes
                    from ctypes import wintypes
                    k32 = ctypes.windll.kernel32
                    GENERIC_READ = 0x80000000
                    SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
                    OPEN_EXISTING = 3
                    k32.CreateFileW.restype = ctypes.c_void_p
                    h = k32.CreateFileW(self._csv, GENERIC_READ, SHARE_ALL,
                                        None, OPEN_EXISTING, 0x80000000, None)
                    if h and h != ctypes.c_void_p(-1).value:
                        try:
                            sz = wintypes.LARGE_INTEGER()
                            if k32.GetFileSizeEx(h, ctypes.byref(sz)):
                                size = int(sz.value)
                                if size < self._file_off:
                                    self._file_off = 0
                                k32.SetFilePointerEx(
                                    h, wintypes.LARGE_INTEGER(self._file_off),
                                    None, 0)  # FILE_BEGIN
                                n = size - self._file_off
                                if n > 0:
                                    buf = ctypes.create_string_buffer(n)
                                    got = wintypes.DWORD()
                                    if k32.ReadFile(h, buf, n,
                                                    ctypes.byref(got), None):
                                        raw = buf.raw[:got.value]
                                self._file_off = size
                        finally:
                            k32.CloseHandle(h)
                except Exception:
                    raw = b""
            if not raw:
                with open(self._csv, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if size < self._file_off:      # 文件被重写/截断，从头读
                        self._file_off = 0
                    f.seek(self._file_off)
                    raw = f.read()
                    self._file_off = size
            text = raw.decode("utf-8", errors="replace")
            max_col = max([c for c in (self._col, self._proc_col, self._pid_col) if c is not None])
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) <= max_col:      # 不完整行：表头还没写完或行被截断
                    continue
                v = _num(parts[self._col])
                if v is None or v <= 0:        # 跳过 NA / 空值 / 非法帧时间
                    continue
                proc = ""
                if self._proc_col is not None and self._proc_col < len(parts):
                    proc = parts[self._proc_col].strip()
                # Application 列在无提权时可能是 <unknown>，用 PID 列兜底作为身份标识
                if (not proc or proc.lower() in ("<unknown>", "unknown")) \
                        and self._pid_col is not None and self._pid_col < len(parts):
                    pid_val = parts[self._pid_col].strip()
                    if pid_val and pid_val != "NA":
                        proc = f"pid:{pid_val}"
                out.append((v, proc))
        except Exception:
            pass
        return out

    # ---- v2.6.0 全局抓取动态识别 ----
    def _proc_key(self, proc):
        """把 CSV 的进程标识统一成小写进程名；pid:<n> 用 psutil 解析成真实名字。"""
        s = str(proc or "").strip()
        if not s or s.lower() in ("<unknown>", "unknown", "na"):
            return ""
        if s.lower().startswith("pid:"):
            try:
                pid = int(s[4:])
            except Exception:
                return s.lower()
            if pid not in self._pid_names:
                nm = ""
                if psutil:
                    try:
                        if psutil.pid_exists(pid):
                            nm = psutil.Process(pid).name() or ""
                    except Exception:
                        nm = ""
                self._pid_names[pid] = nm
            nm = self._pid_names.get(pid) or ""
            return nm.lower() if nm else s.lower()
        return os.path.basename(s.replace("\\", "/")).lower()

    @staticmethod
    def _is_game_proc(key):
        """判断进程名是否可能是游戏本体（排除桌面/浏览器/启动器/工具/反作弊）。"""
        k = str(key or "").lower().strip()
        if not k:
            return False
        if k in _FPS_EXCLUDE or k in _FPS_LAUNCHERS or k in _FPS_BACKGROUND:
            return False
        if k.startswith("pid:"):
            return False   # 身份未知，不主动选它当游戏
        return not any(h in k for h in _FPS_NON_GAME_HINTS)

    def _candidate_map(self):
        """当前可能的游戏：窗口候选 + 前台窗口 + 全屏窗口（强信号）+ GPU 进程（弱信号）。
        返回 {key: (显示名, PID)}、全屏/GPU PID 集合、强信号 key 集合。"""
        out, strong = {}, set()
        try:
            cands = _enumerate_game_candidates()
        except Exception:
            cands = []
        for name, pid in cands:
            key = self._proc_key(name)
            if key and self._is_game_proc(key):
                out.setdefault(key, (name, pid or 0))
                strong.add(key)
        try:
            fg, fg_pid, _launcher = _foreground_game_process()
        except Exception:
            fg, fg_pid = None, None
        if fg:
            key = self._proc_key(fg)
            if key and self._is_game_proc(key):
                out.setdefault(key, (fg, fg_pid or 0))
                strong.add(key)
        try:
            fs_pids = _fullscreen_pids()
        except Exception:
            fs_pids = set()
        try:
            gpu_pids = _gpu_process_pids_cached()
        except Exception:
            gpu_pids = set()
        for pid in fs_pids:
            key = self._proc_key(f"pid:{pid}")
            if key and self._is_game_proc(key):
                out.setdefault(key, (key, pid))
                strong.add(key)
        for pid in gpu_pids:
            key = self._proc_key(f"pid:{pid}")
            if key and self._is_game_proc(key):
                out.setdefault(key, (key, pid))   # 仅用于加权，不能单独判定为游戏
        return out, fs_pids, gpu_pids, strong

    def _is_zombie_proc(self, now):
        """v3.1.0：检测 PresentMon 是否卡死（进程活着但 CPU 不增、CSV 无数据）。

        孤儿 ETW 会话会导致 PresentMon 启动后空转：CPU 时间不增、不写 CSV。
        连续 3 次（约 3 秒）检测到即判定僵尸，触发 kill + 清会话 + 重建。
        """
        p = self._proc
        if p is None or p.poll() is not None:
            self._zombie_hits = 0
            return False
        # CSV 有内容就说明在正常采集，直接排除
        try:
            if self._csv and os.path.exists(self._csv) and os.path.getsize(self._csv) > 0:
                self._zombie_hits = 0
                return False
        except Exception:
            pass
        try:
            cpu = 0.0
            if psutil:
                try:
                    cpu = float(psutil.Process(p.pid).cpu_times().user +
                                psutil.Process(p.pid).cpu_times().system)
                except Exception:
                    cpu = 0.0
        except Exception:
            self._zombie_hits = 0
            return False
        if abs(cpu - self._last_cpu) < 0.01:   # CPU 几乎不增
            self._zombie_hits += 1
        else:
            self._zombie_hits = 0
        self._last_cpu = cpu
        if self._zombie_hits >= 3:
            _log("检测到 PresentMon 僵尸进程（CPU 不增且无 CSV），自动清理并重建")
            self._stop()
            self._cleanup_stale_session()
            self._zombie_hits = 0
            self._last_try = 0.0   # 允许立即重建
            return True
        return False

    def _recent_game_frames(self, now, strong_keys=None):
        """近 2 秒内是否还有游戏帧（用于空闲暂停判断）。"""
        return any((now - t <= 2.0 and k in strong_keys)
                   for t, _ms, k in self._frames) if strong_keys is not None else \
               any((now - t <= 2.0 and self._is_game_proc(k))
                   for t, _ms, k in self._frames)

    def _pick_game(self, recent, candidate_map, fs_pids, gpu_pids, strong_keys):
        """从滑动窗口里挑帧占比最高、且属于强信号（窗口/前台/全屏）的进程。"""
        groups = {}
        for _t, ms, key in recent:
            if not key or key not in strong_keys or not self._is_game_proc(key):
                continue
            g = groups.setdefault(key, [0, 0.0])
            g[0] += 1
            g[1] += ms
        if not groups:
            return None
        best_key, best_score = None, -1
        for key, (count, _ms) in groups.items():
            score = count                      # 帧数占比为主
            info = candidate_map.get(key)
            pid = info[1] if info else 0
            if pid and pid in fs_pids:
                score += 2000000               # 全屏窗口权重最高
            if pid and pid in gpu_pids:
                score += 500000                # GPU 占用加成
            if score > best_score:
                best_key, best_score = key, score
        return best_key

    # ---- 主采集 ----
    def collect(self):
        with self._lock:
            enabled = bool(CONFIG.get("fps_enabled", False))
            if not enabled:
                if self._proc is not None:
                    self._stop()
                return self._na("disabled", "off")

            # 1) 权限前置：非管理员无法建立 ETW 会话，如实报 no_permission（不误报 no_game）
            admin = _is_admin()
            if not admin:
                if self._proc is not None:
                    self._stop()
                self._reason = "agent 非管理员启动，PresentMon 无法建立 ETW 会话"
                return self._na("no_permission", "off")

            # 2) exe 定位（内置 tools/PresentMon/ → PATH → 常见目录；缺则后台下载）
            exe = _presentmon_exe()
            if not exe:
                now0 = time.time()
                if not self._downloading and now0 - self._last_download >= 300.0:
                    self._last_download = now0
                    self._downloading = True
                    threading.Thread(target=self._bg_download, daemon=True).start()
                return self._na("presentmon_missing", "waiting")

            # 3) v2.6.0：默认全局抓取，游戏从 CSV 动态识别；手动点选仅作覆盖兜底
            manual = str(CONFIG.get("fps_process_name") or "").strip()
            manual_pid = _int(CONFIG.get("fps_pid")) or 0
            now = time.time()
            mode, game, pid = "global", None, None
            candidate_map, fs_pids, gpu_pids, strong_keys = {}, set(), set(), set()
            has_game = False
            if manual:
                game = manual if manual.lower().endswith(".exe") else manual + ".exe"
                pid = manual_pid or None
                if not pid and psutil:   # 兼容旧配置只存了名字
                    try:
                        for p in psutil.process_iter(["pid", "name"]):
                            if (p.info.get("name") or "").lower() == game.lower():
                                pid = p.info["pid"]
                                break
                    except Exception:
                        pass
                mode, has_game = "manual", True
                if pid and psutil and not psutil.pid_exists(pid):
                    return self._na("no_process", "waiting", game=game, mode=mode)
            else:
                candidate_map, fs_pids, gpu_pids, strong_keys = self._candidate_map()
                has_game = bool(strong_keys)

            self._mode = mode
            self._target_proc = game          # 全局模式为 None，proc 由 CSV 识别填充
            target = (game or "*", pid)
            proc_alive = self._proc is not None and self._proc.poll() is None
            changed = self._target_key != target

            # v3.0.2 单实例常驻：PresentMon 一旦拉起就长期运行，全程追加写同一个 CSV。
            # 不再因“无游戏空闲 15 秒”kill 进程，也不再定时重复拉起——停旧启新会与 ETW
            # 会话释放产生竞态，刷 “a trace session named ... is already running” 并撞 error 4201。
            # 无游戏时只返回 N/A，进程保持常驻，游戏一起来立刻自动出数（reason=ok）。
            if mode == "global" and not has_game and not self._recent_game_frames(now, strong_keys):
                return self._na("no_game", "waiting", mode="global")
            self._idle_since = 0.0

            # 仅两种情况重建唯一实例：① 目标被用户手动切换；② 进程意外退出。
            # 重建统一由 _start() 负责：启动前先清理残留会话，再拉起唯一实例（不再 kill 健康进程）。
            if changed or not proc_alive:
                if now - self._last_try < 3.0:
                    return self._na(self._diag_failure(game), "waiting", game=game, mode=mode)
                self._last_try = now
                self._start(game, pid, admin=admin, mode=mode)
                time.sleep(0.6)   # ETW 会话拉起需要时间
                proc_alive = self._proc is not None and self._proc.poll() is None
                if not proc_alive:
                    return self._na(self._diag_failure(game), "waiting", game=game, mode=mode)
            if not proc_alive:
                return self._na(self._diag_failure(game), "waiting", game=game, mode=mode)

            # v3.1.0 僵尸自愈：进程活着但空转(孤儿ETW会话)→已 kill+清会话，
            # 这里立即重建一个干净实例；下一轮就能正常抓帧。
            if self._is_zombie_proc(now):
                self._last_try = now
                self._start(game, pid, admin=admin, mode=mode)
                time.sleep(0.6)
                if self._proc is None or self._proc.poll() is not None:
                    return self._na(self._diag_failure(game), "waiting", game=game, mode=mode)
                return self._na("warming", "waiting", game=game, mode=mode)

            # 4) 增量读取固定 CSV（按列名解析），全局模式按进程名分组
            self._log_stderr_errors()
            new_frames = self._read_new_frames()
            manual_key = self._proc_key(game) if manual else ""
            self._other_frames_seen = False
            self._manual_matched_frame = False
            for ms, proc in new_frames:
                key = self._proc_key(proc)
                if not key:
                    continue
                if mode == "manual":
                    raw = str(proc or "").lower()
                    matched = (key == manual_key) or (pid and raw == f"pid:{pid}")
                    if not matched:
                        # v3.1.1：锁定进程无帧，但别的进程在出帧——记录这个信号
                        if self._is_game_proc(key):
                            self._other_frames_seen = True
                        continue
                    self._manual_matched_frame = True
                elif not self._is_game_proc(key):
                    continue
                self._frames.append((now, ms, key))
                stat = self._key_stats.setdefault(key, [0, 0.0])
                stat[0] += 1
                stat[1] += ms
                self._last_ms = ms

            # v3.1.1 手动锁定自愈：锁定的进程持续无帧，但 CSV 里别的游戏在出帧，
            # 说明锁错了目标（如锁到 SpeedBall 小工具，而真游戏 A Glimmer 在渲染）。
            # 超过 6 秒自动清空 manual 覆盖，交回全局自动识别，避免前端干等。
            if mode == "manual":
                if self._manual_matched_frame:
                    self._manual_no_frame_since = 0.0
                else:
                    if not self._manual_no_frame_since:
                        self._manual_no_frame_since = now
                    if self._other_frames_seen and (now - self._manual_no_frame_since) >= 6.0:
                        _log(f"手动锁定 {game} 抓不到帧，但检测到其他游戏在渲染，自动切回全局自动模式")
                        with CONFIG_LOCK:
                            CONFIG["fps_process_name"] = ""
                            CONFIG["fps_pid"] = 0
                        save_config_file()
                        self._mode = "global"
                        self._target_proc = None
                        self._pid = None
                        self._target_key = None
                        self._manual_no_frame_since = 0.0
                        self._stop()
                        self._last_try = 0.0
                        return self._na("warming", "waiting", mode="global")

            recent = [f for f in self._frames if now - f[0] <= 2.0][-120:]
            if mode == "manual":
                selected_key = manual_key
                display = game or manual_key
                selected_pid = pid or 0
            else:
                selected_key = self._pick_game(recent, candidate_map, fs_pids,
                                               gpu_pids, strong_keys)
                if not selected_key:
                    elapsed = now - (self._capture_start or now)
                    if strong_keys:
                        key0 = next(iter(strong_keys))
                        name = (candidate_map.get(key0) or (key0, 0))[0]
                        # v3.0.8：warming 必须有超时。旧版这里无超时，识别到游戏
                        # 但 CSV 无帧时会永远卡在 warming（幽灵状态），前端干等。
                        if elapsed > 8.0:
                            csv_ok = bool(self._csv and os.path.exists(self._csv)
                                          and os.path.getsize(self._csv) > 0)
                            reason = "no_frames" if not csv_ok else "waiting_game"
                            return self._na(reason, "waiting", game=name, mode=mode)
                        return self._na("warming", "waiting", game=name, mode=mode)
                    return self._na("no_game" if elapsed > 8.0 else "warming",
                                    "waiting", mode=mode)
                info = candidate_map.get(selected_key)
                display = info[0] if info else selected_key
                selected_pid = info[1] if info else 0

            self._detected_key = selected_key
            self._detected = display
            game_ms = [ms for _t, ms, k in recent if k == selected_key]
            if not game_ms:
                elapsed = now - (self._capture_start or now)
                if elapsed > 8.0:
                    csv_ok = bool(self._csv and os.path.exists(self._csv)
                                  and os.path.getsize(self._csv) > 0)
                    if not csv_ok:
                        reason = "no_frames"   # PresentMon 在跑但 CSV 无数据
                    elif mode == "manual" and pid and psutil and psutil.pid_exists(pid):
                        reason = "waiting_game"
                    elif mode == "global":
                        reason = "no_game"
                    else:
                        reason = "no_process"
                    return self._na(reason, "waiting", game=display, mode=mode)
                return self._na("warming", "waiting", game=display, mode=mode)

            # 5) 近 2 秒 / 最多 120 帧滑动窗口：平均 FPS、1% Low、帧时间
            frame_ms = sum(game_ms) / len(game_ms)
            cur = 1000.0 / frame_ms if frame_ms > 0 else None
            stat = self._key_stats.get(selected_key, [0, 0.0])
            avg = (stat[0] * 1000.0 / stat[1]) if (stat[0] and stat[1] > 0) else None
            low1 = None
            if len(game_ms) >= 10:
                slowest = sorted(game_ms, reverse=True)[:max(1, int(len(game_ms) * 0.01))]
                if slowest:
                    low1 = 1000.0 / (sum(slowest) / len(slowest))
            self._log_stderr_errors()
            return {
                "available": True,
                "current": round(cur, 1) if cur is not None else None,
                "avg": round(avg, 1) if avg is not None else None,
                "low1": round(low1, 1) if low1 is not None else None,
                "frametime": round(frame_ms, 2),
                "game": display,
                "process": display,
                "pid": selected_pid,
                "reason": "ok",
                "status": "capturing",
                "mode": mode,
            }

    def _na(self, reason="waiting_game", status="waiting", game=None, mode=None):
        locked = (game or self._target_proc
                  or str(CONFIG.get("fps_process_name") or "").strip() or None)
        # v3.0.8：附人类可读诊断，便于从 /api/status 直接定位问题
        diag_map = {
            "no_frames": "PresentMon 已启动但未捕获到任何帧：请确认游戏正在运行且以管理员权限启动 start.bat",
            "no_game": "未检测到正在运行的游戏",
            "waiting_game": "等待游戏帧（游戏可能已最小化或未呈现）",
            "no_permission": "agent 非管理员启动，无法建立 ETW 会话",
            "presentmon_missing": "未找到 PresentMon.exe",
            "start_failed": "PresentMon 启动失败，见 PresentMon-stderr.log",
        }
        return {"available": False, "current": None, "avg": None,
                "low1": None, "frametime": None, "game": game,
                "process": locked, "pid": (_int(CONFIG.get("fps_pid")) or self._pid or 0),
                "reason": reason, "status": status, "mode": mode,
                "diag": diag_map.get(reason, "")}


FPS_MONITOR = FpsMonitor()


# ---------------------------------------------------------------------------
# 主采集器
# ---------------------------------------------------------------------------
class Sampler:
    def __init__(self):
        self._net_prev = None
        self._net_time = None
        self._disk_prev = None
        self._disk_time = None
        self._cpu_times_prev = None
        self._smart_cache = {}
        self._smart_time = 0.0
        self._batt_cache = {}
        self._batt_time = 0.0
        self._hung_cache = set()
        self._hung_time = 0.0
        self._peak_cache = {}          # pid -> 历史 CPU 占用峰值
        self._peak_time = 0.0
        # v3.1.7 分层采集：慢数据(WMI/进程列表/句柄)放后台低频刷新，
        # 主采样循环只做 psutil 毫秒级快采集，避免 16 秒慢操作拖垮刷新率。
        self._slow = {}                # 中频慢数据(WMI计数器,4s)
        self._slow_heavy = {}          # 低频重数据(进程列表/句柄/无响应,20s)
        self._slow_time = 0.0
        self._slow_ttl = 4.0           # 中频慢数据后台刷新间隔(秒)
        self._slow_lock = threading.Lock()
        if psutil:
            try:
                psutil.cpu_percent(None)
                psutil.cpu_times_percent(None)
            except Exception:
                pass
            try:
                # 预热进程 CPU，避免首次采样为 0
                for _p in psutil.process_iter(["cpu_percent"]):
                    pass
            except Exception:
                pass
            try:
                self._net_prev = psutil.net_io_counters()
                self._net_time = time.time()
                self._disk_prev = psutil.disk_io_counters()
                self._disk_time = time.time()
                self._cpu_times_prev = psutil.cpu_times()
            except Exception:
                pass

    # ---- CPU ----
    def _cpu(self, snap):
        cpu = {}
        if psutil:
            try:
                cpu["total"] = psutil.cpu_percent(None)
            except Exception:
                pass
            try:
                cpu["per_core"] = psutil.cpu_percent(None, percpu=True)
            except Exception:
                pass
            try:
                t = psutil.cpu_times_percent(None)
                cpu["user"] = getattr(t, "user", None)
                cpu["system"] = getattr(t, "system", None)
                cpu["idle"] = getattr(t, "idle", None)
                cpu["irq"] = (getattr(t, "interrupt", None) or 0) + (getattr(t, "dpc", None) or 0)
            except Exception:
                pass
            try:
                f = psutil.cpu_freq()
                # psutil 返回 MHz，统一转 Hz
                cpu["freq_current"] = f.current * 1e6 if f and f.current else None
                cpu["freq_min"] = f.min * 1e6 if f and f.min else None
                cpu["freq_max"] = f.max * 1e6 if f and f.max else None
            except Exception:
                pass
            try:
                cpu["count_physical"] = psutil.cpu_count(logical=False)
                cpu["count_logical"] = psutil.cpu_count(logical=True)
            except Exception:
                pass
        # v3.1.7：WMI 部分(基频/队列/中断)改读后台慢缓存，不阻塞主采样循环。
        _sw = self._get_slow("cpu")
        if _sw:
            for k in ("name", "freq_base", "queue_length", "interrupts_per_sec"):
                if k in _sw:
                    cpu[k] = _sw[k]
        cpu.setdefault("name", platform.processor() or None)
        snap["cpu"] = cpu

    # ---- 内存 ----
    def _memory(self, snap):
        mem = {}
        if psutil:
            try:
                vm = psutil.virtual_memory()
                mem["total"] = vm.total
                mem["available"] = vm.available
                mem["used"] = vm.used
                mem["percent"] = vm.percent
            except Exception:
                pass
        # v3.1.7：WMI(commit/内存条)改读后台慢缓存。
        _sw = self._get_slow("memory")
        if _sw:
            for k in ("commit_used", "commit_limit", "pages_per_sec", "cached",
                      "nonpaged_pool", "paged_pool", "freq_mhz", "mem_modules", "mem_capacity"):
                if k in _sw:
                    mem[k] = _sw[k]
        snap["memory"] = mem

    # ---- 硬盘 ----
    def _disk(self, snap):
        disk = {}
        if psutil:
            now = time.time()
            try:
                io = psutil.disk_io_counters()
                if self._disk_prev and now > self._disk_time:
                    dt = now - self._disk_time
                    disk["read_speed"] = (io.read_bytes - self._disk_prev.read_bytes) / dt
                    disk["write_speed"] = (io.write_bytes - self._disk_prev.write_bytes) / dt
                self._disk_prev = io
                self._disk_time = now
            except Exception:
                pass
            try:
                parts = []
                for p in psutil.disk_partitions(all=False):
                    try:
                        u = psutil.disk_usage(p.mountpoint)
                        parts.append({
                            "mount": p.mountpoint, "fstype": p.fstype,
                            "total": u.total, "used": u.used, "free": u.free,
                            "percent": u.percent,
                        })
                    except Exception:
                        continue
                disk["partitions"] = parts
                if parts:
                    disk["percent_free"] = min(
                        (p["free"] / p["total"] * 100.0) for p in parts if p.get("total"))
            except Exception:
                pass
        # v3.1.7：WMI(忙碌度/延迟)改读后台慢缓存。
        _sw = self._get_slow("disk")
        if _sw:
            for k in ("busy_percent", "queue_length", "read_latency_ms", "write_latency_ms"):
                if k in _sw:
                    disk[k] = _sw[k]
        # SMART（缓存 30s）
        if time.time() - self._smart_time > 30:
            try:
                self._smart_cache = collect_smart() or {}
                self._smart_time = time.time()
            except Exception:
                pass
        if self._smart_cache:
            disk["temp"] = self._smart_cache.get("temp")
            disk["power_on_hours"] = self._smart_cache.get("hours")
            disk["wear"] = self._smart_cache.get("wear")
            disk["percent_used"] = self._smart_cache.get("percent_used")
            disk["smart_model"] = self._smart_cache.get("model")
        snap["disk"] = disk

    # ---- GPU ----
    def _gpu(self, snap):
        gpus = collect_gpu()
        g = {"count": len(gpus), "list": gpus}
        if gpus:
            g.update({k: gpus[0].get(k) for k in gpus[0] if k != "processes"})
            g["processes"] = gpus[0].get("processes", [])
        snap["gpu"] = g
        # 诊断：nvidia-smi 是否可用（能读到 GPU 即视为可用）
        snap.setdefault("diagnostics", {})["nvidia_smi_ok"] = bool(gpus)

    # ---- 网络 ----
    def _network(self, snap):
        net = {}
        if psutil:
            now = time.time()
            try:
                io = psutil.net_io_counters()
                if self._net_prev and now > self._net_time:
                    dt = now - self._net_time
                    net["down_speed"] = (io.bytes_recv - self._net_prev.bytes_recv) / dt
                    net["up_speed"] = (io.bytes_sent - self._net_prev.bytes_sent) / dt
                net["down_total"] = io.bytes_recv
                net["up_total"] = io.bytes_sent
                self._net_prev = io
                self._net_time = now
            except Exception:
                pass
            try:
                from collections import Counter
                c = Counter()
                for conn in psutil.net_connections(kind="tcp"):
                    c[conn.status] += 1
                net["tcp_total"] = sum(c.values())
                net["tcp_by_state"] = dict(c)
            except Exception:
                pass
        # v3.1.7：WMI(output_queue)改读后台慢缓存。
        _sw = self._get_slow("network")
        if _sw and "output_queue" in _sw:
            net["output_queue"] = _sw["output_queue"]
        with PING_LOCK:
            net["ping"] = dict(PING_RESULT)
        snap["network"] = net

    # ---- 温度 / 风扇 / 电压 ----
    def _thermal(self, snap):
        lhm = collect_lhm()
        cpu = snap.get("cpu", {})
        gpu = snap.get("gpu", {})
        disk = snap.get("disk", {})
        # CPU 温度：每次采样都重新查 WMI（不做"只读一次缓存"），读取失败置 null，绝不用旧值冒充。
        # 优先取 CPU Package / Tctl(AMD) 封装温度，不钉在单个核心。
        cpu_sensors = lhm.get("cpu_temp_sensors", [])
        cpu_temp, cpu_temp_source = _pick_cpu_temp_sensor(cpu_sensors)
        if cpu_temp is None:
            # LHM 未运行或读不到时，走 WMI ACPI 热区温度兜底
            cpu_temp = collect_cpu_temp_acpi()
            cpu_temp_source = "acpi" if cpu_temp is not None else None
        cpu["temp_package"] = cpu_temp
        cpu["temp_per_core"] = cpu_sensors
        cpu["package_power"] = lhm.get("cpu_power")

        thermal = {
            "cpu": cpu_temp,
            "gpu": gpu.get("temp") if gpu.get("temp") is not None else lhm.get("gpu_temp"),
            "disk": disk.get("temp") if disk.get("temp") is not None else lhm.get("disk_temp"),
            "motherboard": lhm.get("motherboard_temp"),
            "fans": lhm.get("fans", []),
            "voltages": lhm.get("voltages", []),
        }
        snap["thermal"] = thermal
        # 诊断：LibreHardwareMonitor 是否在运行（供前端提示"未检测到"而非干等）
        diag = snap.setdefault("diagnostics", {})
        diag["lhm_connected"] = bool(lhm.get("connected"))
        diag["cpu_temp_source"] = cpu_temp_source
        diag["last_update"] = time.time()

    # ---- 功耗 / 电池 ----
    def _power(self, snap):
        power = {"battery_present": False, "battery_source": None}
        source = None
        # 1) 首选 psutil
        if psutil:
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    power["battery_present"] = True
                    power["battery_percent"] = b.percent
                    power["plugged"] = b.power_plugged
                    secs = getattr(b, "secsleft", None)
                    power["time_left_sec"] = secs if secs and secs > 0 else None
                    if b.power_plugged and b.percent >= 100:
                        power["status"] = "已充满"
                    elif b.power_plugged:
                        power["status"] = "充电中"
                    else:
                        power["status"] = "放电中"
                    source = "psutil"
            except Exception:
                pass
        # 2) psutil 读不到时走 WMI Win32_Battery 兜底（部分笔记本 psutil 返回 None）
        if not power.get("battery_present"):
            wb = collect_battery_wmi()
            if wb:
                power["battery_present"] = True
                power["battery_percent"] = wb.get("percent")
                power["plugged"] = wb.get("plugged")
                power["status"] = wb.get("status")
                power["health"] = wb.get("health")
                source = wb.get("source")
        power["battery_source"] = source
        # 3) 补充健康度/循环次数/充放电功率（root\\WMI 静态数据，30s 缓存）
        if time.time() - self._batt_time > 30:
            try:
                self._batt_cache = collect_battery_extra()
                self._batt_time = time.time()
            except Exception:
                pass
        if power.get("health") is None:
            power["health"] = self._batt_cache.get("health")
        power["cycle"] = self._batt_cache.get("cycle")
        power["charge_power"] = self._batt_cache.get("power")
        snap["power"] = power

    # ---- 系统 / 进程 ----
    def _system(self, snap):
        # v3.1.7：boot/uptime 实时(毫秒级)，进程列表/句柄/负载全部读后台慢缓存。
        sysinfo = {}
        if psutil:
            try:
                boot = psutil.boot_time()
                sysinfo["boot_time"] = boot
                sysinfo["uptime"] = time.time() - boot
            except Exception:
                pass
        _sw = self._get_slow("system")
        if _sw:
            for k in ("process_count", "thread_count", "processes", "handle_count", "load"):
                if k in _sw:
                    sysinfo[k] = _sw[k]
        snap["system"] = sysinfo

    # ---- 游戏帧率（v3.1.4 已永久移除）----
    def _fps(self, snap):
        # 用户要求彻底关闭帧率采集：不再启动 PresentMon、不碰 ETW 会话、
        # 不读 CSV，避免任何由帧率模块引起的桌面刷新/卡顿。
        snap["fps"] = {
            "available": False, "current": None, "avg": None, "low1": None,
            "frametime": None, "game": None, "process": None, "pid": 0,
            "reason": "disabled", "status": "off", "mode": None,
            "diag": "帧率采集功能已移除",
        }

    def _hung(self):
        """无响应进程 PID 集合（带 10s 缓存，避免采样循环频繁调用 tasklist）。"""
        now = time.time()
        if now - self._hung_time > 10:
            try:
                self._hung_cache = _hung_pids()
                self._hung_time = now
            except Exception:
                pass
        return self._hung_cache

    def collect(self):
        snap = {"time": time.time(), "host": self._host_info()}
        self._cpu(snap)
        self._memory(snap)
        self._disk(snap)
        self._gpu(snap)
        self._network(snap)
        self._thermal(snap)
        self._power(snap)
        self._system(snap)
        self._fps(snap)
        # 分区磁盘空间在 system 里复用
        snap["system"]["partitions"] = snap.get("disk", {}).get("partitions", [])
        # 诊断兜底：确保 diagnostics 字段始终存在且各 key 齐备
        _diag = snap.get("diagnostics") or {}
        _diag.setdefault("lhm_connected", False)
        _diag.setdefault("nvidia_smi_ok", False)
        _diag.setdefault("cpu_temp_source", None)
        _diag.setdefault("last_update", snap.get("time"))
        snap["diagnostics"] = _diag
        # 顶层便捷字段
        snap["alerts"] = evaluate_alerts(snap)
        return snap

    def _get_slow(self, key):
        """读取后台慢采集缓存的某个分组（cpu/memory/disk/network/system）。"""
        with self._slow_lock:
            return self._slow.get(key)

    def _host_info(self):
        h = {
            "hostname": socket.gethostname(),
            "platform": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        }
        if psutil:
            try:
                h["cpu_model"] = platform.processor()
            except Exception:
                pass
        return h


# ---------------------------------------------------------------------------
# 进程状态分组（无响应 / 运行中 / 睡眠 / 挂起）
# ---------------------------------------------------------------------------
def _proc_status(pid, ps_status, hung):
    """归一化进程状态：not_responding / suspended / sleeping / running。

    psutil 的 status 取值（不同平台略有差异）：
      - 'running' / 'waking'        → running
      - 'sleeping' / 'idle'         → sleeping
      - 'stopped' / 'tracing_stop'  → suspended
      - 'disk-sleep' / 'paused'     → suspended
      - 'zombie' / 'dead'           → suspended（视作已结束/挂起）
    无响应（Windows Not Responding）优先级最高。
    """
    if pid in hung:
        return "not_responding"
    s = (ps_status or "").lower()
    if s in ("stopped", "tracing_stop", "disk-sleep", "paused", "zombie", "dead"):
        return "suspended"
    if s in ("sleeping", "idle"):
        return "sleeping"
    return "running"


def _hung_pids():
    """检测无响应(Not Responding)进程，返回 PID 集合。

    用 PowerShell 的 Get-Process .Responding 属性（Windows API 检测），
    比 tasklist /v 快且不会被卡死的进程拖慢。
    """
    try:
        out, _ = _run_cmd(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Process | Where-Object { -not $_.Responding }).Id -join ','"],
            timeout=8)
        pids = set()
        for tok in out.replace("\r", "").replace("\n", "").split(","):
            tok = tok.strip()
            if tok.isdigit():
                pids.add(int(tok))
        return pids
    except Exception:
        return set()


def collect_processes(brief=False):
    """采集进程列表。brief=True 只取 pid/name/mem/cpu，跳过 WMI，供帧率点选秒开。"""
    result = {"processes": [], "total": 0, "threads": 0, "handles": 0, "uptime": None}
    if not psutil:
        return result
    hung = {} if brief else _hung_pids()
    procs = []
    total_threads = 0
    try:
        # 更新 CPU 峰值缓存（每 2s 刷一次，避免频繁全量遍历）；brief 模式跳过
        if not brief:
            _update_peak_cache()
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "num_threads", "status"]):
            try:
                info = p.info
                mem = info.get("memory_info")
                pid = info["pid"]
                _cpu_now = info.get("cpu_percent")
                _peak = _PEAK.get(pid)
                if _peak is not None and (_cpu_now is None or _peak > _cpu_now):
                    _peak = max(_peak, _cpu_now or 0)
                elif _cpu_now is not None:
                    _peak = max(_peak or 0, _cpu_now)
                procs.append({
                    "pid": pid, "name": info.get("name") or "",
                    "cpu": _cpu_now,
                    "peak": _peak,
                    "mem": mem.rss if mem else None,
                    "threads": info.get("num_threads"),
                    "status": _proc_status(pid, info.get("status"), hung),
                })
                total_threads += info.get("num_threads") or 0
            except Exception:
                continue
        result["total"] = len(procs)
        if brief:
            # v2.5.10：帧率点选专用。带窗口标题 + GPU 占用标记，按"游戏优先"排序，
            # 让真正的游戏 exe（有独立窗口且占 GPU）顶到前面，ROB 这类无窗口后台进程靠后。
            titles = _window_titles_by_pid()
            gpu_pids = _gpu_process_pids()
            for p in procs:
                low = (p.get("name") or "").lower()
                p["title"] = titles.get(p["pid"], "")
                p["gpu"] = 1 if p["pid"] in gpu_pids else 0
                p["win"] = 1 if p["pid"] in titles else 0
                bg_pattern = ("service.exe", "webview2.exe", "overlay.exe",
                              "assistant.exe", "srcext.exe", "host.exe")
                p["bg"] = 1 if (low in _FPS_BACKGROUND or low in _FPS_LAUNCHERS
                                or low in _FPS_EXCLUDE
                                or low.endswith(bg_pattern)) else 0
            # 排序权重：非后台 > 有窗口且占GPU > 有窗口 > 仅占GPU > 内存。
            # 游戏一般既有独立窗口又占 GPU；ROB/服务/覆盖层等沉底。
            procs.sort(key=lambda x: (
                (x.get("bg") or 0),
                -((x["gpu"] or 0) and (x["win"] or 0)),
                -(x["win"] or 0),
                -(x["gpu"] or 0),
                -(x["mem"] or 0),
            ))
            result["processes"] = [
                {"pid": p["pid"], "name": p["name"], "mem": p["mem"],
                 "cpu": p["cpu"], "title": p["title"],
                 "gpu": p["gpu"], "win": p["win"]}
                for p in procs
            ]
            return result
        result["threads"] = total_threads
        try:
            result["uptime"] = time.time() - psutil.boot_time()
        except Exception:
            pass
        try:
            hc = 0
            w = _get_wmi()
            if w:
                for r in w.Win32_Process():
                    hc += _int(getattr(r, "HandleCount", None)) or 0
            result["handles"] = hc
        except Exception:
            pass
        order = {"not_responding": 0, "running": 1, "sleeping": 2, "suspended": 3}
        procs.sort(key=lambda x: (order.get(x["status"], 4), -(x["cpu"] or 0)))
        result["processes"] = procs
    except Exception:
        pass
    return result


# 进程 CPU 峰值缓存（跨请求共享，最多保留 4000 条）
_PEAK = {}
_PEAK_LOCK = threading.Lock()
_PEAK_LAST = 0.0          # 上次刷新峰值缓存的时间戳


def _update_peak_cache():
    """周期性刷新进程 CPU 峰值缓存：把当前瞬时 CPU 合并进峰值。"""
    global _PEAK_LAST
    now = time.time()
    with _PEAK_LOCK:
        if now - _PEAK_LAST < 2.0:
            return
        _PEAK_LAST = now
    if not psutil:
        return
    try:
        for p in psutil.process_iter(["pid", "cpu_percent"]):
            try:
                info = p.info
                pid = info["pid"]
                v = info.get("cpu_percent")
                if v is None:
                    continue
                with _PEAK_LOCK:
                    if pid not in _PEAK or v > _PEAK[pid]:
                        _PEAK[pid] = v
            except Exception:
                continue
        # 精简缓存，避免无限增长
        if len(_PEAK) > 4000:
            with _PEAK_LOCK:
                for k in list(_PEAK.keys())[:2000]:
                    _PEAK.pop(k, None)
    except Exception:
        pass


def _get_proc_detail(pid):
    """按 PID 查询进程的 exe 路径与命令行（供详情展示），失败返回 None。"""
    if not psutil:
        return None
    try:
        p = psutil.Process(pid)
        info = {"exe": None, "cmdline": None}
        try:
            info["exe"] = p.exe() or None
        except Exception:
            pass
        try:
            cl = p.cmdline() or []
            info["cmdline"] = " ".join(cl) if cl else None
        except Exception:
            pass
        return info
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------
def evaluate_alerts(snap):
    if not CONFIG.get("alerts_enabled", True):
        return []
    th = CONFIG.get("thresholds", {})
    alerts = []
    cpu = snap.get("cpu", {})
    gpu = snap.get("gpu", {})
    mem = snap.get("memory", {})
    disk = snap.get("disk", {})
    power = snap.get("power", {})

    def _warn(cat, msg):
        alerts.append({"level": "warning", "category": cat, "message": msg})

    t = cpu.get("temp_package")
    if t is not None and th.get("cpu_temp") and t >= th["cpu_temp"]:
        _warn("cpu", f"CPU 封装温度过高：{t:.0f}°C")
    t = gpu.get("temp")
    if t is not None and th.get("gpu_temp") and t >= th["gpu_temp"]:
        _warn("gpu", f"GPU 温度过高：{t:.0f}°C")
    t = disk.get("temp")
    if t is not None and th.get("disk_temp") and t >= th["disk_temp"]:
        _warn("disk", f"硬盘温度过高：{t:.0f}°C")
    if cpu.get("total") is not None and th.get("cpu_percent") and cpu["total"] >= th["cpu_percent"]:
        _warn("cpu", f"CPU 使用率过高：{cpu['total']:.0f}%")
    if gpu.get("util") is not None and th.get("gpu_percent") and gpu["util"] >= th["gpu_percent"]:
        _warn("gpu", f"GPU 使用率过高：{gpu['util']:.0f}%")
    if mem.get("percent") is not None and th.get("mem_percent") and mem["percent"] >= th["mem_percent"]:
        _warn("memory", f"内存使用率过高：{mem['percent']:.0f}%")
    for p in disk.get("partitions", []):
        if p.get("percent") is not None and th.get("disk_free_percent") and (100 - p["percent"]) <= th["disk_free_percent"]:
            _warn("disk", f"磁盘 {p['mount']} 空间即将耗尽（剩余 {100 - p['percent']:.0f}%）")
    if power.get("battery_present"):
        bp = power.get("battery_percent")
        if bp is not None and not power.get("plugged") and th.get("battery_low") and bp <= th["battery_low"]:
            _warn("power", f"电池电量过低：{bp:.0f}%")
    return alerts


# ---------------------------------------------------------------------------
# SQLite 历史
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()


def init_db():
    try:
        with _db_lock:
            con = sqlite3.connect(DB_PATH)
            con.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, data TEXT)")
            con.commit()
            con.close()
    except Exception:
        pass


def _store_snapshot_sync(snap):
    """实际的 sqlite 写入（后台线程执行）。"""
    try:
        with _db_lock:
            con = sqlite3.connect(DB_PATH)
            con.execute("INSERT OR REPLACE INTO samples (ts, data) VALUES (?, ?)",
                        (snap["time"], json.dumps(snap, ensure_ascii=False)))
            keep = CONFIG.get("history_keep", 3600)
            con.execute("DELETE FROM samples WHERE ts < ?", (time.time() - keep,))
            con.commit()
            con.close()
    except Exception:
        pass


# v3.1.7：sqlite 写入偶发卡顿 5 秒(杀软/磁盘IO)，异步化——主循环只入队不阻塞。
_STORE_Q = queue.Queue(maxsize=8)


def store_snapshot(snap):
    try:
        _STORE_Q.put_nowait(snap)
    except queue.Full:
        try:
            _STORE_Q.get_nowait()   # 丢弃最旧的一条，保证实时链路不阻塞
        except Exception:
            pass
        try:
            _STORE_Q.put_nowait(snap)
        except Exception:
            pass


def store_loop():
    """后台写库线程：从队列取快照落库。"""
    while True:
        snap = _STORE_Q.get()
        try:
            _store_snapshot_sync(snap)
        except Exception:
            pass


def extract_path(data, path):
    cur = data
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def query_history(metric, seconds):
    out = []
    try:
        with _db_lock:
            con = sqlite3.connect(DB_PATH)
            cur = con.execute(
                "SELECT ts, data FROM samples WHERE ts >= ? ORDER BY ts ASC",
                (time.time() - seconds,))
            for ts, data in cur.fetchall():
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                v = extract_path(d, metric)
                if v is not None:
                    out.append({"t": ts, "v": v})
            con.close()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# PWA 图标（纯 Python 生成 192x192 PNG）
# ---------------------------------------------------------------------------
def _png_chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return c


def make_icon():
    W = H = 192
    px = bytearray()
    for y in range(H):
        px.append(0)  # filter type 0
        for x in range(W):
            # 深色背景
            r, g, b, a = 17, 24, 39, 255
            # 圆角矩形（显示器轮廓）
            in_body = (16 <= x <= 176 and 44 <= y <= 148)
            in_stand = (86 <= x <= 106 and 148 <= y <= 160)
            in_top = (76 <= x <= 116 and 34 <= y <= 44)
            if in_body:
                r, g, b = 32, 40, 58
                # 屏幕
                if 26 <= x <= 166 and 54 <= y <= 138:
                    r, g, b = 13, 17, 28
            if in_stand:
                r, g, b = 45, 212, 191
            if in_top:
                r, g, b = 45, 212, 191
            # 折线图（红色趋势线）
            if 30 <= x <= 162:
                yy = 120 - int(50 * math.sin((x - 30) / 132.0 * math.pi)) - 10
                if abs(y - yy) <= 2:
                    r, g, b = 255, 107, 107
            # 圆角：四角透明
            corner = 20
            if x < corner and y < corner:
                if (x - corner) ** 2 + (y - corner) ** 2 > corner ** 2:
                    a = 0
            elif x >= W - corner and y < corner:
                if (x - (W - corner)) ** 2 + (y - corner) ** 2 > corner ** 2:
                    a = 0
            elif x < corner and y >= H - corner:
                if (x - corner) ** 2 + (y - (H - corner)) ** 2 > corner ** 2:
                    a = 0
            elif x >= W - corner and y >= H - corner:
                if (x - (W - corner)) ** 2 + (y - (H - corner)) ** 2 > corner ** 2:
                    a = 0
            px += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(px), 9)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b""))


ICON_PNG = make_icon()


# ---------------------------------------------------------------------------
# v3.0.0 驾驶舱：状态扩展 + 电脑端动作
# 只新增，不改动原有采样/SSE/快照字段；取不到的数据一律 None，前端占位不报错。
# ---------------------------------------------------------------------------
_COCKPIT_LOCK = threading.Lock()
_COCKPIT_CACHE = {}


def _cached(key, ttl, producer):
    now = time.time()
    with _COCKPIT_LOCK:
        item = _COCKPIT_CACHE.get(key)
        if item is not None and now - item[0] < ttl:
            return item[1]
    try:
        value = producer()
    except Exception:
        value = None
    with _COCKPIT_LOCK:
        _COCKPIT_CACHE[key] = (now, value)
    return value


def _audio_state():
    """Windows Core Audio：默认输出设备音量(0-100)与静音状态，纯 ctypes COM，无额外依赖。"""
    if os.name != "nt":
        return {"volume": None, "muted": None}

    def produce():
        import ctypes
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = (("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8))

            def __init__(self, text):
                h = text.strip("{}").replace("-", "")
                super(_Guid, self).__init__(
                    int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16),
                    (ctypes.c_ubyte * 8)(*[int(h[i:i + 2], 16) for i in range(16, 32, 2)]))

        ole = ctypes.windll.ole32
        hr = ole.CoInitialize(None)
        enum = dev = endpoint = None
        try:
            cls = _Guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            iid_enum = _Guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            iid_endpoint = _Guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
            enum = ctypes.c_void_p()
            if ole.CoCreateInstance(ctypes.byref(cls), None, 1,
                                    ctypes.byref(iid_enum), ctypes.byref(enum)):
                return {"volume": None, "muted": None}

            def vtable(ptr):
                return ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia)
            fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(vtable(enum)[4])
            dev = ctypes.c_void_p()
            if fn(enum, 0, 1, ctypes.byref(dev)):
                return {"volume": None, "muted": None}
            # IMMDevice::Activate(IAudioEndpointVolume)
            fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_Guid),
                                    ctypes.c_int, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p))(vtable(dev)[3])
            endpoint = ctypes.c_void_p()
            if fn(dev, ctypes.byref(iid_endpoint), 1, None, ctypes.byref(endpoint)):
                return {"volume": None, "muted": None}
            vt = vtable(endpoint)
            # IAudioEndpointVolume::GetMasterVolumeLevelScalar / GetMute
            fn_vol = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_float))(vt[9])
            fn_mute = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_int))(vt[15])
            vol, mute = ctypes.c_float(), ctypes.c_int()
            v_ok = fn_vol(endpoint, ctypes.byref(vol)) == 0
            m_ok = fn_mute(endpoint, ctypes.byref(mute)) == 0
            return {
                "volume": round(vol.value * 100) if v_ok else None,
                "muted": bool(mute.value) if m_ok else None,
            }
        finally:
            # 逆序释放 COM 引用，避免每次请求泄漏
            for ptr in (endpoint, dev, enum):
                if ptr is not None and ptr.value:
                    try:
                        rel = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(
                            vtable(ptr)[2])
                        rel(ptr)
                    except Exception:
                        pass
            if hr == 0:
                try:
                    ole.CoUninitialize()
                except Exception:
                    pass

    return _cached("audio", 1.5, produce)


def _brightness():
    """笔记本屏幕亮度百分比；台式机/无权限返回 None。"""
    def produce():
        if not HAS_WMI:
            return None
        import pythoncom
        pythoncom.CoInitialize()
        try:
            w = _wmi_mod.WMI(namespace="root/wmi")
            row = wmi_first(w.WmiMonitorBrightness())
            return _int(getattr(row, "CurrentBrightness", None)) if row else None
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    return _cached("brightness", 10.0, produce)


def _power_plan():
    """当前电源计划名称，例如「平衡」。"""
    def produce():
        out, _ = _run_cmd(["powercfg", "/getactivescheme"], timeout=3)
        m = re.search(r"\(([^)]+)\)", out or "")
        return m.group(1).strip() if m else None

    return _cached("power_plan", 30.0, produce)


_PLAY_PS = (
    "$ErrorActionPreference='Stop'; try { "
    "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
    "[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null; "
    "$op=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync(); "
    "$mgr=[System.WindowsRuntimeSystemExtensions]::AsTask($op).GetAwaiter().GetResult(); "
    "$sess=$mgr.GetCurrentSession(); "
    "if($null -eq $sess){'null'} else { "
    "$p=[System.WindowsRuntimeSystemExtensions]::AsTask($sess.TryGetMediaPropertiesAsync()).GetAwaiter().GetResult(); "
    "$t=[string]$p.Title; $a=[string]$p.Artist; $b=[string]$p.AlbumTitle; "
    "$t=$t -replace '[\\r\\n|]',' '; $a=$a -replace '[\\r\\n|]',' '; $b=$b -replace '[\\r\\n|]',' '; "
    "$t+'|'+$a+'|'+$b+'|'+$sess.GetPlaybackInfo().PlaybackStatus.ToString() } "
    "} catch { 'null' }"
)


def _playing_ps_fallback():
    """PowerShell WinRT 兜底：只有 title/artist/album/status（无封面/进度）。"""
    try:
        out, _ = _run_cmd(["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-Command", _PLAY_PS], timeout=6)
        text = (out or "").strip()
        if not text or text == "null" or text.lower().startswith("err="):
            return None
        parts = text.split("|")
        while len(parts) < 4:
            parts.append("")
        title, artist, album, status = [p.strip() for p in parts[:4]]
        if not title and not artist:
            return None
        return {"title": title or None, "artist": artist or None,
                "album": album or None, "status": status or None}
    except Exception:
        return None


def _winrt_import(module):
    """兼容 winsdk 与 winrt-* 两套命名空间：优先 winsdk，回退 winrt。"""
    last = None
    for base in ("winsdk", "winrt"):
        try:
            mod = __import__(base + "." + module, fromlist=["*"])
            return mod
        except Exception as e:
            last = e
    raise last if last else ImportError(module)


# v3.0.7：在播采集放在独立短命子进程里执行。
# 原因：WinRT async（open_read_async）若在已初始化的 agent 主进程内调用，会因 COM
# 单元/消息泵状态卡死，拖垮 /api/status；独立进程每次干净初始化，实测秒级返回。
_PLAY_CHILD = r"""
import asyncio, base64, json, datetime, sys
try:
    import winrt.windows.media.control as mctl
    from winrt.windows.storage.streams import DataReader
except Exception as e:
    print(json.dumps({"__err__": "import: " + str(e)}))
    sys.exit(0)

async def cover_b64(thumb):
    if thumb is None:
        return None
    try:
        st = await thumb.open_read_async()
        size = int(st.size)
        if size <= 0 or size > 3 * 1024 * 1024:
            return None
        r = DataReader(st)
        await r.load_async(size)
        raw = bytes(r.read_buffer(r.unconsumed_buffer_length))
        try: r.close()
        except Exception: pass
        try: st.close()
        except Exception: pass
        ct = "image/jpeg"
        mime = "image/png" if "png" in ct else "image/jpeg"
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None

async def main():
    try:
        mgr = await mctl.GlobalSystemMediaTransportControlsSessionManager.request_async()
        sess = mgr.get_current_session()
        if sess is None:
            print(json.dumps(None)); return
        props = await sess.try_get_media_properties_async()
        title = (getattr(props, "title", "") or "").strip() or None
        artist = (getattr(props, "artist", "") or "").strip() or None
        album = (getattr(props, "album_title", "") or "").strip() or None
        cover = await cover_b64(getattr(props, "thumbnail", None))
        status = None
        try:
            raw = sess.get_playback_info().playback_status
            nm = getattr(raw, "name", None)
            if nm:
                status = str(nm)
            else:
                val = getattr(raw, "value", raw)
                sv = str(val).strip()
                status = {0:"Closed",1:"Opening",2:"Changed",3:"Stopped",
                          4:"Playing",5:"Paused"}.get(int(sv), sv) if sv.isdigit() else sv.split(".")[-1].strip()
        except Exception:
            pass
        position = duration = None
        try:
            tl = sess.get_timeline_properties()
            def sec(ts):
                if isinstance(ts, datetime.timedelta):
                    return round(ts.total_seconds(), 2)
                if hasattr(ts, "duration"):
                    return round(int(ts.duration) / 10000000.0, 2)
                return None
            position = sec(tl.position)
            duration = sec(tl.end_time)
        except Exception:
            pass
        if not (title or artist or album):
            print(json.dumps(None)); return
        print(json.dumps({"title":title,"artist":artist,"album":album,
                          "status":status,"cover":cover,
                          "position":position,"duration":duration}))
    except Exception as e:
        print(json.dumps({"__err__": str(e)[:200]}))

asyncio.run(main())
"""

_PLAY_CHILD_FAIL = {"__err__": ""}


def _playing_winrt():
    """v3.0.7：独立子进程跑 WinRT 取真实在播（标题/艺人/封面/状态/进度/时长）。失败 None。"""
    if os.name != "nt":
        return None
    try:
        import sys as _sys
        out, _ = _run_cmd([_sys.executable, "-c", _PLAY_CHILD], timeout=8)
        text = (out or "").strip().splitlines()
        if not text:
            return None
        data = json.loads(text[-1])
        if not data or "__err__" in data:
            return None
        # 去掉外层的 __err__ 噪声，正常返回 playing 字典
        return data if (data.get("title") or data.get("artist")) else None
    except Exception:
        return None


_PLAY_COVER_CACHE = {"key": "", "cover": None}

# v3.1.6：歌名+艺人联网查时长（国产播放器不上报时间轴的兜底）
# 缓存：key=(title,artist) -> 时长秒数（None=查不到，避免反复查）
_PLAY_DURATION_CACHE = {}
# 进度估算：记录"当前歌"首次进入播放的时刻与累计播放秒数（处理暂停）
_PLAY_PROGRESS = {"key": "", "status": "", "start_ts": 0.0, "acc": 0.0}


def _lookup_duration(title, artist):
    """用歌名+艺人查网易云音乐，严格匹配后返回时长秒数；查不到返回 None。
    结果按 (title,artist) 永久缓存（进程内），避免每 2 秒重复联网。"""
    key = (str(title or "").strip().lower(), str(artist or "").strip().lower())
    if not key[0]:
        return None
    if key in _PLAY_DURATION_CACHE:
        return _PLAY_DURATION_CACHE[key]
    result = None
    try:
        import urllib.request as _uq, urllib.parse as _up
        q = (key[0] + (" " + key[1] if key[1] else ""))
        url = "https://music.163.com/api/search/get?" + _up.urlencode(
            {"s": q, "type": 1, "offset": 0, "limit": 8})
        req = _uq.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
        ctx = None
        try:
            import ssl as _ssl
            ctx = _ssl._create_unverified_context()
        except Exception:
            pass
        raw = _uq.urlopen(req, timeout=6, context=ctx).read().decode("utf-8", "replace")
        data = json.loads(raw)
        songs = ((data.get("result") or {}).get("songs") or [])
        # 严格匹配：歌名完全一致，艺人至少有交集
        t0 = key[0]
        a0 = key[1]
        for s in songs:
            sn = str(s.get("name") or "").strip().lower()
            artists = [str(x.get("name") or "").strip().lower()
                       for x in (s.get("artists") or [])]
            name_ok = (sn == t0) or (t0 in sn) or (sn in t0)
            artist_ok = (not a0) or any(a0 in a or a in a0 for a in artists)
            dur = _int(s.get("duration"))
            if name_ok and artist_ok and dur and dur > 10000:
                result = round(dur / 1000.0, 1)
                break
    except Exception:
        result = None
    _PLAY_DURATION_CACHE[key] = result
    return result


def _estimate_progress(result):
    """用联网查到的时长 + 检测到播放的时刻，估算进度 position。
    注意：播放器不上报真实位置，这是尽力而为的估算；暂停时冻结，切歌重置。"""
    key = ((result.get("title") or "") + "|" + (result.get("artist") or "")).lower()
    st = str(result.get("status") or "").lower()
    now = time.time()
    playing = ("play" in st)
    g = _PLAY_PROGRESS
    if g["key"] != key:
        # 切歌：重置累计
        g.update(key=key, status=st, start_ts=now if playing else 0.0, acc=0.0)
    elif g["status"] != st:
        # 播放状态变化：从播放转暂停时把已播时间累加，恢复播放时重设起点
        if "play" in str(g["status"]):
            g["acc"] += max(0.0, now - g["start_ts"])
        if playing:
            g["start_ts"] = now
        g["status"] = st
    dur = result.get("duration")
    if not (dur and dur > 0):
        return result
    elapsed = g["acc"] + (now - g["start_ts"] if playing and g["start_ts"] else 0.0)
    result["position"] = round(min(elapsed, dur), 1)
    result["estimated"] = True
    return result


def _playing():
    """v3.0.7 正在播放：WinRT 子进程取真实标题/艺人/封面/状态/进度；失败回退 PowerShell。"""
    def produce():
        if os.name != "nt":
            return None
        result = _playing_winrt()
        if result:
            # 封面 data URL 较大：同一首歌沿用缓存，避免每 2 秒重传 ~20KB base64
            key = (result.get("title") or "") + "|" + (result.get("artist") or "")
            if _PLAY_COVER_CACHE["key"] == key:
                result["cover"] = _PLAY_COVER_CACHE["cover"]
            else:
                _PLAY_COVER_CACHE["key"] = key
                _PLAY_COVER_CACHE["cover"] = result.get("cover")
            # v3.1.6：播放器不上报时间轴时，用歌名+艺人联网查时长并估算进度
            pos = result.get("position")
            du = result.get("duration")
            if not du or du <= 0:
                found = _lookup_duration(result.get("title"), result.get("artist"))
                if found and found > 0:
                    result["duration"] = found
                    result = _estimate_progress(result)
            return result
        return _playing_ps_fallback()

    # 2 秒刷新：进度条 position/duration 实时走动
    return _cached("playing", 2.0, produce)


def _weather():
    """可选天气（wttr.in），30 分钟缓存；失败静默降级 None，不影响任何现有功能。"""
    def produce():
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://wttr.in/?format=j1",
                headers={"User-Agent": "PCMonitor/" + APP_VERSION})
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            cc = (data.get("current_condition") or [{}])[0]
            area = (data.get("nearest_area") or [{}])[0]
            desc = ((cc.get("weatherDesc") or [{}])[0] or {}).get("value")
            city = ((area.get("areaName") or [{}])[0] or {}).get("value")
            return {"temp": _num(cc.get("temp_C")), "desc": desc, "city": city}
        except Exception:
            return None

    now = time.time()
    with _COCKPIT_LOCK:
        item = _COCKPIT_CACHE.get("weather")
        if item is not None and now - item[0] < (1800.0 if item[1] else 300.0):
            return item[1]
    value = produce()
    with _COCKPIT_LOCK:
        _COCKPIT_CACHE["weather"] = (now, value)
    return value


def extended_status():
    """驾驶舱专用 /api/status：复用现有快照 + 新增字段，绝不改动 /api/snapshot。"""
    with SNAPSHOT_LOCK:
        snap = dict(SNAPSHOT)
    cpu = dict(snap.get("cpu") or {})
    cpu["perCore"] = cpu.get("per_core")
    gpu = dict(snap.get("gpu") or {})
    gpu.update({
        "memUsed": gpu.get("vram_used"),
        "memTotal": gpu.get("vram_total"),
        "freq": gpu.get("core_clock"),
        "hot": gpu.get("temp"),
        "fan": gpu.get("fan"),
    })
    mem = dict(snap.get("memory") or {})
    swap = {}
    if psutil:
        try:
            s = psutil.swap_memory()
            swap = {"total": s.total, "used": s.used, "free": s.free}
        except Exception:
            pass
    mem["swapTotal"] = swap.get("total")
    mem["swapUsed"] = swap.get("used")
    mem["speed"] = (mem.get("freq_mhz") or 0) * 1e6 or None
    net = dict(snap.get("network") or {})
    ping = net.get("ping") or {}
    net.update({
        "up": net.get("up_speed"),
        "down": net.get("down_speed"),
        "ping": ping.get("latency_ms") if isinstance(ping, dict) else None,
        "rxTotal": net.get("down_total"),
        "txTotal": net.get("up_total"),
    })
    return {
        **snap,
        "version": APP_VERSION,
        "cpu": cpu,
        "gpu": gpu,
        "mem": mem,
        "net": net,
        "audio": _audio_state(),
        "display": {"brightness": _brightness()},
        "power": {"plan": _power_plan()},
        "uptime": (snap.get("system") or {}).get("uptime"),
        "playing": _playing(),
        "weather": _weather(),
    }


def _media_key(vk):
    import ctypes
    u = ctypes.windll.user32
    u.keybd_event(vk, 0, 0, 0)
    u.keybd_event(vk, 0, 2, 0)   # KEYEVENTF_KEYUP


def _screenshot():
    """v3.1.0：Pillow ImageGrab 优先（可靠），GDI 兜底；存图片目录并打开文件夹。"""
    folder = os.path.join(os.path.expanduser("~"), "Pictures")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        folder = BASE_DIR
    path = os.path.join(folder, "PCMonitor-" + time.strftime("%Y%m%d-%H%M%S") + ".png")
    saved = False
    # 方案 A：Pillow ImageGrab（最可靠，支持多显示器）
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        img.save(path)
        saved = True
    except Exception:
        saved = False
    # 方案 B：GDI 兜底
    if not saved:
        try:
            import win32gui
            import win32ui
            import win32con
            w = win32gui.GetSystemMetrics(0)
            h = win32gui.GetSystemMetrics(1)
            hdc = win32gui.GetDC(0)
            src = win32ui.CreateDCFromHandle(hdc)
            dst = src.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src, w, h)
            try:
                dst.SelectObject(bmp)
                dst.BitBlt((0, 0), (w, h), src, (0, 0), win32con.SRCCOPY)
                bmp.SaveBitmapFile(dst, path)
                saved = True
            finally:
                for obj in (bmp, dst, src):
                    try:
                        obj.DeleteObject() if obj is bmp else obj.DeleteDC()
                    except Exception:
                        pass
                try:
                    win32gui.ReleaseDC(0, hdc)
                except Exception:
                    pass
        except Exception:
            saved = False
    if not saved or not os.path.exists(path):
        return None
    # 打开图片所在文件夹，让用户立刻看到截图（不弹窗阻塞）
    try:
        subprocess.Popen(["explorer.exe", "/select,", path],
                         creationflags=(0x08000000 if os.name == "nt" else 0))
    except Exception:
        pass
    return path


def _launch(path):
    if os.name == "nt":
        os.startfile(path)   # noqa: S606 - 用户从驾驶舱主动点击启动
        return path
    return None


# v3.0.6：驾驶舱方块键动作名 → 编号映射（兼容旧的数字 1-12 下发方式）
_ACTION_ALIASES = {
    "prev": 1, "previous": 1, "上一曲": 1,
    "play_pause": 2, "playpause": 2, "play": 2, "pause": 2, "播放暂停": 2,
    "next": 3, "下一曲": 3,
    "mute": 4, "静音": 4,
    "terminal": 5, "cmd": 5, "wt": 5,
    "vscode": 6, "code": 6,
    "browser": 7, "web": 7,
    "files": 8, "explorer": 8,
    "taskmgr": 9, "taskmanager": 9,
    "screenshot": 10, "snip": 10, "截图": 10,
    "sleep": 11, "息屏": 11,
    "lock": 12, "锁屏": 12,
}


def _resolve_action(code):
    """把动作名（字符串）或编号（数字）统一解析成 1-12；无效返回 None。"""
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code if 1 <= code <= 12 else None
    s = str(code or "").strip().lower()
    if not s:
        return None
    if s in _ACTION_ALIASES:
        return _ACTION_ALIASES[s]
    try:
        n = int(s)
        return n if 1 <= n <= 12 else None
    except Exception:
        return None


def run_action(code):
    """执行驾驶舱动作：接受字符串动作名或数字 1-12，返回 (ok, detail)。

    每个动作独立 try/except（外层兜底），任何异常只影响本次点击，不拖垮采样主循环。
    """
    n = _resolve_action(code)
    if n is None:
        return False, f"无效动作：{code}"
    if os.name != "nt":
        return False, "仅支持 Windows 电脑端"
    try:
        if n == 1:
            _media_key(0xB1)               # VK_MEDIA_PREV_TRACK
            return True, "上一曲"
        if n == 2:
            _media_key(0xB3)               # VK_MEDIA_PLAY_PAUSE
            return True, "播放/暂停"
        if n == 3:
            _media_key(0xB0)               # VK_MEDIA_NEXT_TRACK
            return True, "下一曲"
        if n == 4:
            _media_key(0xAD)               # VK_VOLUME_MUTE
            return True, "静音切换"
        if n == 5:
            wt = shutil_which("wt")
            return True, _launch(wt or "cmd.exe")
        if n == 6:
            code_exe = shutil_which("code") or shutil_which("Code")
            if not code_exe:
                guess = os.path.join(os.path.expanduser("~"), "AppData", "Local",
                                     "Programs", "Microsoft VS Code", "Code.exe")
                code_exe = guess if os.path.exists(guess) else None
            return (True, _launch(code_exe)) if code_exe else (False, "未找到 VS Code")
        if n == 7:
            import webbrowser
            webbrowser.open("about:blank")
            return True, "默认浏览器"
        if n == 8:
            return True, _launch(os.path.expanduser("~"))
        if n == 9:
            subprocess.Popen(["taskmgr.exe"], shell=True)
            return True, "任务管理器"
        if n == 10:
            path = _screenshot()
            return (True, path) if path else (False, "截图失败")
        if n == 11:
            # v3.0.6：sleep = 系统睡眠（SetSuspendState），失败回退到息屏。
            import ctypes
            ok = False
            try:
                # SetSuspendState(hibernate=False, force=False, wakeupEvents=False)
                ok = bool(ctypes.windll.powrprof.SetSuspendState(0, 0, 0))
            except Exception:
                ok = False
            if ok:
                return True, "已睡眠"
            # 回退：关闭显示器（HWND_BROADCAST / WM_SYSCOMMAND / SC_MONITORPOWER / 2）
            ctypes.windll.user32.SendNotifyMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return True, "睡眠未获权限，已息屏（移动鼠标唤醒）"
        if n == 12:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return True, "已锁屏"
        return False, "未知动作编号"
    except Exception as e:
        _log(f"驾驶舱动作 {n} 执行失败：{e}")
        return False, str(e)


def fps_probe(v1=True, seconds=5):
    """v3.1.4：帧率功能已永久移除，探针不再运行 PresentMon（避免系统卡顿）。"""
    return {"v1_metrics": bool(v1), "seconds": seconds, "ok": False,
            "error": "帧率采集功能已移除", "csv_size": 0}

    result = {"v1_metrics": bool(v1), "seconds": seconds, "ok": False}
    exe = _presentmon_exe()
    if not exe:
        result["error"] = "PresentMon.exe not found"
        return result
    result["admin"] = _is_admin()
    probe_dir = _PRESENTMON_DIR
    csv_path = os.path.join(probe_dir, "_probe_v1.csv" if v1 else "_probe_v2.csv")
    err_path = os.path.join(probe_dir, "_probe_err.txt")
    try:
        # 释放会话，避免与常驻实例冲突（探针跑完再由采样循环重建）
        subprocess.run(["logman", "stop", "PCMonitorFPS", "-ets"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    except Exception:
        pass
    try:
        if os.path.exists(csv_path):
            os.remove(csv_path)
    except Exception:
        pass
    cmd = [exe, "--output_file", csv_path,
           "--session_name", "PCMonitorProbeV1" if v1 else "PCMonitorProbeV2",
           "--no_console_stats", "--timed", str(seconds)]
    if v1:
        cmd.append("--v1_metrics")
    try:
        with open(err_path, "wb") as ef:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=ef,
                                 creationflags=(0x08000000 if os.name == "nt" else 0),
                                 cwd=os.path.dirname(exe) or None)
            try:
                rc = p.wait(timeout=seconds + 8)
                result["exit_code"] = rc
            except subprocess.TimeoutExpired:
                p.kill()
                result["exit_code"] = "timeout"
    except Exception as e:
        result["error"] = str(e)
    if os.path.exists(csv_path):
        size = os.path.getsize(csv_path)
        result["csv_size"] = size
        result["ok"] = size > 0
        try:
            with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as cf:
                result["csv_head"] = [next(cf, "").rstrip() for _ in range(3)]
        except Exception:
            pass
    else:
        result["csv_size"] = 0
    try:
        with open(err_path, "rb") as ef:
            raw = ef.read(4000)
        s = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw[:20] \
            else raw.decode("utf-8", errors="replace")
        result["stderr"] = s.replace("\x00", "")[:800]
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "PCMonitor/" + APP_VERSION

    def log_message(self, fmt, *args):
        # 每次请求打印一行日志（来源IP + 方法 + 路径），便于排查 APP 是否连上
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]}  {self.command}  {self.path}")
        except Exception:
            pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _load_dashboard())
        elif path == "/manifest.json":
            self._send(200, "application/manifest+json",
                       json.dumps({
                           "name": "电脑监测",
                           "short_name": "PC监测",
                           "start_url": "/",
                           "display": "standalone",
                           "background_color": "#111827",
                           "theme_color": "#111827",
                           "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}],
                       }, ensure_ascii=False))
        elif path == "/sw.js":
            self._send(200, "application/javascript",
                       "self.addEventListener('fetch', e => {});\n"
                       "self.addEventListener('install', () => self.skipWaiting());\n")
        elif path == "/icon.png":
            self._send(200, "image/png", ICON_PNG)
        elif path == "/api/snapshot":
            self._json(current_snapshot())
        elif path == "/api/status":
            # v3.0.0 驾驶舱专用：现有快照 + 新增字段（不影响 /api/snapshot 与 SSE）
            self._json(extended_status())
        elif path == "/api/history":
            metric = (qs.get("metric") or ["cpu.total"])[0]
            try:
                seconds = float((qs.get("seconds") or ["300"])[0])
            except Exception:
                seconds = 300
            self._json(query_history(metric, seconds))
        elif path == "/api/processes":
            brief = (qs.get("brief") or [""])[0] in ("1", "true", "yes")
            self._json(collect_processes(brief=brief))   # 与进程页共用同一份枚举逻辑
        elif path.startswith("/api/process/"):
            try:
                pid = int(path.rsplit("/", 1)[-1])
            except Exception:
                pid = None
            self._json(_get_proc_detail(pid) or {})
        elif path == "/api/ping":
            self._json({"ok": True, "time": time.time()})
        elif path == "/api/config":
            self._json(get_config())
        elif path == "/api/fps_target":
            self._json(FPS_MONITOR.state())
        elif path == "/api/fps_probe":
            # v3.0.9 诊断：以管理员权限独立跑一次 PresentMon 采集，验证抓帧能力。
            # ?v1=1 用 --v1_metrics，?v1=0 不用；?sec=5 采集秒数。
            v1 = qs.get("v1", ["1"])[0] not in ("0", "false", "no")
            try:
                sec = max(2, min(20, int(qs.get("sec", ["5"])[0])))
            except Exception:
                sec = 5
            self._json(fps_probe(v1=v1, seconds=sec))
        elif path == "/api/stream":
            self._sse()
        else:
            self._send(404, "text/plain; charset=utf-8", "Not Found")

    def do_OPTIONS(self):
        # CORS 预检：允许跨域 GET/POST，避免 fetch 带 Content-Type 时被浏览器拦截
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if path == "/api/config":
            update_config(data)
            save_config_file()
            self._json({"ok": True, "config": get_config()})
        elif path == "/api/fps_target":
            # 状态页点选进程：PID 优先，立即拉起 PresentMon --process_id 并落盘持久化。
            name = data.get("name", data.get("process_name", data.get("proc", "")))
            state = FPS_MONITOR.select_target(
                data.get("pid", 0), name,
                enabled=bool(data.get("enabled", True)))
            save_config_file()
            self._json({"ok": True, "config": get_config(), "fps": state})
        elif path == "/api/action":
            # v3.0.6 驾驶舱：action 支持字符串名(prev/play_pause/.../lock)与数字 1-12，
            # 在电脑端真实执行媒体/启动/截图/睡眠/锁屏；失败返回 {ok:false,reason}。
            act = data.get("action", data.get("code"))
            ok, detail = run_action(act)
            resp = {"ok": bool(ok), "action": act, "detail": detail}
            if not ok:
                resp["reason"] = detail
            self._json(resp)
        else:
            self._send(404, "text/plain; charset=utf-8", "Not Found")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = queue.Queue(maxsize=20)
        with SSE_LOCK:
            SSE_CLIENTS.append(q)
        try:
            q.put_nowait(("data: " + json.dumps(current_snapshot(), ensure_ascii=False) + "\n\n").encode("utf-8"))
        except Exception:
            pass
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                    self.wfile.write(item)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, Exception):
            pass
        finally:
            with SSE_LOCK:
                if q in SSE_CLIENTS:
                    SSE_CLIENTS.remove(q)


def current_snapshot():
    with SNAPSHOT_LOCK:
        return dict(SNAPSHOT)


def get_config():
    with CONFIG_LOCK:
        return dict(CONFIG)


def update_config(data):
    with CONFIG_LOCK:
        if "interval" in data:
            CONFIG["interval"] = max(0.2, float(data["interval"]))
        if "ping_target" in data:
            CONFIG["ping_target"] = str(data["ping_target"])
        if "alerts_enabled" in data:
            CONFIG["alerts_enabled"] = bool(data["alerts_enabled"])
        if "fps_enabled" in data:
            CONFIG["fps_enabled"] = bool(data["fps_enabled"])
            # 通知 FPS 采集器：开启时下次采样拉起 PresentMon，关闭时立即释放进程
            FPS_MONITOR.set_enabled(CONFIG["fps_enabled"])
        if "fps_process_name" in data:
            CONFIG["fps_process_name"] = str(data["fps_process_name"] or "").strip()
            CONFIG["fps_pid"] = 0   # 名字变了，旧 PID 失效
            FPS_MONITOR.reset_target()
        if "fps_pid" in data:
            CONFIG["fps_pid"] = _int(data["fps_pid"]) or 0
            FPS_MONITOR.reset_target()
        if "thresholds" in data and isinstance(data["thresholds"], dict):
            for k, v in data["thresholds"].items():
                if k in CONFIG["thresholds"]:
                    CONFIG["thresholds"][k] = float(v)


def _collect_slow_data(sampler):
    """v3.1.7 后台慢采集：集中执行所有耗时 WMI 查询 + 进程列表遍历 +
    句柄统计 + 无响应检测，一次性写入 sampler._slow。

    这些数据变化慢（几秒级），放在独立线程低频刷新，主采样循环直接读缓存，
    不再被 16 秒的慢操作拖住，interval 参数才真正生效。
    """
    out = {}
    w = _get_wmi()

    # ---- CPU：名称 / 基频 / 队列 / 中断 ----
    cw = {}
    if w:
        try:
            r = wmi_first(w.Win32_Processor())
            if r:
                cw["name"] = getattr(r, "Name", None)
                _base = _num(getattr(r, "MaxClockSpeed", None))
                cw["freq_base"] = _base * 1e6 if _base is not None else None
        except Exception:
            pass
        try:
            r = wmi_first(w.Win32_PerfFormattedData_PerfOS_System())
            if r:
                cw["queue_length"] = _num(getattr(r, "ProcessorQueueLength", None))
        except Exception:
            pass
        try:
            r = wmi_first(w.Win32_PerfFormattedData_PerfOS_Processor(Name="_Total"))
            if r:
                cw["interrupts_per_sec"] = _num(getattr(r, "InterruptsPersec", None))
        except Exception:
            pass
    out["cpu"] = cw

    # ---- 内存：commit / 内存条 ----
    mw = {}
    if w:
        try:
            r = wmi_first(w.Win32_PerfFormattedData_PerfOS_Memory())
            if r:
                mw["commit_used"] = _num(getattr(r, "CommittedBytes", None))
                mw["commit_limit"] = _num(getattr(r, "CommitLimit", None))
                mw["pages_per_sec"] = _num(getattr(r, "PagesPersec", None))
                mw["cached"] = _num(getattr(r, "CacheBytes", None))
                mw["nonpaged_pool"] = _num(getattr(r, "PoolNonpagedBytes", None))
                mw["paged_pool"] = _num(getattr(r, "PoolPagedBytes", None))
        except Exception:
            pass
        try:
            phys = list(w.Win32_PhysicalMemory())
            if phys:
                speeds = [_num(getattr(p, "ConfiguredClockSpeed", None)) or _num(getattr(p, "Speed", None)) for p in phys]
                speeds = [s for s in speeds if s]
                if speeds:
                    mw["freq_mhz"] = int(speeds[0])
                mw["mem_modules"] = len(phys)
                mw["mem_capacity"] = sum(_num(getattr(p, "Capacity", None)) or 0 for p in phys)
        except Exception:
            pass
    out["memory"] = mw

    # ---- 磁盘：忙碌度 / 延迟 ----
    dw = {}
    if w:
        try:
            r = wmi_first(w.Win32_PerfFormattedData_PerfDisk_PhysicalDisk(Name="_Total"))
            if r:
                dw["busy_percent"] = _num(getattr(r, "PercentDiskTime", None))
                dw["queue_length"] = _num(getattr(r, "CurrentDiskQueueLength", None))
                rl = _num(getattr(r, "AvgDisksecPerRead", None))
                wl = _num(getattr(r, "AvgDisksecPerWrite", None))
                dw["read_latency_ms"] = round(rl * 1000, 2) if rl is not None else None
                dw["write_latency_ms"] = round(wl * 1000, 2) if wl is not None else None
        except Exception:
            pass
    out["disk"] = dw

    # ---- 网络：输出队列 ----
    nw = {}
    if w:
        try:
            qlen = 0
            for r in w.Win32_PerfFormattedData_Tcpip_NetworkInterface():
                qlen += _num(getattr(r, "OutputQueueLength", None)) or 0
            nw["output_queue"] = qlen
        except Exception:
            pass
    out["network"] = nw

    # ---- 系统负载(轻量 WMI) ----
    sw = {}
    if w:
        try:
            r = wmi_first(w.Win32_PerfFormattedData_PerfOS_System())
            if r:
                sw["load"] = _num(getattr(r, "ProcessorQueueLength", None))
        except Exception:
            pass
    # 重数据(进程/句柄/无响应)若已采集过则带上，保持字段完整
    with sampler._slow_lock:
        for k in ("process_count", "thread_count", "processes", "handle_count"):
            if k in sampler._slow_heavy:
                sw[k] = sampler._slow_heavy[k]
    out["system"] = sw

    with sampler._slow_lock:
        sampler._slow = out
        sampler._slow_time = time.time()


# v3.1.7：重采集放独立子进程——psutil 遍历 356 进程要 6 秒且持 GIL，
# 放线程里会阻塞实时推送。子进程不占 agent 的 GIL，主循环照常 0.5s 推送。
_HEAVY_CHILD = r"""
import json, sys
try:
    import psutil
except Exception as e:
    print(json.dumps({"__err__": str(e)})); sys.exit(0)
try:
    procs = []
    total_threads = 0
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "num_threads", "status"]):
        try:
            i = p.info
            mem = i.get("memory_info")
            procs.append({"pid": i["pid"], "name": i["name"], "cpu": i.get("cpu_percent"),
                          "mem": mem.rss if mem else None, "threads": i.get("num_threads"),
                          "status": i.get("status")})
            total_threads += i.get("num_threads") or 0
        except Exception:
            continue
    procs.sort(key=lambda x: x["cpu"] or 0, reverse=True)
    hc = 0
    for p in psutil.process_iter(["pid"]):
        try: hc += p.num_handles()
        except Exception: pass
    print(json.dumps({"process_count": len(procs), "thread_count": total_threads,
                      "processes": procs[:15], "handle_count": hc}))
except Exception as e:
    print(json.dumps({"__err__": str(e)[:200]}))
"""


def _collect_heavy_data(sampler):
    """低频重采集：在独立子进程里跑，结果写 sampler._slow_heavy。
    子进程持有自己的 GIL，agent 主循环不受阻塞。"""
    import sys as _sys
    sw = {}
    try:
        out, _ = _run_cmd([_sys.executable, "-c", _HEAVY_CHILD], timeout=25)
        line = (out or "").strip().splitlines()
        if line:
            d = json.loads(line[-1])
            if isinstance(d, dict) and "__err__" not in d:
                sw = d
    except Exception:
        pass
    with sampler._slow_lock:
        sampler._slow_heavy = sw


def slow_loop(sampler):
    """中频慢采集线程：WMI 计数器，每 _slow_ttl 秒刷新。"""
    while True:
        try:
            _collect_slow_data(sampler)
        except Exception:
            pass
        time.sleep(max(1.0, sampler._slow_ttl))


def heavy_loop(sampler):
    """低频重采集线程：进程列表等，每 20 秒刷新，低优先级。"""
    while True:
        try:
            _collect_heavy_data(sampler)
        except Exception:
            pass
        time.sleep(20.0)


def broadcast(snap):
    msg = ("data: " + json.dumps(snap, ensure_ascii=False) + "\n\n").encode("utf-8")
    with SSE_LOCK:
        clients = list(SSE_CLIENTS)
    for q in clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass


# ---------------------------------------------------------------------------
# 采样主循环
# ---------------------------------------------------------------------------
def sampler_loop(sampler):
    while True:
        t0 = time.time()
        try:
            snap = sampler.collect()
            with SNAPSHOT_LOCK:
                SNAPSHOT.clear()
                SNAPSHOT.update(snap)
            broadcast(snap)
            store_snapshot(snap)
        except Exception:
            pass
        interval = CONFIG.get("interval", 1.0)
        elapsed = time.time() - t0
        time.sleep(max(0.05, interval - elapsed))


# ---------------------------------------------------------------------------
# 工具：局域网 IP
# ---------------------------------------------------------------------------
def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            ips.append(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    try:
        if psutil:
            for ifname, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith("127."):
                        if a.address not in ips:
                            ips.append(a.address)
    except Exception:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser(description="电脑监测代理")
    ap.add_argument("--port", type=int, default=None, help="服务端口")
    ap.add_argument("--interval", type=float, default=None, help="采样间隔(秒)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--selfcheck", action="store_true", help="自检环境与采集后退出")
    args = ap.parse_args()

    if psutil is None:
        print("[错误] 缺少 psutil，请先安装： pip install psutil")
        sys.exit(1)

    if args.selfcheck:
        return selfcheck()

    if args.port:
        CONFIG["port"] = args.port
    if args.interval:
        CONFIG["interval"] = max(0.2, args.interval)

    init_db()

    # v3.1.3：设置清晰的控制台标题，任务栏里一眼能认出这是监测服务窗口。
    # 用户关闭该窗口 = 关闭服务，不再有"隐藏后台关不掉"的困惑。
    if os.name == "nt":
        try:
            import ctypes as _ctypes
            _ctypes.windll.kernel32.SetConsoleTitleW("PCMonitor-Agent-RUNNING - close this window to stop")
        except Exception:
            pass

    # v3.1.4：帧率采集已永久移除，不再执行任何 PresentMon / ETW 会话清理
    # （logman query/stop 会触发系统级 trace 操作，用户报告桌面刷新卡顿）。

    # 打印访问信息
    port = CONFIG["port"]
    print("=" * 56)
    print("  电脑监测代理已启动")
    print("=" * 56)
    for ip in lan_ips():
        print(f"  手机浏览器访问:  http://{ip}:{port}")
    print(f"  本机访问:        http://127.0.0.1:{port}")
    print("=" * 56)
    print("  手机 APP 里这样填（设置页）：")
    for ip in lan_ips():
        print(f"      IP 地址:  {ip}      端口:  {port}")
    print("  手机和电脑需连同一个 WiFi。")
    print("  游戏帧率: 已移除（v3.1.4，不启动任何帧率采集工具）")
    print("  按 Ctrl+C 退出。")
    print()

    sampler = Sampler()

    # v3.1.2：预热快照放到服务启动之后的后台线程。
    # 旧版在 serve_forever 之前同步调 sampler.collect()，若某个硬件采集
    # (WMI/LibreHardwareMonitor/磁盘IO) 卡住，HTTP 服务就一直绑不上端口，
    # 手机端连不上、看起来像"服务没启动"。改为先监听再预热，杜绝启动阻塞。
    def _warmup():
        try:
            snap = sampler.collect()
            with SNAPSHOT_LOCK:
                SNAPSHOT.update(snap)
        except Exception:
            pass

    # v3.1.7：后台慢采集线程——集中跑 WMI/进程列表等 16 秒慢操作，
    # 主采样循环只做毫秒级快采集，interval 参数才真正生效。
    threading.Thread(target=heavy_loop, args=(sampler,), daemon=True).start()
    threading.Thread(target=slow_loop, args=(sampler,), daemon=True).start()
    threading.Thread(target=store_loop, daemon=True).start()
    threading.Thread(target=sampler_loop, args=(sampler,), daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()
    threading.Thread(target=_warmup, daemon=True).start()

    try:
        server = ThreadingHTTPServer((args.host, port), Handler)
        server.daemon_threads = True
        server.serve_forever()
    except OSError as e:
        print(f"[错误] 端口 {port} 被占用或无法绑定：{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# 自检（验证环境与采集是否正常）
# ---------------------------------------------------------------------------
def selfcheck():
    print("=" * 52)
    print("  环境自检")
    print("=" * 52)
    print(f"  Python     : {platform.python_version()}")
    print(f"  psutil     : {'OK ' + psutil.__version__ if psutil else '缺失'}")
    print(f"  wmi/pywin32: {'OK' if HAS_WMI else '缺失（部分指标将显示 N/A）'}")
    print(f"  nvidia-smi : {'OK' if shutil_which('nvidia-smi') else '未找到'}")
    _pm = _presentmon_exe()
    if _pm:
        _pm_tag = "已内置" if _pm == _PRESENTMON_EXE_PATH else "已找到"
        print(f"  PresentMon : {_pm}（{_pm_tag}，游戏帧率需管理员权限）")
    else:
        print("  PresentMon : 未找到（首次开启帧率将自动下载；游戏帧率显示 N/A）")
    print(f"  游戏帧率   : {'开启' if CONFIG.get('fps_enabled') else '关闭（默认，设置里可开启）'}"
          f"；管理员权限 {'是' if _is_admin() else '否（帧率无法采集）'}"
          f"；锁定进程 {(CONFIG.get('fps_process_name') or '自动识别')}"
          f"（PID {CONFIG.get('fps_pid') or '未指定'}）")
    print("-" * 52)
    try:
        s = Sampler()
        snap = s.collect()
        d = snap
        print(f"  CPU    : {d['cpu'].get('total') if d['cpu'].get('total') is not None else 'N/A'}% "
              f"({len(d['cpu'].get('per_core', []))} 核心)")
        print(f"  内存   : {d['memory'].get('percent')}% 已用")
        print(f"  硬盘   : {len(d['disk'].get('partitions', []))} 个分区")
        print(f"  GPU    : {d['gpu'].get('name') or 'N/A'}")
        print(f"  网络   : TCP {d['network'].get('tcp_total')} 连接")
        print(f"  电池   : {d['power'].get('battery_percent')}%"
              + (f" 健康 {d['power'].get('health')}%" if d['power'].get('health') else ""))
        print(f"  进程   : {d['system'].get('process_count')} 个")
        _diag = d.get("diagnostics") or {}
        print(f"  诊断   : LibreHardwareMonitor {'运行中' if _diag.get('lhm_connected') else '未检测到'}；"
              f"nvidia-smi {'正常' if _diag.get('nvidia_smi_ok') else '不可用'}"
              f"；CPU温度来源 {_diag.get('cpu_temp_source') or '无'}"
              f"；电池来源 {d['power'].get('battery_source') or '无'}")
        print("=" * 52)
        print("  自检通过，可以启动监测服务。")
    except Exception as e:
        print(f"  [错误] 采集失败：{e}")
        print("=" * 52)
        sys.exit(1)
    return 0


# ---------------------------------------------------------------------------
# 内嵌 Web 控制台（手机 APP 界面）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Web 控制台（优先读取 dashboard.html，便于单独调整界面；缺失时用精简兜底）
# ---------------------------------------------------------------------------
def _load_dashboard():
    # 与手机 APP（Capacitor）共用同一份前端源 www/index.html，
    # 保证 PWA 入口（浏览器访问本服务）与 APK 入口的 UI 完全一致，不再两份文件各改各的。
    candidates = [
        os.path.join(BASE_DIR, "..", "pc-monitor-app", "www", "index.html"),  # 优先：APP 源码（与 APK 打包同一份）
        os.path.join(BASE_DIR, "pc-monitor-app", "www", "index.html"),        # 兼容：从项目根目录运行
        os.path.join(BASE_DIR, "dashboard.html"),                              # 兜底：旧版单页控制台
    ]
    for p in candidates:
        try:
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    return f.read()
        except Exception:
            continue
    return FALLBACK_HTML


FALLBACK_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#0a0e1a;color:#eef1f7;font-family:sans-serif;text-align:center;padding-top:40vh">
<h2>监控服务运行中</h2><p style="color:#9aa5b8">缺少 dashboard.html，请还原该文件后刷新页面。</p></body></html>"""


HTML = _load_dashboard()



if __name__ == "__main__":
    main()
