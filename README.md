# 🔋 BatteryGuard — 电池充电守护

> 给**不支持官方充放电阈值**的 Windows 笔记本，加一个"充到 80% 自动断电、掉到 65% 自动通电"的外置保护。
> 通过**局域网**控制一台涂鸦（Tuya / 智能生活）智能插座实现，**不需要云端、不需要会员、不需要管理员权限**。

---

## 它解决什么问题

很多轻薄本（尤其是国产/轻薄本）厂商没有提供类似联想"养护模式"、华硕"Battery Health Charging"的充电上限设置。
长期插电会让电池长时间停在 100%，加速老化。

BatteryGuard 的做法很直接：**把充电器插在智能插座上**，用程序监测电量——

- 电量 **≥ 80%** 且正在充电 → 关闭插座（停止充电，笔记本切换到电池供电）
- 电量 **≤ 65%** 且未充电 → 打开插座（恢复充电）

让电池长期工作在 **65%~80%** 这一最舒服的区间，延长寿命。

---

## 特性

| | |
|---|---|
| 🖥 **控制面板** | 浏览器界面，无需安装客户端（内置 HTTP 服务） |
| 🔔 **系统托盘** | 常驻右下角，悬停显示电量/健康度，右键菜单可启停守护 |
| 🔋 **电池健康** | 健康度、磨损率、满充容量、充放电功率、循环次数（参考 BatteryMaster 思路） |
| 📈 **充放电记录** | 每次「充电/放电」切换自动记一条（时长、电量区间、功率、充入放出 Wh） |
| ⚙️ **可调** | 充电上限、放电下限、检测间隔、面板端口，全部可视化修改，即时生效 |
| 🚀 **开机自启** | 一键脚本静默启动，登录后自动进入守护 |
| 📦 **绿色单文件** | 打包好的 `BatteryGuard.exe` 无需安装 Python |
| 🔒 **纯局域网** | 只和插座通信，不上传任何数据；不走云端、不需要外网 |

---

## 硬件准备

1. 一台**涂鸦（Tuya）方案**的智能插座 / 智能排插（市面绝大多数"智能生活""Smart Life"App 控制的插座都是涂鸦方案）
2. 把**笔记本的原装充电器插在这台插座上**
3. 插座和笔记本要在**同一个 Wi-Fi / 局域网**下

> ⚠️ **重要**：请确认插座的功率规格能满足你的充电器（例如 65W 充电器不要接 10A 以下的小插座）。

---

## 快速开始（推荐：直接用 exe）

