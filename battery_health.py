"""电池健康度模块（参考 BatteryMaster 的思路，纯 Python 实现）。

无需管理员权限，通过 WMI（root\\WMI 下的电池类）读取：

- `BatteryFullChargedCapacity`：当前满充容量（mWh）→ 健康度的分子
- `BatteryStatus`：剩余容量、充/放电功率、电压、是否充放电
- `BatteryRuntime`：预计续航（秒）
- `BatteryCycleCount` / `BatteryStaticData`：循环次数、设计容量（部分机型不提供）

设计容量若取不到，需要用户在面板里手动填一次（config.json 的
`battery.design_capacity_mwh`），之后即可算出：
    健康度 = 当前满充容量 / 设计容量 × 100%
    磨损率 = 100% − 健康度

另提供「历史记录」：每天最多记一条（日期 / 满充容量 / 健康度），
写入 battery_health_history.json，用于观察电池衰减趋势。

用法（命令行自测）：
    python battery_health.py
"""

import ctypes
import json
import os
import subprocess
from datetime import datetime

CREATE_NO_WINDOW = 0x08000000


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def get_power_status():
    """用 Win32 API 即时读电源状态（微秒级，不启子进程），适合高频采样。

    返回 {ac_online, charging, percent, life_left_sec}；失败返回 None。
    """
    try:
        s = SYSTEM_POWER_STATUS()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        if not ok:
            return None
        pct = s.BatteryLifePercent
        if pct == 255 or (s.BatteryFlag & 128):  # 255=未知，128=无电池
            pct = None
        life = None if s.BatteryLifeTime in (0, 0xFFFFFFFF) else int(s.BatteryLifeTime)
        return {
            "ac_online": s.ACLineStatus == 1,
            "charging": bool(s.BatteryFlag & 8),
            "percent": pct,
            "life_left_sec": life,
        }
    except Exception:
        return None

# 一次 PowerShell 调用把所有能拿的电池数据都取回来（JSON 输出，便于解析）
_PS_SCRIPT = r'''
$ErrorActionPreference = 'SilentlyContinue'
$r = @{}
$f = Get-CimInstance -Namespace root\WMI -ClassName BatteryFullChargedCapacity | Select-Object -First 1
if ($f) { $r['full_mwh'] = [int]$f.FullChargedCapacity }
$s = Get-CimInstance -Namespace root\WMI -ClassName BatteryStatus | Select-Object -First 1
if ($s) {
  $r['remaining_mwh'] = [int]$s.RemainingCapacity
  $r['voltage_mv']    = [int]$s.Voltage
  $r['charge_mw']     = [int]$s.ChargeRate
  $r['discharge_mw']  = [int]$s.DischargeRate
  $r['charging']      = [bool]$s.Charging
  $r['discharging']   = [bool]$s.Discharging
  $r['ac_online']     = [bool]$s.PowerOnline
}
$rt = Get-CimInstance -Namespace root\WMI -ClassName BatteryRuntime | Select-Object -First 1
if ($rt) { $r['runtime_sec'] = [int]$rt.EstimatedRuntime }
$c = Get-CimInstance -Namespace root\WMI -ClassName BatteryCycleCount | Select-Object -First 1
if ($c) { $r['cycle_count'] = [int]$c.CycleCount }
$sd = Get-CimInstance -Namespace root\WMI -ClassName BatteryStaticData | Select-Object -First 1
if ($sd) {
  if ($sd.DesignedCapacity) { $r['design_mwh'] = [int]$sd.DesignedCapacity }
  if ($sd.CycleCount) { $r['cycle_count'] = [int]$sd.CycleCount }
}
$b = Get-CimInstance Win32_Battery | Select-Object -First 1
if ($b) {
  if ($b.DesignCapacity) { $r['design_mwh'] = [int]$b.DesignCapacity }
  $r['name'] = [string]$b.Name
}
$r | ConvertTo-Json -Compress
'''


