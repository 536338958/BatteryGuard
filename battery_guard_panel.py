"""电池充电守护 —— 图形化控制面板（浏览器版）。

无需 tkinter，用 Python 内置 http.server 启动本地服务，
在浏览器中提供控制面板：启动/停止守护、查询/清空日志、
实时显示电量与插座状态、编辑充电阈值、手动控制插座。

用法：
    python battery_guard_panel.py           # 启动后浏览器打开 http://127.0.0.1:8899
    python battery_guard_panel.py --port 8899
"""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import battery_guard as bg
import battery_health as bh


def _app_dir():
    """返回程序数据目录，兼容源码运行与 PyInstaller 单文件打包两种场景。

    - 源码运行：脚本所在目录
    - 打包成 exe：exe 所在目录（config.json 应放在 exe 同目录）
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = _app_dir()
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "battery-guard.log")

# ---------------------------------------------------------------------------
# 守护状态（全局，供 HTTP 请求读写）
# ---------------------------------------------------------------------------
class GuardState:
    def __init__(self):
        self.running = False
        self.stop_event = threading.Event()
        self.thread = None
        self.device = None
        self.config = None
        self.log_lines = []  # 内存中保留最近日志
        self.lock = threading.Lock()

    def load_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.config = json.load(f)

    def save_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def log(self, msg):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        # 同时写日志文件（GBK）
        try:
            with open(LOG_PATH, "a", encoding="gbk", errors="replace") as f:
                f.write(line + "\n")
        except Exception:
            pass


STATE = GuardState()
_TRAY_REF = None  # 全局托盘图标引用（原生 Win32 实现），防止被 GC 回收
_HTTP_SERVER = None  # 当前 HTTP 服务实例（供重启程序时关闭端口）
_PANEL_PORT = 8899  # 当前面板端口（供状态显示）
_RESTART_REQUESTED = False  # 是否请求了「重启程序」（由主线程退出前拉起新实例）

SESSION_PATH = os.path.join(SCRIPT_DIR, "battery_session_history.json")
SESSION_SAMPLE = 20   # 秒：电源状态采样间隔（用 Win32 API，开销极小）
SESSION_MIN_DUR = 5   # 秒：短于此值的抖动忽略
SESSION_MAX = 200     # 最多保留多少条记录


# ---------------------------------------------------------------------------
# 守护循环
# ---------------------------------------------------------------------------
def guard_loop():
    while not STATE.stop_event.is_set():
        # 每轮重新读取配置：阈值与检测间隔改完立即生效，无需重启守护
        cfg = STATE.config or {}
        bat_cfg = cfg.get("battery", {})
        off_at = float(bat_cfg.get("charge_off_at", 80))
        on_at = float(bat_cfg.get("charge_on_at", 65))
        try:
            interval = max(30, int(cfg.get("interval_seconds", 300)))
        except (TypeError, ValueError):
            interval = 300

        pct = bg.get_battery_percent()
        plugged = bg.is_charging()
        STATE.log(f"电量 {pct}%，插电={plugged}")

        if pct >= 0 and STATE.device is not None:
            try:
                if pct >= off_at and plugged:
                    STATE.log(f"电量 {pct}% ≥ {off_at}%，关闭插座")
                    STATE.device.set_value(bg.DP_SWITCH, False)
                elif pct <= on_at and not plugged:
                    STATE.log(f"电量 {pct}% ≤ {on_at}%，打开插座")
                    STATE.device.set_value(bg.DP_SWITCH, True)
                else:
                    STATE.log("无需动作")
            except Exception as e:
                STATE.log(f"控制插座失败：{e}")

        STATE.stop_event.wait(interval)


# ---------------------------------------------------------------------------
# 充放电分段记录（每次「充电 / 放电 / 满电」状态切换记一条，参考 BatteryMaster）
# ---------------------------------------------------------------------------
def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分钟"


def _classify(st):
    """把电源状态归为 充电 / 放电 / 满电（接电源但未充电）。"""
    if not st or st.get("percent") is None:
        return None
    if st.get("charging"):
        return "充电"
    if st.get("ac_online"):
        return "满电"
    return "放电"


class SessionRecorder:
    """后台采样电源状态，状态切换时把上一段记录下来。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = []
        self.cur = None
        self._load()

    def _load(self):
        try:
            with open(SESSION_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.records = data
        except Exception:
            self.records = []

    def _save(self):
        try:
            with self.lock:
                data = self.records[-SESSION_MAX:]
            with open(SESSION_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def recent(self, n=30):
        with self.lock:
            return list(self.records[-n:])

    def current(self):
        """当前进行中的分段（尚未结束，未写入记录）。"""
        cur = self.cur
        if not cur:
            return None
        return {
            "type": cur.get("type"),
            "since": cur.get("start"),
            "start_pct": cur.get("start_pct"),
            "end_pct": cur.get("end_pct"),
            "dur_text": _fmt_dur(time.time() - cur.get("start_ts", time.time())),
        }

    def clear(self):
        with self.lock:
            self.records = []
        self._save()
        return True

    def _begin(self, stype, st, info):
        self.cur = {
            "type": stype,
            "start_ts": time.time(),
            "start": datetime.now().strftime("%m-%d %H:%M"),
            "start_pct": st.get("percent"),
            "end_pct": st.get("percent"),
            "start_wh": round((info.get("remaining_mwh") or 0) / 1000.0, 2)
            if info.get("ok") and info.get("remaining_mwh") else None,
        }

    def _close(self, st, info):
        cur = self.cur
        self.cur = None
        if not cur:
            return
        dur = time.time() - cur["start_ts"]
        if dur < SESSION_MIN_DUR:
            return
        end_pct = st.get("percent", cur.get("end_pct"))
        end_wh = round((info.get("remaining_mwh") or 0) / 1000.0, 2) \
            if info.get("ok") and info.get("remaining_mwh") else None
        energy = None
        if cur.get("start_wh") is not None and end_wh is not None:
            energy = round(end_wh - cur["start_wh"], 2)
        power = None
        if energy is not None and dur >= 30:
            power = round(energy / (dur / 3600.0), 1)
        rec = {
            "type": cur["type"],
            "start": cur["start"],
            "start_ts": round(cur["start_ts"], 1),
            "end": datetime.now().strftime("%m-%d %H:%M"),
            "dur_sec": int(dur),
            "dur_text": _fmt_dur(dur),
            "start_pct": cur.get("start_pct"),
            "end_pct": end_pct,
            "dpct": round((end_pct or 0) - (cur.get("start_pct") or 0), 1),
            "start_wh": cur.get("start_wh"),
            "end_wh": end_wh,
            "energy_wh": energy,
            "power_w": power,
            "full_wh": info.get("full_wh"),
            "health": info.get("health"),
        }
        with self.lock:
            self.records.append(rec)
        self._save()
        STATE.log(f"记录[{rec['type']}] {rec['dur_text']}，"
                  f"{rec['start_pct']}%→{rec['end_pct']}%"
                  + (f"，{rec['energy_wh']:+.2f}Wh" if rec["energy_wh"] is not None else ""))

    def run(self):
        while True:
            try:
                st = bh.get_power_status()
                stype = _classify(st)
                if stype:
                    if self.cur is None:
                        info = bh.get_health(design_mwh=design_capacity(), force=True)
                        self._begin(stype, st, info)
                    elif self.cur["type"] != stype:
                        info = bh.get_health(design_mwh=design_capacity(), force=True)
                        self._close(st, info)
                        self._begin(stype, st, info)
                    else:
                        self.cur["end_pct"] = st.get("percent")
            except Exception:
                pass
            time.sleep(SESSION_SAMPLE)


RECORDER = SessionRecorder()


def design_capacity():
    """config.json 里配置的设计容量（mWh），0 表示未知。"""
    try:
        return int((STATE.config.get("battery") or {}).get("design_capacity_mwh", 0) or 0)
    except (TypeError, ValueError):
        return 0


def init_device():
    try:
        from tinytuya import Device
        sc = STATE.config["socket"]
        STATE.device = Device(sc["device_id"], sc["ip"], sc["local_key"], version=3.3)
        STATE.log("涂鸦设备已连接：" + sc["ip"])
        return True
    except ImportError:
        STATE.log("缺少 tinytuya 库，插座控制不可用")
        return False
    except Exception as e:
        STATE.log("设备初始化失败：" + str(e))
        return False


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
def build_html():
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>电池充电守护</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Microsoft YaHei", "Segoe UI", sans-serif;
         background: #1e1f22; color: #e0e0e0; min-height: 100vh; }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 24px; }
  .cols { display: flex; gap: 16px; align-items: flex-start; }
  .col { flex: 1 1 0; min-width: 0; }
  @media (max-width: 1040px) { .cols { flex-direction: column; } }
  .hr { height: 1px; background: #3a3b3f; margin: 16px 0; }
  .hist { background: #141517; border-radius: 8px; padding: 6px 10px;
          height: 320px; overflow-y: auto; }
  .hrow { display: flex; align-items: center; gap: 8px; padding: 10px 6px;
          border-bottom: 1px solid #34353a; }
  .hrow:last-child { border-bottom: none; }
  .hrow .c1 { flex: 1.15 1 0; min-width: 0; }
  .hrow .c2 { flex: 0.9 1 0; }
  .hrow .c3 { flex: 0.85 1 0; }
  .hrow .c4 { flex: 0.85 1 0; text-align: right; }
  .hrow .t { font-size: 13px; }
  .hrow .s { font-size: 11px; color: #888; margin-top: 2px; }
  .chg { color: #4caf50; }
  .dis { color: #ff6b6b; }
  .idl { color: #9aa0a6; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 20px; }
  .card { background: #2a2b2e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; color: #aaa; font-weight: normal; margin-bottom: 14px; }
  .status-grid { display: flex; gap: 16px; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 140px; background: #333438; border-radius: 10px; padding: 16px; }
  .stat .label { font-size: 12px; color: #999; }
  .stat .value { font-size: 28px; font-weight: bold; margin-top: 4px; }
  .stat .value.battery { color: #4da3ff; }
  .stat .value.charging { color: #4caf50; }
  .stat .value.off { color: #ff9800; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; }
  .badge.run { background: #1e4620; color: #4caf50; }
  .badge.stop { background: #3a3a3a; color: #aaa; }
  .btn { display: inline-block; padding: 10px 22px; border: none; border-radius: 8px;
         font-size: 14px; cursor: pointer; color: #fff; margin-right: 10px; }
  .btn.start { background: #2e7d32; }
  .btn.stop { background: #c62828; }
  .btn.gray { background: #555; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .row label { font-size: 13px; color: #bbb; }
  .row input { width: 70px; padding: 6px 8px; border-radius: 6px; border: 1px solid #555;
               background: #333; color: #fff; font-size: 14px; }
  .row input.wide { width: 220px; font-family: Consolas, monospace; font-size: 13px; }
  .row input.full { width: 100%; font-family: Consolas, monospace; font-size: 13px; }
  .row .small { font-size: 12px; color: #777; }
  .log { background: #141517; border-radius: 8px; padding: 12px; height: 260px;
         overflow-y: auto; font-family: Consolas, monospace; font-size: 12px;
         line-height: 1.6; white-space: pre-wrap; }
  .log div { color: #9fd3a8; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🔋 电池充电守护</h1>
  <div class="sub">充电到上限自动断电 · 掉到下限自动通电</div>

  <div class="cols">
    <div class="col">

      <div class="card">
        <h2>实时状态</h2>
        <div class="status-grid">
          <div class="stat"><div class="label">当前电量</div><div class="value battery" id="battery">--</div></div>
          <div class="stat"><div class="label">充电状态</div><div class="value charging" id="charging">--</div></div>
          <div class="stat"><div class="label">守护状态</div><div class="value off" id="runstate">--</div></div>
        </div>
        <div class="small" style="margin-top:10px" id="trayinfo">托盘图标：--</div>
      </div>

      <div class="card">
        <h2>控制</h2>
        <div class="row">
          <button class="btn start" id="btnStart" onclick="action('start')">▶ 启动守护</button>
          <button class="btn stop" id="btnStop" onclick="action('stop')">■ 停止守护</button>
          <button class="btn gray" onclick="action('restart')">↻ 重启守护</button>
        </div>
        <div class="row">
          <span class="small">「重启守护」= 按当前配置重连插座并重启守护循环，<b>程序本身不退出</b>，改阈值/设备后用这个。</span>
        </div>
        <div class="row">
          <span class="small">手动控制插座：</span>
          <button class="btn gray" onclick="action('on')">通电</button>
          <button class="btn gray" onclick="action('off')">断电</button>
          <span class="small">（调试用）</span>
        </div>
        <div class="hr"></div>
        <div class="row">
          <label>检测间隔（秒）</label>
          <input type="number" id="intervalSec" min="30" max="86400" step="10">
          <button class="btn gray" onclick="saveInterval()">保存间隔</button>
          <span class="small">默认 300 秒，下一轮检测生效</span>
        </div>
        <div class="row">
          <label>面板端口</label>
          <input type="number" id="panelPort" min="1024" max="65535">
          <button class="btn gray" onclick="savePort()">保存端口</button>
          <button class="btn gray" onclick="restartApp()">↻ 重启程序</button>
        </div>
        <div class="row">
          <span class="small">「重启程序」= <b>整个程序退出并重新拉起</b>（新进程），改端口等设置后用这个。</span>
        </div>
      </div>

      <div class="card">
        <h2>充电阈值（%）</h2>
        <div class="row">
          <label>充电上限（达到断电）</label>
          <input type="number" id="offAt" min="20" max="100">
          <label>放电下限（达到通电）</label>
          <input type="number" id="onAt" min="20" max="100">
          <button class="btn gray" onclick="saveThreshold()">保存阈值</button>
        </div>
        <div class="hr"></div>
        <div class="row">
          <button class="btn start" onclick="applyPreset('care')">保养模式 65%~80%</button>
          <button class="btn gray" onclick="applyPreset('calib')">校准模式 20%~100%</button>
        </div>
        <div class="row">
          <span class="small">保存后立即生效。保养模式=日常浅充浅放，对锂电池最友好；校准模式=偶尔做一次，用于校正电量显示。</span>
        </div>
      </div>

      <div class="card">
        <h2>设备配置（涂鸦插座）</h2>
        <div class="row">
          <label style="width:80px">device_id</label>
          <input class="full" id="deviceId" placeholder="设备 ID">
        </div>
        <div class="row">
          <label style="width:80px">local_key</label>
          <input class="full" id="localKey" placeholder="本地密钥">
        </div>
        <div class="row">
          <label style="width:80px">IP 地址</label>
          <input class="wide" id="deviceIp" placeholder="192.168.1.x">
          <button class="btn gray" onclick="saveDevice()">保存设备配置</button>
        </div>
        <div class="row">
          <span class="small">修改后需重启守护生效（或重新连接设备）。</span>
        </div>
      </div>

    </div>

    <div class="col">

      <div class="card">
        <h2>电池健康</h2>
        <div class="status-grid">
          <div class="stat"><div class="label">健康度</div><div class="value battery" id="healthPct">--</div></div>
          <div class="stat"><div class="label">磨损率</div><div class="value" id="wearPct">--</div></div>
          <div class="stat"><div class="label">满充容量</div><div class="value" id="fullWh">--</div></div>
        </div>
        <div class="small" style="margin-top:10px" id="healthDetail">--</div>
        <div class="row" style="margin-top:12px">
          <label>设计容量(mWh)</label>
          <input type="number" id="designMwh" min="0" step="100">
          <button class="btn gray" onclick="saveDesign()">保存设计容量</button>
          <button class="btn gray" onclick="refreshHealth()">立即检测</button>
          <button class="btn gray" onclick="useFullAsDesign()">用满充作基准(新机)</button>
        </div>
        <div class="row">
          <span class="small">本机读不到设计容量，填一次即可自动算健康度（例：46200 = 46.2Wh）</span>
        </div>
        <div class="hr"></div>
        <div class="row">
          <span class="small">充放电记录（每次「充电/放电/满电」切换记一条）</span>
          <button class="btn gray" onclick="clearHistory()">清空记录</button>
        </div>
        <div class="small" id="curSession" style="margin-bottom:8px">当前：--</div>
        <div class="hist" id="healthHistory">暂无记录</div>
      </div>

      <div class="card">
        <h2>运行日志</h2>
        <div class="row">
          <button class="btn gray" onclick="action('log')">查询日志</button>
          <button class="btn gray" onclick="action('clearlog')">清空日志</button>
          <span class="small">日志每 5 秒自动刷新；按钮用于重新载入日志文件 / 清空</span>
        </div>
        <div class="log" id="log"></div>
      </div>

    </div>
  </div>
</div>

<script>
  function action(a) {
    fetch('/api/action?op=' + a).then(r => r.json()).then(d => {
      if (d.error) alert(d.error);
      refresh();
    });
  }
  function saveThreshold() {
    const off = document.getElementById('offAt').value;
    const on = document.getElementById('onAt').value;
    fetch('/api/threshold?off=' + off + '&on=' + on).then(r => r.json()).then(d => {
      if (d.error) alert(d.error);
      refresh();
    });
  }
  let lastHealth = null;
  function renderHealth(d) {
    const h = d.health || {};
    lastHealth = h;
    const hp = document.getElementById('healthPct');
    hp.textContent = (h.health != null) ? h.health + '%' : '未知';
    hp.style.color = (h.health == null) ? '#ff9800' : (h.health >= 80 ? '#4caf50' : (h.health >= 60 ? '#ff9800' : '#f44336'));
    document.getElementById('wearPct').textContent = (h.wear != null) ? h.wear + '%' : '--';
    document.getElementById('fullWh').textContent = (h.full_wh != null) ? h.full_wh + 'Wh' : '--';
    if (h.design_mwh) document.getElementById('designMwh').value = h.design_mwh;
    let t = [];
    if (h.design_wh) t.push('设计容量 ' + h.design_wh + 'Wh');
    if (h.cycle_count) t.push('循环 ' + h.cycle_count + ' 次');
    if (h.discharge_w) t.push('放电 ' + h.discharge_w + 'W');
    if (h.charge_w) t.push('充电 ' + h.charge_w + 'W');
    if (h.voltage_v) t.push('电压 ' + h.voltage_v + 'V');
    if (h.runtime_min) t.push('预计续航 ' + h.runtime_min + ' 分钟');
    if (h.ts) t.push('更新 ' + h.ts);
    if (!h.design_mwh) t.push('未填设计容量，无法计算健康度');
    document.getElementById('healthDetail').textContent = t.join(' · ');
    renderHistory(d.sessions);
    renderCurrent(d.session_current);
  }
  function renderHistory(list) {
    const el = document.getElementById('healthHistory');
    if (!list || !list.length) { el.textContent = '暂无记录'; return; }
    el.innerHTML = list.slice().reverse().map(function (x) {
      const tcls = x.type === '充电' ? 'chg' : (x.type === '放电' ? 'dis' : 'idl');
      const pcls = x.dpct > 0 ? 'chg' : (x.dpct < 0 ? 'dis' : 'idl');
      const ecls = (x.energy_wh || 0) > 0 ? 'chg' : ((x.energy_wh || 0) < 0 ? 'dis' : 'idl');
      const sg = function (v, u) { return (v > 0 ? '+' : '') + v + u; };
      return '<div class="hrow">' +
        '<div class="c1"><div class="t ' + tcls + '">' + x.type + ' ' + (x.dur_text || '') + '</div>' +
          '<div class="s">开始于 ' + (x.start || '--') + '</div></div>' +
        '<div class="c2"><div class="t">' + (x.start_pct != null ? x.start_pct : '--') +
          '→' + (x.end_pct != null ? x.end_pct : '--') + '%</div>' +
          '<div class="s ' + pcls + '">' + sg(x.dpct, '%') + '</div></div>' +
        '<div class="c3"><div class="t">' + (x.full_wh != null ? x.full_wh + 'wh' : '--') + '</div>' +
          '<div class="s">健康度 ' + (x.health != null ? x.health + '%' : '未知') + '</div></div>' +
        '<div class="c4"><div class="t">' + (x.power_w != null ? x.power_w + 'W' : '--') + '</div>' +
          '<div class="s ' + ecls + '">' +
          (x.energy_wh != null ? sg(x.energy_wh, 'wh') : '--') + '</div></div>' +
        '</div>';
    }).join('');
  }
  function renderCurrent(c) {
    const el = document.getElementById('curSession');
    if (!c) { el.textContent = '当前：--'; return; }
    const cls = c.type === '充电' ? '#4caf50' : (c.type === '放电' ? '#ff6b6b' : '#9aa0a6');
    el.innerHTML = '当前：<span style="color:' + cls + '">' + c.type + '</span> ' +
      c.dur_text + '（自 ' + (c.since || '--') + '，' +
      (c.start_pct != null ? c.start_pct : '--') + '%→' +
      (c.end_pct != null ? c.end_pct : '--') + '%）';
  }
  function clearHistory() {
    if (!confirm('清空全部充放电记录？')) return;
    fetch('/api/health?clearhistory=1').then(r => r.json()).then(function () { refresh(); });
  }
  function refreshHealth() {
    fetch('/api/health?refresh=1').then(r => r.json()).then(d => {
      if (d.error) { alert(d.error); } else { renderHealth(d); }
    });
  }
  function saveDesign() {
    const v = document.getElementById('designMwh').value;
    fetch('/api/health?design=' + encodeURIComponent(v)).then(r => r.json()).then(d => {
      if (d.error) { alert(d.error); } else { renderHealth(d); }
    });
  }
  function useFullAsDesign() {
    if (!lastHealth || !lastHealth.full_mwh) { alert('请先点「立即检测」'); return; }
    if (!confirm('把当前满充容量 ' + lastHealth.full_wh + 'Wh 设为设计容量基准？（仅建议新机使用）')) return;
    document.getElementById('designMwh').value = lastHealth.full_mwh;
    saveDesign();
  }
  function saveInterval() {
    const sec = document.getElementById('intervalSec').value;
    fetch('/api/interval?seconds=' + encodeURIComponent(sec)).then(r => r.json()).then(d => {
      if (d.error) { alert(d.error); }
      else { alert('检测间隔已保存：' + d.interval + ' 秒，下一轮检测生效'); }
      refresh();
    });
  }
  function savePort() {
    const p = document.getElementById('panelPort').value;
    fetch('/api/port?port=' + encodeURIComponent(p)).then(r => r.json()).then(d => {
      if (d.error) { alert(d.error); }
      else { alert('端口已保存为 ' + d.port + '，请点「重启程序」生效'); }
      refresh();
    });
  }
  function restartApp() {
    if (!confirm('重启程序？会重新拉起本程序并应用新的端口设置。')) return;
    alert('正在重启，请稍等几秒后刷新本页（端口可能已改变）');
    fetch('/api/action?op=restartapp').catch(function () {});
  }
  function applyPreset(mode) {
    let off, on;
    if (mode === 'care') { off = 80; on = 65; }
    else if (mode === 'calib') { off = 100; on = 20; }
    document.getElementById('offAt').value = off;
    document.getElementById('onAt').value = on;
    fetch('/api/threshold?off=' + off + '&on=' + on).then(r => r.json()).then(d => {
      if (d.error) alert(d.error);
      refresh();
    });
  }
  function saveDevice() {
    const id = document.getElementById('deviceId').value.trim();
    const key = document.getElementById('localKey').value.trim();
    const ip = document.getElementById('deviceIp').value.trim();
    if (!id || !key || !ip) {
      alert('device_id、local_key、IP 都不能为空');
      return;
    }
    fetch('/api/device?device_id=' + encodeURIComponent(id) + '&local_key=' + encodeURIComponent(key) + '&ip=' + encodeURIComponent(ip))
      .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); }
        else { alert('设备配置已保存，请重启守护生效'); }
        refresh();
      });
  }
  function refresh() {
    fetch('/api/status').then(r => r.json()).then(d => {
      document.getElementById('battery').textContent = d.battery >= 0 ? d.battery + '%' : '--';
      document.getElementById('charging').textContent = d.charging ? '充电中' : '未充电';
      // 守护状态（唯一的守护，即面板自身的守护循环）
      document.getElementById('runstate').textContent = d.running ? '运行中' : '已停止';
      document.getElementById('runstate').style.color = d.running ? '#4caf50' : '#ff9800';
      const trayEl = document.getElementById('trayinfo');
      if (d.tray) {
        trayEl.textContent = '托盘图标：已显示（屏幕右下角通知区域，可点 ^ 展开）';
        trayEl.style.color = '#4caf50';
      } else {
        trayEl.textContent = '托盘图标：未显示';
        trayEl.style.color = '#ff9800';
      }
      document.getElementById('offAt').value = d.off_at;
      document.getElementById('onAt').value = d.on_at;
      // 运行参数：检测间隔 / 面板端口
      if (d.interval) document.getElementById('intervalSec').value = d.interval;
      if (d.port) document.getElementById('panelPort').value = d.port;
      // 电池健康度（服务端有缓存，不会每次都打 WMI）
      renderHealth({ health: d.health, sessions: d.sessions, session_current: d.session_current });
      // 设备配置
      if (d.device_id) document.getElementById('deviceId').value = d.device_id;
      if (d.local_key) document.getElementById('localKey').value = d.local_key;
      if (d.ip) document.getElementById('deviceIp').value = d.ip;
      document.getElementById('btnStart').disabled = d.running;
      document.getElementById('btnStop').disabled = !d.running;
      const logEl = document.getElementById('log');
      logEl.innerHTML = '';
      d.log.forEach(l => {
        const div = document.createElement('div');
        div.textContent = l;
        logEl.appendChild(div);
      });
      logEl.scrollTop = logEl.scrollHeight;
    });
  }
  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>"""


class PanelHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务 + 跳过 getfqdn。

    `HTTPServer.server_bind()` 会调 `socket.getfqdn()`，它需要 `encodings.idna`
    编解码器；PyInstaller 打包后该模块可能缺失，导致启动直接抛
    `LookupError: unknown encoding: idna` 而崩溃。server_name 实际只用于
    极少场合，直接用绑定地址代替即可。
    """

    daemon_threads = True
    block_on_close = False

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默 HTTP 访问日志

    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self):
        data = build_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._send_html()

        if path == "/api/status":
            pct = bg.get_battery_percent()
            plugged = bg.is_charging()
            with STATE.lock:
                logs = list(STATE.log_lines[-50:])
            sc = STATE.config.get("socket", {})
            tray_ok = False
            tray_rect = None
            if _TRAY_REF is not None:
                tray_ok = bool(_TRAY_REF.added)
                try:
                    tray_rect = _TRAY_REF.rect()
                except Exception:
                    tray_rect = None
            return self._send_json({
                "battery": pct,
                "charging": plugged,
                "running": STATE.running,
                "tray": tray_ok,
                "tray_rect": tray_rect,
                "interval": int(STATE.config.get("interval_seconds", 300)),
                "port": _PANEL_PORT,
                "health": bh.get_health(design_mwh=design_capacity()),
                "sessions": RECORDER.recent(30),
                "session_current": RECORDER.current(),
                "off_at": STATE.config["battery"]["charge_off_at"],
                "on_at": STATE.config["battery"]["charge_on_at"],
                "device_id": sc.get("device_id", ""),
                "local_key": sc.get("local_key", ""),
                "ip": sc.get("ip", ""),
                "log": logs,
            })

        if path == "/api/action":
            op = parse_qs(parsed.query).get("op", [""])[0]
            return self._handle_action(op)

        if path == "/api/threshold":
            q = parse_qs(parsed.query)
            return self._handle_threshold(q)

        if path == "/api/interval":
            q = parse_qs(parsed.query)
            return self._handle_interval(q)

        if path == "/api/port":
            q = parse_qs(parsed.query)
            return self._handle_port(q)

        if path == "/api/health":
            q = parse_qs(parsed.query)
            return self._handle_health(q)

        if path == "/api/device":
            q = parse_qs(parsed.query)
            return self._handle_device(q)

        self.send_response(404)
        self.end_headers()

    def _handle_action(self, op):
        try:
            if op == "start":
                if STATE.running:
                    return self._send_json({"error": "守护已在运行"})
                if STATE.device is None:
                    return self._send_json({"error": "设备未连接，无法启动"})
                STATE.stop_event.clear()
                STATE.running = True
                STATE.thread = threading.Thread(target=guard_loop, daemon=True)
                STATE.thread.start()
                STATE.log("守护已启动")
                return self._send_json({"ok": True})

            if op == "stop":
                if not STATE.running:
                    return self._send_json({"error": "守护未运行"})
                STATE.stop_event.set()
                STATE.running = False
                STATE.log("守护已停止")
                return self._send_json({"ok": True})

            if op == "restart":
                # 重启守护：先停再启，并按当前配置重连设备
                if STATE.running:
                    STATE.stop_event.set()
                    if STATE.thread is not None:
                        STATE.thread.join(timeout=10)
                    STATE.running = False
                    STATE.log("守护已停止（准备重启）")
                # 重新加载配置并重连设备
                try:
                    STATE.load_config()
                    init_device()
                except Exception as e:
                    STATE.log(f"重启时重连设备失败：{e}")
                if STATE.device is None:
                    return self._send_json({"error": "设备未连接，重启失败"})
                STATE.stop_event.clear()
                STATE.running = True
                STATE.thread = threading.Thread(target=guard_loop, daemon=True)
                STATE.thread.start()
                STATE.log("守护已重启")
                return self._send_json({"ok": True})

            if op == "log":
                # 从文件读取
                try:
                    with open(LOG_PATH, "r", encoding="gbk", errors="replace") as f:
                        content = f.read()
                    with STATE.lock:
                        STATE.log_lines = content.splitlines()[-500:]
                except Exception:
                    pass
                STATE.log("已加载日志文件")
                return self._send_json({"ok": True})

            if op == "clearlog":
                try:
                    with open(LOG_PATH, "w", encoding="gbk") as f:
                        f.write("")
                    with STATE.lock:
                        STATE.log_lines = []
                except Exception:
                    pass
                STATE.log("日志已清空")
                return self._send_json({"ok": True})

            if op == "restartapp":
                # 重启程序（改端口/改配置后用）：关掉本实例，由主线程拉起新实例
                request_restart()
                return self._send_json({"ok": True})

            if op == "on":
                if STATE.device is None:
                    return self._send_json({"error": "设备未连接"})
                STATE.device.set_value(bg.DP_SWITCH, True)
                STATE.log("手动：插座通电")
                return self._send_json({"ok": True})

            if op == "off":
                if STATE.device is None:
                    return self._send_json({"error": "设备未连接"})
                STATE.device.set_value(bg.DP_SWITCH, False)
                STATE.log("手动：插座断电")
                return self._send_json({"ok": True})

            return self._send_json({"error": "未知操作：" + op})

        except Exception as e:
            return self._send_json({"error": str(e)})

    def _handle_threshold(self, q):
        try:
            off = float(q.get("off", ["0"])[0])
            on = float(q.get("on", ["0"])[0])
            if off <= on:
                return self._send_json({"error": "充电上限必须大于放电下限"})
            if not (20 <= off <= 100 and 20 <= on <= 100):
                return self._send_json({"error": "阈值需在 20~100 之间"})
            STATE.config["battery"]["charge_off_at"] = off
            STATE.config["battery"]["charge_on_at"] = on
            STATE.save_config()
            STATE.log(f"阈值已保存：上限 {off}%，下限 {on}%")
            return self._send_json({"ok": True})
        except ValueError:
            return self._send_json({"error": "请输入有效数字"})

    def _handle_interval(self, q):
        """保存检测间隔（秒），立即生效，无需重启守护。"""
        raw = q.get("seconds", [""])[0].strip()
        try:
            sec = int(float(raw))
        except ValueError:
            return self._send_json({"error": "请输入有效整数（秒）"})
        if sec < 30:
            return self._send_json({"error": "间隔不能小于 30 秒"})
        if sec > 86400:
            return self._send_json({"error": "间隔不能大于 86400 秒"})
        STATE.config["interval_seconds"] = sec
        STATE.save_config()
        STATE.log(f"检测间隔已保存：{sec} 秒（下一轮生效）")
        return self._send_json({"ok": True, "interval": sec})

    def _handle_port(self, q):
        """保存面板端口到 config.json，需重启程序后生效。"""
        raw = q.get("port", [""])[0].strip()
        try:
            port = int(float(raw))
        except ValueError:
            return self._send_json({"error": "请输入有效端口号"})
        if not (1024 <= port <= 65535):
            return self._send_json({"error": "端口需在 1024~65535 之间"})
        STATE.config["panel_port"] = port
        STATE.save_config()
        STATE.log(f"面板端口已保存：{port}（重启程序后生效）")
        return self._send_json({"ok": True, "port": port, "need_restart": True})

    def _handle_health(self, q):
        """电池健康度：?refresh=1 立即重新检测，?design=46200 保存设计容量，
        ?clearhistory=1 清空充放电记录。"""
        if q.get("clearhistory", ["0"])[0] in ("1", "true", "yes"):
            RECORDER.clear()
            STATE.log("充放电记录已清空")
            return self._send_json({"ok": True, "sessions": []})

        raw_design = q.get("design", [""])[0].strip()
        if raw_design:
            try:
                dv = int(float(raw_design))
                if dv < 0:
                    return self._send_json({"error": "设计容量不能为负"})
                STATE.config.setdefault("battery", {})["design_capacity_mwh"] = dv
                STATE.save_config()
                STATE.log(f"设计容量已保存：{dv} mWh")
            except ValueError:
                return self._send_json({"error": "请输入有效数字（mWh）"})

        refresh = q.get("refresh", ["0"])[0] in ("1", "true", "yes", "on")
        info = bh.get_health(design_mwh=design_capacity(), force=refresh)
        if refresh and info.get("ok"):
            STATE.log(bh.format_summary(info))
        return self._send_json({"ok": True, "health": info,
                                "sessions": RECORDER.recent(30)})

    def _handle_device(self, q):
        """保存设备配置（device_id / local_key / ip）到 config.json。"""
        device_id = q.get("device_id", [""])[0].strip()
        local_key = q.get("local_key", [""])[0].strip()
        ip = q.get("ip", [""])[0].strip()
        if not device_id or not local_key or not ip:
            return self._send_json({"error": "device_id、local_key、IP 都不能为空"})
        STATE.config["socket"] = {
            "device_id": device_id,
            "local_key": local_key,
            "ip": ip,
        }
        STATE.save_config()
        STATE.log(f"设备配置已保存：{ip}（{device_id}）")
        # 尝试重新连接设备
        try:
            from tinytuya import Device
            STATE.device = Device(device_id, ip, local_key, version=3.3)
            STATE.log("已按新配置重新连接设备")
        except Exception as e:
            STATE.log(f"重新连接失败：{e}")
        return self._send_json({"ok": True})


def _spawn_new_instance():
    """以脱离方式拉起一个新实例（它会读取 config.json 里的最新端口）。"""
    try:
        import subprocess
        rest = [a for a in sys.argv[1:] if not a.startswith("--port")]
        if getattr(sys, "frozen", False):
            target = [sys.executable]
        else:
            target = [sys.executable, os.path.abspath(__file__)]
        # 脱离本进程：不继承控制台/管道，父进程退出后新实例照常运行
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        subprocess.Popen(target + rest, cwd=SCRIPT_DIR, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=flags)
        STATE.log("已拉起新实例，本实例即将退出")
        return True
    except Exception as e:
        STATE.log(f"拉起新实例失败：{e}")
        return False


def request_restart():
    """请求重启：停守护、关掉本实例的 HTTP 服务。

    真正的「拉起新实例」由主线程在退出前完成 —— 后台线程会在主线程
    退出时被直接杀掉，交给它拉起会偶发失败。
    """
    global _RESTART_REQUESTED
    _RESTART_REQUESTED = True
    if STATE.running:
        STATE.stop_event.set()
    if _HTTP_SERVER is not None:
        try:
            _HTTP_SERVER.shutdown()
        except Exception:
            pass


def main():
    global _PANEL_PORT, _HTTP_SERVER, _RESTART_REQUESTED
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None,
                        help="面板端口，优先级最高；缺省则用 config.json 的 panel_port（默认 8899）")
    parser.add_argument("--open-browser", action="store_true",
                        help="启动后自动打开控制页（默认不打开，通过托盘双击打开）")
    parser.add_argument("--autostart", action="store_true",
                        help="启动后自动进入守护状态（用于开机自启，静默运行不弹浏览器）")
    args = parser.parse_args()

    # 先加载配置，才能知道用户自定义的端口
    STATE.load_config()

    if args.port:
        port = args.port
    else:
        try:
            port = int(STATE.config.get("panel_port", 8899))
        except (TypeError, ValueError):
            port = 8899
    _PANEL_PORT = port
    url = f"http://127.0.0.1:{port}"

    def _open_browser():
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    # 端口占用检测：若已有实例在运行，不重复起进程（控制页可通过托盘打开）
    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.settimeout(0.5)
    try:
        _probe.connect(("127.0.0.1", port))
        _probe.close()
        print(f"控制面板已在运行：{url}（本次不重复启动）")
        # 已在运行时：手动双击则直接打开控制页；开机自启（--autostart）则静默退出
        if args.open_browser or not args.autostart:
            _open_browser()
        return
    except OSError:
        _probe.close()

    init_device()
    STATE.log(f"控制面板已启动（端口 {port}）")

    # 多线程服务：请求在各自线程里处理，
    # 这样「重启程序」才能在处理请求的线程里安全地关闭服务（单线程会死锁）
    server = PanelHTTPServer(("127.0.0.1", port), Handler)
    _HTTP_SERVER = server
    STATE.config["panel_port"] = port

    # 充放电分段记录（每 20 秒采样一次电源状态，状态切换即落一条）
    threading.Thread(target=RECORDER.run, daemon=True).start()

    # 自动守护：--autostart 或 config 里 auto_start_guard=true 时，启动后自动进入守护循环
    _auto_start = args.autostart or STATE.config.get("auto_start_guard", False)
    if _auto_start and STATE.device is not None:
        STATE.stop_event.clear()
        STATE.running = True
        STATE.thread = threading.Thread(target=guard_loop, daemon=True)
        STATE.thread.start()
        STATE.log("已自动进入守护状态")

    # 启动系统托盘（双击打开控制页，右键菜单可退出）
    _start_tray(url, server)

    # 托盘就绪后弹一次气泡，便于确认程序已进入系统托盘
    if _TRAY_REF is not None and _TRAY_REF.added:
        _TRAY_REF.notify("已在后台运行\n双击托盘图标打开控制面板",
                         title="电池充电守护")

    # --open-browser 时自动打开一次控制页
    if args.open_browser:
        threading.Timer(1.0, _open_browser).start()

    print(f"控制面板已启动：{url}")
    print("程序在系统托盘运行中，双击托盘图标打开控制面板")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if STATE.running:
            STATE.stop_event.set()
        # 先关闭监听套接字，再退出，避免新实例检测到端口仍被占用
        try:
            server.server_close()
        except Exception:
            pass
        if _TRAY_REF is not None:
            _TRAY_REF.stop()
        if _RESTART_REQUESTED:
            _spawn_new_instance()


def _start_tray(url: str, server: HTTPServer):
    """启动系统托盘图标（原生 Win32 Shell_NotifyIcon，不依赖 pystray）。

    - 悬停 tooltip：实时显示电量/充电/守护状态
    - 单击/双击图标：打开控制面板
    - 右键菜单：打开控制面板 / 启动守护 / 停止守护 / 显示状态 / 退出
    """
    global _TRAY_REF
    try:
        import win_tray
    except Exception as e:
        STATE.log(f"托盘模块加载失败：{e}")
        print("托盘模块加载失败：", e)
        return

    ID_OPEN, ID_START, ID_STOP, ID_INFO, ID_QUIT, ID_RESTART, ID_HEALTH = (
        1001, 1002, 1003, 1004, 1005, 1006, 1007)

    def _open():
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def _do_start():
        if STATE.running:
            return "守护已在运行"
        if STATE.device is None:
            return "设备未连接，无法启动"
        STATE.stop_event.clear()
        STATE.running = True
        STATE.thread = threading.Thread(target=guard_loop, daemon=True)
        STATE.thread.start()
        STATE.log("守护已启动（托盘菜单）")
        return "守护已启动"

    def _do_stop():
        if not STATE.running:
            return "守护未运行"
        STATE.stop_event.set()
        STATE.running = False
        STATE.log("守护已停止（托盘菜单）")
        return "守护已停止"

    def _status_text():
        try:
            pct = bg.get_battery_percent()
            plugged = bg.is_charging()
            run = "守护运行中" if STATE.running else "守护已停止"
            text = f"电量 {pct}% | {'充电中' if plugged else '未充电'} | {run}"
            try:
                h = bh.get_health(design_mwh=design_capacity())
                if h.get("ok") and h.get("health") is not None:
                    text += f" | 健康 {h['health']}%"
            except Exception:
                pass
            return text
        except Exception as e:
            return f"状态读取失败：{e}"

    def _quit():
        STATE.log("收到退出指令，正在停止…")
        if STATE.running:
            STATE.stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass
        if _TRAY_REF is not None:
            _TRAY_REF.stop()

    def _menu():
        return [
            (ID_OPEN, "打开控制面板"),
            (None, None),
            (ID_STOP if STATE.running else ID_START,
             "停止守护" if STATE.running else "启动守护"),
            (ID_INFO, "显示状态"),
            (ID_HEALTH, "电池健康度"),
            (None, None),
            (ID_RESTART, "重启程序"),
            (ID_QUIT, "退出"),
        ]

    def _on_command(cmd):
        msg = None
        if cmd == ID_OPEN:
            _open()
        elif cmd == ID_START:
            msg = _do_start()
        elif cmd == ID_STOP:
            msg = _do_stop()
        elif cmd == ID_INFO:
            msg = _status_text()
        elif cmd == ID_HEALTH:
            info = bh.get_health(design_mwh=design_capacity(), force=True)
            STATE.log(bh.format_summary(info))
            msg = (bh.format_summary(info) + "\n设计容量可在控制面板填写"
                   if info.get("health") is None else
                   bh.format_summary(info) + f"\n循环 {info.get('cycle_count', 0)} 次")
        elif cmd == ID_RESTART:
            request_restart()
        elif cmd == ID_QUIT:
            _quit()
        if msg and _TRAY_REF is not None:
            _TRAY_REF.notify(msg, title="电池充电守护")

    tray = win_tray.TrayIcon(icon_path=win_tray.find_icon_file(),
                             tooltip="电池充电守护")
    tray.on_click = _open
    tray.on_double_click = _open
    tray.on_command = _on_command
    tray.set_menu_provider(_menu)

    ok = tray.start(timeout=5)
    _TRAY_REF = tray  # 全局强引用，防止被 GC 回收
    if ok:
        STATE.log(f"托盘图标已创建（hwnd={tray.hwnd}）")
        print("托盘图标已创建")
    else:
        STATE.log(f"托盘图标创建失败：{tray.error}")
        print("托盘图标创建失败：", tray.error)

    def _update_tip():
        while True:
            try:
                if _TRAY_REF is not None:
                    _TRAY_REF.update(tip=_status_text())
            except Exception:
                pass
            time.sleep(10)

    threading.Thread(target=_update_tip, daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 无控制台模式下也要留下线索
        try:
            import traceback
            with open(os.path.join(SCRIPT_DIR, "crash.log"), "a", encoding="utf-8") as f:
                f.write(datetime.now().isoformat() + "\n")
                f.write(traceback.format_exc() + "\n\n")
        except Exception:
            pass
        raise