1. 从 Releases 下载 `BatteryGuard.exe`，放到任意文件夹（例如 `D:\BatteryGuard\`）
2. 在同目录新建 `config.json`（复制 `config.example.json` 改即可）
3. 填入你的插座三参数（见下一节）
4. 双击 `BatteryGuard.exe` → 右下角出现绿色电池图标，并弹出一次气泡提示
5. 单击/双击托盘图标，或右键 →「打开控制面板」，进入 http://127.0.0.1:8899

### 源码运行

```bash
pip install -r requirements.txt
python battery_guard.py --once     # 先跑一轮，确认能读到电量、能控制插座
python battery_guard_panel.py      # 启动控制面板
```

---

## 获取插座三参数（最详细的一步）

需要三项信息：`device_id`、`local_key`、`ip`。

### 方法一：涂鸦 IoT 平台（推荐，能同时拿到 device_id 和 local_key）

**1. 注册开发者账号**
打开 <https://iot.tuya.com> → 注册/登录（个人账号即可，免费）。

**2. 创建「云开发」项目**

- 进入 **控制台 → 云开发 → 创建项目**
- 行业随意选（如"智能家居"），开发方式选"自定义开发"，区域按你的 App 所在区域选
  （国内一般用「中国数据中心」，用 Smart Life 国际版则选「Western America」等）
- 创建完成后记下项目的 **Access ID / Access Secret**（后面用不到，但平台会要求）

**3. 关联你的 App 账号（关键步骤）**

- 在项目里找到 **设备 → 关联 App 账号**（或「云开发 → 设备 → 添加设备 → 关联 App 账号」）
- 页面会出现二维码 → 用手机上控制该插座的 App（**智能生活 / Smart Life / 涂鸦智能**）扫码授权
- 授权成功后，你的设备列表就会出现在平台上

**4. 读取 device_id 与 local_key**

- 进入 **云开发 → 设备 → 我的设备**（或「设备管理」）
- 点开你的插座，即可看到：
  - **Device ID** → 就是 `device_id`
  - **Device Secret / Local Key** → 就是 `local_key`

**5. 获取插座的内网 IP**

任选一种：

- 路由器后台（DHCP 客户端列表）里找该设备的 IP
- 手机 App：设备 → 设备信息 / 网络信息里通常显示 IP
- 电脑上 `arp -a` 扫描，或用 `python -m tinytuya scan`

**6. 强烈建议：给插座绑定固定 IP**
在路由器里做「DHCP 静态分配 / 地址保留」（按 MAC 绑定），否则插座重启后 IP 变了，程序就找不到它。

### 方法二：tinytuya 局域网扫描（可拿 device_id 和 IP，拿不到 local_key）

```bash
pip install tinytuya
python -m tinytuya scan
```

会列出局域网内所有涂鸦设备的 `Device ID`、`IP`、`Version`。
`local_key` 仍需用方法一从平台获取。

### 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 平台里看不到设备 | 没做「关联 App 账号」扫码，或项目区域选错 |
| 提示 `设备初始化失败` | `local_key` 填错，或 IP 不通（先 `ping <IP>`） |
| 之前能用，突然不行 | 插座重新配网后 `local_key` 会变；IP 变了也要改 |
| 换了新插座 | 重新走一次上面的流程，或直接在面板「设备配置」里改 |

---

## 配置文件

`config.json`（与 exe 同目录）：

```json
{
  "socket": {
    "device_id": "bf1234567890abcdef",
    "local_key": "0123456789abcdef",
    "ip": "192.168.1.50"
  },
  "battery": {
    "charge_off_at": 80,
    "charge_on_at": 65,
    "design_capacity_mwh": 0
  },
  "interval_seconds": 300,
  "panel_port": 8899,
  "auto_start_guard": true
}
```

| 字段 | 说明 | 默认 |
|---|---|---|
| `socket.device_id` | 插座设备 ID | — |
| `socket.local_key` | 插座本地密钥 | — |
| `socket.ip` | 插座局域网 IP（建议路由器绑定静态 IP） | — |
| `battery.charge_off_at` | 充电上限（达到则断电） | 80 |
| `battery.charge_on_at` | 放电下限（达到则通电） | 65 |
| `battery.design_capacity_mwh` | 设计容量（mWh），用于算健康度；0 = 未知 | 0 |
| `interval_seconds` | 检测间隔（秒），最小 30 | 300 |
| `panel_port` | 控制面板端口 | 8899 |
| `auto_start_guard` | 启动后自动进入守护 | true |

也可以在面板的**设备配置 / 充电阈值 / 运行参数**里直接改，不用手编辑文件。

---

## 界面说明（两列布局）

| 左列 | 右列 |
|---|---|
| 实时状态（电量 / 充电状态 / 守护状态 / 托盘状态） | 电池健康（健康度、磨损率、满充容量、充放电记录） |
| 控制（启动/停止/重启守护、手动通断电、检测间隔、端口、重启程序） | 运行日志（含查询/清空日志按钮） |
| 充电阈值（含保养模式 65~80%、校准模式 20~100%） | |
| 设备配置（device_id / local_key / IP） | |

### 两个「重启」的区别

| 按钮 | 作用 | 程序退出？ | 什么时候用 |
|---|---|---|---|
| **↻ 重启守护** | 按当前配置重连插座并重启守护循环 | 否 | 改了阈值 / 设备配置后 |
| **↻ 重启程序** | 整个程序退出并重新拉起 | 是 | 改了端口等需要重新读取的配置后 |

### 电池策略建议

- **保养模式 65%~80%**：日常使用，锂电池浅充浅放最舒服（推荐）
- **校准模式 20%~100%**：偶尔跑一次，用于校正系统电量显示；**不要长期用**
  （"深充深放对锂电池好"是镍镉电池时代的旧观念）

---

## 开机自启

把 `BatteryGuard-autostart.vbs` 复制到启动文件夹，并保证它与 `BatteryGuard.exe` **同目录**：

- 按 `Win + R`，输入 `shell:startup` 回车
- 粘贴该 vbs（它会以隐藏窗口启动 exe 并自动进入守护）

停止自启：删掉启动文件夹里的 vbs 即可。

---

## 电池健康度说明

数据通过 Windows WMI 读取，**不需要管理员权限**：

| 指标 | 来源 |
|---|---|
| 当前满充容量 | `root\WMI\BatteryFullChargedCapacity` |
| 充/放电功率、电压、剩余容量 | `root\WMI\BatteryStatus` |
| 预计续航 | `root\WMI\BatteryRuntime` |
| 循环次数 | `root\WMI\BatteryCycleCount`（多数机型不提供） |
| 设计容量 | `root\WMI\BatteryStaticData` / `Win32_Battery`（**很多机型读不到**） |

- **健康度 = 当前满充容量 ÷ 设计容量 × 100%**
- 读不到设计容量时，健康度显示"未知"，在面板里**手动填一次**即可
  （可从 HWiNFO / BatteryMaster / 机身规格查到；新机也可用面板上的「用满充作基准」按钮）
- 每次「充电 / 放电 / 满电」状态切换会记一条记录，存 `battery_session_history.json`

> `powercfg /batteryreport` 需要管理员权限，本工具不使用它。

---

## 常见问题

**Q：没有弹浏览器？**
程序默认不弹窗，进入托盘即可。单击/双击托盘图标打开面板。

**Q：双击托盘图标开了两个页面？**
已修复：单击有 0.35 秒防抖，双击只会打开一个页面。

**Q：端口 8899 被占用？**
在面板「运行参数 → 面板端口」改成别的（如 9000），点「重启程序」生效。

**Q：笔记本合盖/休眠后还会守护吗？**
休眠时程序会暂停。建议在电源设置里把"合盖"设为"不采取任何操作"。

**Q：断电后笔记本靠电池运行，还能控制插座吗？**
能。控制走局域网直连，只要还连着同一个 Wi-Fi 就行。

**Q：程序退出时插座会保持原状态吗？**
是的，会保持最后一次的状态（可能一直充到 100%）。建议保持程序常驻 + 开机自启。

---

## 隐私与安全

- 程序**只在本机运行**，只和你的插座在局域网内通信，**不会上传任何数据**
- `config.json` 里的 `device_id` / `local_key` 属于**敏感信息**，请勿提交到公开仓库
  （仓库已通过 `.gitignore` 排除 `config.json`、日志和记录文件）
- 发布版本请使用 `config.example.json`

---

## 目录结构

```
BatteryGuard/
├─ battery_guard.py          核心守护脚本（也可命令行单独运行，支持 --once）
├─ battery_guard_panel.py    控制面板主程序（HTTP 服务 + 页面 + 托盘接入）
├─ battery_health.py         电池健康度（WMI 读取 + 充放电分段记录）
├─ win_tray.py               原生 Win32 系统托盘（ctypes，无第三方依赖）
├─ make_icon.py              生成 app.ico / app.png
├─ requirements.txt          依赖
├─ BatteryGuard.spec         PyInstaller 打包配置
├─ start_panel.bat           源码模式启动器
├─ BatteryGuard-autostart.vbs 开机自启脚本
├─ config.example.json       配置模板
└─ app.ico / app.png         图标
```

### 打包 exe

```bash
pip install pyinstaller
pyinstaller BatteryGuard.spec --noconfirm --clean
# 产物在 dist/，把 dist\BatteryGuard.exe 与 config.json 放同目录使用
```

> 打包前请先结束正在运行的 BatteryGuard.exe（否则文件被占用）。

---

## 许可证

MIT License。详见 `LICENSE`。

第三方依赖：`tinytuya`（MIT）、`psutil`（BSD）、`cryptography`（Apache-2.0 / BSD）。

电池健康度的展示思路参考了 [BatteryMaster](https://github.com/topabomb/BatteryMaster)（MIT 风格开源项目）。
