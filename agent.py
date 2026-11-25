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
import urllib.parse
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
        "cpu_power": None, "battery_power": None,
    }
    w = _get_wmi()
    if w is None:
        return result
    try:
        ns = _wmi_mod.WMI(namespace=r"root\LibreHardwareMonitor")
        sensors = ns.Sensor()
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
        # WMI：基频 / 处理器队列 / 中断
        w = _get_wmi()
        if w:
            try:
                r = wmi_first(w.Win32_Processor())
                if r:
                    cpu["name"] = getattr(r, "Name", None)
                    _base = _num(getattr(r, "MaxClockSpeed", None))  # MHz
                    cpu["freq_base"] = _base * 1e6 if _base is not None else None
            except Exception:
                pass
            try:
                r = wmi_first(w.Win32_PerfFormattedData_PerfOS_System())
                if r:
                    cpu["queue_length"] = _num(getattr(r, "ProcessorQueueLength", None))
            except Exception:
                pass
            try:
                r = wmi_first(w.Win32_PerfFormattedData_PerfOS_Processor(Name="_Total"))
                if r:
                    cpu["interrupts_per_sec"] = _num(getattr(r, "InterruptsPersec", None))
            except Exception:
                pass
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
        w = _get_wmi()
        if w:
            try:
                r = wmi_first(w.Win32_PerfFormattedData_PerfOS_Memory())
                if r:
                    mem["commit_used"] = _num(getattr(r, "CommittedBytes", None))
                    mem["commit_limit"] = _num(getattr(r, "CommitLimit", None))
                    mem["pages_per_sec"] = _num(getattr(r, "PagesPersec", None))
                    mem["cached"] = _num(getattr(r, "CacheBytes", None))
                    mem["nonpaged_pool"] = _num(getattr(r, "PoolNonpagedBytes", None))
                    mem["paged_pool"] = _num(getattr(r, "PoolPagedBytes", None))
            except Exception:
                pass
            try:
                phys = list(w.Win32_PhysicalMemory())
                if phys:
                    speeds = [_num(getattr(p, "ConfiguredClockSpeed", None)) or _num(getattr(p, "Speed", None)) for p in phys]
                    speeds = [s for s in speeds if s]
                    if speeds:
                        mem["freq_mhz"] = int(speeds[0])
                    mem["mem_modules"] = len(phys)
                    mem["mem_capacity"] = sum(_num(getattr(p, "Capacity", None)) or 0 for p in phys)
            except Exception:
                pass
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
        w = _get_wmi()
        if w:
            try:
                r = wmi_first(w.Win32_PerfFormattedData_PerfDisk_PhysicalDisk(Name="_Total"))
                if r:
                    disk["busy_percent"] = _num(getattr(r, "PercentDiskTime", None))
                    disk["queue_length"] = _num(getattr(r, "CurrentDiskQueueLength", None))
                    disk["read_latency_ms"] = _num(getattr(r, "AvgDisksecPerRead", None))
                    disk["write_latency_ms"] = _num(getattr(r, "AvgDisksecPerWrite", None))
                    if disk["read_latency_ms"] is not None:
                        disk["read_latency_ms"] = round(disk["read_latency_ms"] * 1000, 2)
                    if disk["write_latency_ms"] is not None:
                        disk["write_latency_ms"] = round(disk["write_latency_ms"] * 1000, 2)
            except Exception:
                pass
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
        w = _get_wmi()
        if w:
            try:
                qlen = 0
                for r in w.Win32_PerfFormattedData_Tcpip_NetworkInterface():
                    qlen += _num(getattr(r, "OutputQueueLength", None)) or 0
                net["output_queue"] = qlen
            except Exception:
                pass
        with PING_LOCK:
            net["ping"] = dict(PING_RESULT)
        snap["network"] = net

    # ---- 温度 / 风扇 / 电压 ----
    def _thermal(self, snap):
        lhm = collect_lhm()
        cpu = snap.get("cpu", {})
        gpu = snap.get("gpu", {})
        disk = snap.get("disk", {})
        # CPU 封装温度（首选 LHM，其次 WMI ACPI 热区温度）
        cpu_sensors = lhm.get("cpu_temp_sensors", [])
        cpu_temp = None
        for s in cpu_sensors:
            if s["name"].lower() in ("cpu package", "core (tctl/tdie)", "cpu (tctl/tdie)", "tctl", "tdie", "package"):
                cpu_temp = s["value"]
                break
        if cpu_temp is None and cpu_sensors:
            cpu_temp = cpu_sensors[0]["value"]
        if cpu_temp is None:
            cpu_temp = collect_cpu_temp_acpi()
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

    # ---- 功耗 / 电池 ----
    def _power(self, snap):
        power = {"battery_present": False}
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
            except Exception:
                pass
        if time.time() - self._batt_time > 30:
            try:
                self._batt_cache = collect_battery_extra()
                self._batt_time = time.time()
            except Exception:
                pass
        power["health"] = self._batt_cache.get("health")
        power["cycle"] = self._batt_cache.get("cycle")
        charge_power = self._batt_cache.get("power")
        lhm = snap.get("_lhm") or {}
        power["charge_power"] = charge_power if charge_power is not None else lhm.get("battery_power")
        snap["power"] = power

    # ---- 系统 / 进程 ----
    def _system(self, snap):
        sysinfo = {}
        if psutil:
            try:
                boot = psutil.boot_time()
                sysinfo["boot_time"] = boot
                sysinfo["uptime"] = time.time() - boot
            except Exception:
                pass
            try:
                hung = self._hung()
                procs = []
                total_threads = 0
                for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "num_threads", "status"]):
                    try:
                        info = p.info
                        mem = info.get("memory_info")
                        procs.append({
                            "pid": info["pid"], "name": info["name"],
                            "cpu": info.get("cpu_percent"),
                            "mem": mem.rss if mem else None,
                            "threads": info.get("num_threads"),
                            "status": _proc_status(info["pid"], info.get("status"), hung),
                        })
                        total_threads += info.get("num_threads") or 0
                    except Exception:
                        continue
                sysinfo["process_count"] = len(procs)
                sysinfo["thread_count"] = total_threads
                # 按 CPU 排序取前 15（概览用）
                procs.sort(key=lambda x: x["cpu"] or 0, reverse=True)
                sysinfo["processes"] = procs[:15]
            except Exception:
                pass
        w = _get_wmi()
        if w:
            try:
                hc = 0
                for r in w.Win32_Process():
                    hc += _int(getattr(r, "HandleCount", None)) or 0
                sysinfo["handle_count"] = hc
            except Exception:
                pass
            try:
                r = wmi_first(w.Win32_PerfFormattedData_PerfOS_System())
                if r:
                    sysinfo["load"] = _num(getattr(r, "ProcessorQueueLength", None))
            except Exception:
                pass
        # 系统负载用处理器队列长度近似（Windows 无 loadavg）
        snap["system"] = sysinfo

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
        # 分区磁盘空间在 system 里复用
        snap["system"]["partitions"] = snap.get("disk", {}).get("partitions", [])
        # 顶层便捷字段
        snap["alerts"] = evaluate_alerts(snap)
        return snap

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


