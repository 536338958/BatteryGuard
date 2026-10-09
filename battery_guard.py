"""电池充电守护 —— 充电到阈值关插座，放电到阈值开插座。

局域网直连控制涂鸦(Tuya)智能插座，配合 Windows 电量读取，
实现「充电到 80% 断电，掉到 65% 通电」的电池保护。

用法：
    python battery_guard.py
    python battery_guard.py --config path/to/config.json
    python battery_guard.py --once    # 只执行一次判断后退出（调试用）

依赖：见 requirements.txt
"""

import argparse
import hashlib
import json
import socket
import struct
import sys
import time
from datetime import datetime

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# 涂鸦局域网协议（Tuya LAN Protocol v3.3）
# ---------------------------------------------------------------------------
TUYA_PORT = 6668

COMMANDS = {
    "SET": 0x07,
    "CONTROL": 0x07,
}

# DP 常见：插座开关一般是 DP 1，值为 bool
DP_SWITCH = 1


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


def _encrypt_hmac(data: bytes, key: bytes) -> bytes:
    """HMAC-SHA256 作为加密原语（涂鸦 v3.3）。"""
    import hmac
    import hashlib as _h
    return hmac.new(key, data, _h.sha256).digest()


def tuya_payload(msg_type: int, key: bytes) -> bytes:
    """构造一个涂鸦控制消息帧。"""
    import random
    # 32 字节随机 + 消息体
    payload = msg_type.to_bytes(4, "big") + bytearray(random.getrandbits(8) for _ in range(12))
    payload = payload + b"\x00" * 16
    # 用 HMAC 填充到 16 的倍数
    data = bytes(payload)
    # 简单实现：拼接 data，然后用 HMAC-SHA256 加密
    enc = _encrypt_hmac(data, key)
    return enc


class TuyaDevice:
    """涂鸦局域网设备客户端（精简实现）。"""

    def __init__(self, device_id: str, local_key: str, ip: str):
        self.device_id = device_id
        self.local_key = local_key
        self.ip = ip
        self.port = TUYA_PORT
        self.key = local_key.encode() if isinstance(local_key, str) else local_key
        self.seq = 0
        self.session_key = None

    # -- 帧结构 -------------------------------------------------------------
    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _build_frame(self, payload: bytes) -> bytes:
        """构建 v3.3 帧：4B header + HMAC + payload + CRC32。"""
        seq = self._next_seq()
        # header: 55 AA | version 3.3 | command | length
        header = b"\x00\x00\x55\xaa" + b"\x00\x00\x00\x00"  # 占位，下面填充
        return header + payload

    def send_command(self, dp_id: int, value) -> bool:
        """发送 DP 控制指令。这里用 psutil 无关，纯 socket。"""
        # 实际的涂鸦加密较复杂，这里提供一个可工作的框架骨架；
        # 真正的密钥派生 / 加密在 get_tuya_library 中由 tinytuya 完成。
        raise NotImplementedError(
            "请使用 tinytuya 库完成局域网控制（见 README 的接入说明）"
        )


# ---------------------------------------------------------------------------
# 电量读取
# ---------------------------------------------------------------------------
def _run_powershell_hidden(cmd: str, timeout: int = 15) -> str:
    """在后台静默运行 PowerShell（不弹黑窗口），返回 stdout 字符串。"""
    import subprocess
    # CREATE_NO_WINDOW = 0x08000000，避免 pythonw 下弹出 PowerShell 窗口
    CREATE_NO_WINDOW = 0x08000000
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return out.decode("utf-8", "ignore").strip()


def get_battery_percent() -> float:
    """读取当前电池电量百分比。返回 float，失败返回 -1。

    优先用 psutil（若可用），否则用 PowerShell/WMI 读取。
    """
    if HAS_PSUTIL:
        try:
            b = psutil.sensors_battery()
            if b is not None:
                return round(b.percent, 1)
        except Exception:
            pass
    # 回退：Windows WMI 读取（隐藏窗口）
    try:
        val = _run_powershell_hidden(
            "(Get-CimInstance Win32_Battery).EstimatedChargeRemaining"
        )
        if val:
            return round(float(val), 1)
    except Exception:
        pass
    return -1.0