def _run_powershell(script: str, timeout: int = 30):
    """隐藏窗口执行 PowerShell，返回 stdout。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return (p.stdout or b"").decode("utf-8", errors="replace")
    except Exception:
        return ""


def query_raw():
    """查询一次电池原始数据（每次都会真实访问 WMI，较慢，请配合缓存使用）。"""
    out = _run_powershell(_PS_SCRIPT)
    data = {}
    if out.strip():
        start = out.find("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(out[start:end + 1])
            except ValueError:
                data = {}
    if not data:
        return {"ok": False, "error": "WMI 未返回电池数据"}
    data["ok"] = True
    return data


_CACHE = {"data": None, "ts": 0.0}
CACHE_TTL = 600  # 秒：自动刷新间隔（面板状态轮询不会重复打 WMI）


def get_health(design_mwh=0, force=False):
    """返回整理后的健康度信息。

    design_mwh：设计容量（mWh）。传 0 表示未知（则不计算健康度）。
    force：True 则忽略缓存，立即重新查询。
    """
    now = datetime.now().timestamp()
    if not force and _CACHE["data"] and (now - _CACHE["ts"]) < CACHE_TTL:
        cached = dict(_CACHE["data"])
        # 设计容量可能刚被用户改过，用最新值重算
        return _finalize(cached, design_mwh)

    raw = query_raw()
    if not raw.get("ok"):
        if _CACHE["data"]:
            return _finalize(dict(_CACHE["data"]), design_mwh, stale=True)
        return {"ok": False, "error": raw.get("error", "未知错误")}

    raw["_ts"] = now
    _CACHE["data"] = raw
    _CACHE["ts"] = now
    return _finalize(raw, design_mwh)


def _finalize(raw, design_mwh, stale=False):
    design = int(design_mwh or 0)
    auto_design = int(raw.get("design_mwh") or 0)
    if design <= 0:
        design = auto_design

    full = int(raw.get("full_mwh") or 0)
    health = None
    wear = None
    if design > 0 and full > 0:
        health = round(full * 100.0 / design, 1)
        wear = round(100.0 - health, 1)

    cycle = raw.get("cycle_count")
    try:
        cycle = int(cycle) if cycle else 0
    except (TypeError, ValueError):
        cycle = 0

    return {
        "ok": True,
        "ts": datetime.fromtimestamp(raw.get("_ts", datetime.now().timestamp())
                                     ).strftime("%Y-%m-%d %H:%M:%S"),
        "stale": stale,
        "name": raw.get("name") or "",
        "design_mwh": design,
        "design_wh": round(design / 1000.0, 1) if design else None,
        "design_auto": auto_design > 0,
        "full_mwh": full,
        "full_wh": round(full / 1000.0, 1) if full else None,
        "remaining_mwh": int(raw.get("remaining_mwh") or 0),
        "health": health,          # 健康度 %
        "wear": wear,              # 磨损率 %
        "cycle_count": cycle,
        "charge_w": round(int(raw.get("charge_mw") or 0) / 1000.0, 1),
        "discharge_w": round(int(raw.get("discharge_mw") or 0) / 1000.0, 1),
        "voltage_v": round(int(raw.get("voltage_mv") or 0) / 1000.0, 2),
        "runtime_min": int(int(raw.get("runtime_sec") or 0) / 60),
        "ac_online": bool(raw.get("ac_online")),
        "charging": bool(raw.get("charging")),
        "discharging": bool(raw.get("discharging")),
    }


# ---------------------------------------------------------------------------
# 历史记录（每天一条，观察衰减趋势）
# ---------------------------------------------------------------------------
def history_path(base_dir):
    return os.path.join(base_dir, "battery_health_history.json")


def load_history(base_dir, limit=60):
    p = history_path(base_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data[-limit:]
    except Exception:
        pass
    return []


def append_history(base_dir, info):
    """写入一条记录；同一天只保留最新一条。"""
    if not info.get("ok") or not info.get("full_mwh"):
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    entry = {
        "date": today,
        "full_mwh": info.get("full_mwh"),
        "full_wh": info.get("full_wh"),
        "design_mwh": info.get("design_mwh"),
        "health": info.get("health"),
        "cycle_count": info.get("cycle_count"),
    }
    try:
        data = load_history(base_dir, limit=100000)
        data = [d for d in data if d.get("date") != today]
        data.append(entry)
        data = sorted(data, key=lambda d: d.get("date", ""))[-365:]
        with open(history_path(base_dir), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def format_summary(info):
    """生成一行适合气泡提示/日志的摘要。"""
    if not info.get("ok"):
        return "电池健康：读取失败（" + str(info.get("error")) + "）"
    parts = []
    if info.get("health") is not None:
        parts.append(f"健康度 {info['health']}%")
        parts.append(f"磨损 {info['wear']}%")
    if info.get("full_wh"):
        parts.append(f"满充 {info['full_wh']}Wh")
    if info.get("design_wh"):
        parts.append(f"设计 {info['design_wh']}Wh")
    if info.get("cycle_count"):
        parts.append(f"循环 {info['cycle_count']} 次")
    return "电池健康：" + ("，".join(parts) if parts else "设计容量未知，请先在面板填写")


if __name__ == "__main__":
    import sys
    base = os.path.dirname(os.path.abspath(__file__))
    design = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    info = get_health(design_mwh=design, force=True)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(format_summary(info))