def collect_processes():
    """采集全量进程（含状态分组、路径、命令行、CPU 峰值），供 /api/processes 使用。"""
    result = {"processes": [], "total": 0, "threads": 0, "handles": 0, "uptime": None}
    if not psutil:
        return result
    hung = _hung_pids()
    procs = []
    total_threads = 0
    try:
        # 更新 CPU 峰值缓存（每 2s 刷一次，避免频繁全量遍历）
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


def store_snapshot(snap):
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
# HTTP 服务
# ---------------------------------------------------------------------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "PCMonitor/2.1.0"

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
        elif path == "/api/history":
            metric = (qs.get("metric") or ["cpu.total"])[0]
            try:
                seconds = float((qs.get("seconds") or ["300"])[0])
            except Exception:
                seconds = 300
            self._json(query_history(metric, seconds))
        elif path == "/api/processes":
            self._json(collect_processes())
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
            self._json({"ok": True, "config": get_config()})
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
        if "thresholds" in data and isinstance(data["thresholds"], dict):
            for k, v in data["thresholds"].items():
                if k in CONFIG["thresholds"]:
                    CONFIG["thresholds"][k] = float(v)


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
    print("  按 Ctrl+C 退出。")
    print()

    sampler = Sampler()

    # 预热一次快照，避免首次访问空白
    try:
        snap = sampler.collect()
        with SNAPSHOT_LOCK:
            SNAPSHOT.update(snap)
    except Exception:
        pass

    threading.Thread(target=sampler_loop, args=(sampler,), daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()

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
    try:
        p = os.path.join(BASE_DIR, "dashboard.html")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return FALLBACK_HTML


FALLBACK_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#0a0e1a;color:#eef1f7;font-family:sans-serif;text-align:center;padding-top:40vh">
<h2>监控服务运行中</h2><p style="color:#9aa5b8">缺少 dashboard.html，请还原该文件后刷新页面。</p></body></html>"""


HTML = _load_dashboard()



if __name__ == "__main__":
    main()