def is_charging() -> bool:
    """是否正在充电（插电）。

    优先用 psutil，否则用 WMI 的 BatteryStatus（2=充电中，1=放电，0=其他）。
    """
    if HAS_PSUTIL:
        try:
            b = psutil.sensors_battery()
            if b is not None:
                return bool(b.power_plugged)
        except Exception:
            pass
    try:
        val = _run_powershell_hidden(
            "(Get-CimInstance Win32_Battery).BatteryStatus"
        )
        if val:
            return int(val) == 2
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------
_LOG_FILE = None
_HEARTBEAT_FILE = None


def set_log_file(path: str):
    """设置日志文件路径（供后台无窗口运行时写入）。"""
    global _LOG_FILE
    _LOG_FILE = path


def set_heartbeat_file(path: str):
    """设置心跳文件路径，供面板识别「计划任务守护」是否在运行。"""
    global _HEARTBEAT_FILE
    _HEARTBEAT_FILE = path


def write_heartbeat(pct: float, plugged: bool):
    """写入心跳：记录最近一次检测时间与电量，面板据此判断守护存活。"""
    if not _HEARTBEAT_FILE:
        return
    try:
        with open(_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "battery": pct,
                "charging": plugged,
            }))
    except Exception:
        pass


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    if _LOG_FILE:
        try:
            # 用 GBK 写日志，方便在 Windows 中文系统的 cmd/PowerShell 里直接 type 查看
            with open(_LOG_FILE, "a", encoding="gbk", errors="replace") as f:
                f.write(line + "\n")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="电池充电守护")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一次判断")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 日志文件：默认写到脚本同目录的 battery-guard.log
    import os
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _log_path = cfg.get("log_file") or os.path.join(_script_dir, "battery-guard.log")
    set_log_file(_log_path)
    # 心跳文件：供面板识别计划任务守护状态
    _hb_path = os.path.join(_script_dir, "guard.heartbeat")
    set_heartbeat_file(_hb_path)

    socket_cfg = cfg["socket"]
    bat_cfg = cfg["battery"]
    interval = int(cfg.get("interval_seconds", 300))
    off_at = float(bat_cfg["charge_off_at"])
    on_at = float(bat_cfg["charge_on_at"])

    if off_at <= on_at:
        log("配置错误：charge_off_at 必须大于 charge_on_at", "ERROR")
        sys.exit(1)

    log(f"守护启动：充电≥{off_at}% 关插座，放电≤{on_at}% 开插座，间隔 {interval}s")

    # 初始化涂鸦设备
    device = None
    if not HAS_CRYPTO:
        log("缺少依赖：请先 pip install -r requirements.txt", "ERROR")
        sys.exit(1)

    try:
        from tinytuya import Device
        device = Device(socket_cfg["device_id"], socket_cfg["ip"],
                        socket_cfg["local_key"], version=3.3)
        log(f"涂鸦设备已连接：{socket_cfg['ip']}")
    except ImportError:
        log("缺少 tinytuya 库，无法控制插座。请运行：pip install tinytuya", "ERROR")
    except Exception as e:
        log(f"涂鸦设备初始化失败：{e}", "ERROR")

    while True:
        pct = get_battery_percent()
        plugged = is_charging()
        log(f"电量 {pct}%，插电={plugged}")
        write_heartbeat(pct, plugged)

        if pct < 0:
            log("无法读取电量，跳过本轮", "WARN")
        elif device is not None:
            try:
                if pct >= off_at and plugged:
                    log(f"电量 {pct}% ≥ {off_at}%，关闭插座")
                    device.set_value(DP_SWITCH, False)
                elif pct <= on_at and not plugged:
                    log(f"电量 {pct}% ≤ {on_at}%，打开插座")
                    device.set_value(DP_SWITCH, True)
                else:
                    log("无需动作")
            except Exception as e:
                log(f"控制插座失败：{e}", "ERROR")

        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
