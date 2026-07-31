# UGreenLedPilot

绿联 **DXP4800 Plus** 专用机箱 LED 控制应用，面向飞牛 fnOS。

基于 [miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller) 的 `ugreen_leds_cli` 驱动，所有硬件操作均通过该 CLI 完成，无其他控制路径。

**当前版本：v2.2.0** · [下载 Release](https://github.com/WXFffff666/UGreenLedPilot/releases/tag/v2.2.0)

---

## 功能概览

| 功能 | 说明 |
|------|------|
| 三档模式 | 关闭 / 常亮 / 自动（插盘亮、拔盘灭、活动闪） |
| 模式感知监控 | 常亮/关闭时监控休眠；仅自动模式检测热插拔 |
| 事件驱动热插拔 | Linux netlink uevent，拔插时才重扫盘位 |
| 活动闪烁开关 | 可关闭 IO 轮询，进一步降低 CPU |
| 颜色与亮度 | 每颗 LED 独立调色、调光，8 种预设 |
| 全局亮度 | 一键调节所有 LED |
| 批量操作 | 全部常亮 / 全部自动 / 全部关闭 |
| 手动重映射 | 盘位识别异常时可强制重扫 |
| 状态持久化 | 重启后自动恢复上次模式 |
| 实时推送 | SSE 推送状态，无前端轮询 |
| 效果模式 | 呼吸（breath）、手动闪烁（manual-blink）、跑马灯演示（chase，默认关） |
| 速度感知闪烁 | 自动模式闪烁随磁盘/网络活动速度自适应 |
| 多网口活动 | 聚合多个物理网口活动（过滤虚拟网卡） |
| 盘位校准 | 逐灯识别 + 手动绑定盘位（UI 入口） |
| 安全鉴权 | 单用户登录、CSRF、限流、PBKDF2 密码哈希 |

---

## 适用设备

| 项目 | 值 |
|------|-----|
| 型号 | 绿联 DXP4800 Plus |
| 盘位 | 4（HCTL `0:0:0:0` → `3:0:0:0`） |
| 系统 | 飞牛 fnOS（x86_64） |
| LED 驱动 | `ugreen_leds_cli`（I2C，需 `i2c-dev` 模块） |

> 本应用专为 DXP4800 Plus 优化，不保证其他型号可用。

---

## 架构

```
src/app/server/
├── main.py              # 入口（ThreadingHTTPServer）
├── app_context.py       # 启动与全局状态
├── http_handler.py      # REST API + SSE + 静态文件
├── pilot_core.py        # LED 控制核心
├── uevent_watcher.py    # netlink 热插拔监听
├── auth_manager.py      # 鉴权
├── utils.py             # 工具函数
├── ugreen_leds_cli      # 捆绑 LED 驱动（构建时生成）
└── www/                 # 前端（玻璃拟态暗色 UI）
    ├── index.html
    ├── login.html
    ├── app.css
    ├── app.js
    └── login.js
```

### 技术选型

| 层 | 选择 | 原因 |
|----|------|------|
| 后端 | Python 3 标准库 | fnOS 原生支持，无额外运行时 |
| HTTP | ThreadingHTTPServer | 并发处理 API，不阻塞监控 |
| 实时 | SSE (`/api/events`) | 事件阻塞推送，空闲无轮询 |
| 前端 | 原生 HTML/CSS/JS | 无构建链，fpk 直接打包 |
| 硬件 | `ugreen_leds_cli` | I2C 控制，唯一硬件出口 |

---

## 性能策略

监控分为三个层级，按当前 LED 模式自动切换：

| 层级 | 触发条件 | 行为 | CPU 开销 |
|------|----------|------|----------|
| **sleep** | 全部关闭或常亮 | 监控线程休眠，uevent 停止 | ≈ 零 |
| **hotplug** | 自动模式 + 活动闪烁关闭 | 仅 uevent 拔插事件 + 120s 兜底签名扫描 | 极低 |
| **activity** | 自动模式 + 活动闪烁开启 | uevent + 自适应 IO 轮询（空闲退避至 3s） | 低 |

其他优化：

- **CLI 去重**：相同 LED 状态不重复调用子进程
- **设置防抖写**：颜色/亮度变更 0.8s 批量落盘
- **SSE 阻塞等待**：有变更才推送，最长 30s keepalive

---

## 安装

1. 从 [Releases](https://github.com/WXFffff666/UGreenLedPilot/releases) 下载 `UGreenLedPilot-2.2.0.x86_64.fpk`
2. 飞牛应用中心 → 手动安装
3. 打开应用，默认密码 `admin123`（**请立即修改**，至少 8 位，最长 128 位）

### 前置条件

- `i2c-dev` 内核模块已加载（安装脚本会自动尝试 `modprobe i2c-dev`）
- 应用用户已加入 `i2c` 组，或 CLI 已配置 setuid
- 若系统自带 `led_ugreen` 内核模块与 CLI 冲突，需先卸载

### 升级说明

- **全新安装**：data-share 使用新名 `UGreenLedPilot`。
- **从旧版本升级**：保留旧数据目录 `FnUGreenLed`（不删除、不迁移），升级后应用继续沿用旧数据。
- **从 v2.1.0 升级**：v2.2 效果设置持久化为 additive（新键），旧配置无效果字段时按默认值正常加载，无需迁移。

---

## 构建

**Windows：**

```powershell
.\build.ps1
```

**Linux / macOS：**

```bash
./build.sh
```

产物：`build/UGreenLedPilot-<version>.x86_64.fpk`

构建流程：生成图标 → 编译 `ugreen_leds_cli`（Docker）→ `fnpack build` → 收集 fpk。

> **CLI pin 上游 commit `af2b7ae`（2026-07-30，add pve9 pinned interface naming support #113）**
> - 三个构建入口（`release.yml` / `build.ps1` / `build.sh`）在 clone 后统一 `git fetch --depth 1 origin af2b7ae84f65a8730768d4b626570bc824b196e0 && git checkout af2b7ae84f65a8730768d4b626570bc824b196e0`，并以 `git rev-parse HEAD` 断言，确保构建产物不受上游漂移影响。
> - 已提交的捆绑二进制 `src/app/server/ugreen_leds_cli` 由 pin commit 构建（构建脚本检测到二进制存在时跳过 Docker 构建）。SHA256：`9938C0E7A83884F7783ED10BA973DACE16D91792EA3E3CA07F80A3D638D05E32`。
> - 已验证：本地 `tools/ugreen_leds_controller` HEAD = `af2b7ae84f65a8730768d4b626570bc824b196e0`，与 pin 一致。

---

## API 概览

所有写操作需登录 + `X-CSRF-Token` 请求头。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 完整状态（模式、活动、盘位映射等） |
| GET | `/api/events` | SSE 实时推送 |
| POST | `/api/login` | 登录 |
| POST | `/api/logout` | 登出 |
| POST | `/api/control` | 设置单颗 LED 模式 `{led, action}` |
| POST | `/api/color` | 设置颜色 `{led, r, g, b}` |
| POST | `/api/brightness` | 设置亮度 `{led?, brightness}` |
| POST | `/api/preset` | 应用颜色预设 `{led, preset}` |
| POST | `/api/activity-blink` | 开关活动闪烁 `{enabled}` |
| POST | `/api/all/off\|on\|auto` | 批量设置 |
| POST | `/api/remap` | 手动重映射盘位 |
| POST | `/api/change-password` | 修改密码 |
| POST | `/api/reset` | 重置配置 |

---

## 版本历史

| 版本 | 要点 |
|------|------|
| **v2.2.0** | 效果模式（呼吸/手动闪烁/跑马灯演示）；速度感知闪烁；单用户鉴权；多网口活动聚合；盘位校准；性能优化与可靠性修复；玻璃拟态 UI 美化 |
| **v2.1.0** | netlink uevent 零轮询热插拔；分层监控休眠；活动闪烁可关；玻璃拟态 UI |
| **v2.0.0** | 模块化重构；SSE 替代轮询；CLI 去重；独立 www 前端 |
| **v1.1.0** | 模式感知热插拔；DXP4800 Plus 专用；CSRF / 限流 / PBKDF2 |
| **v1.0.0** | 从 FnOSxUGreenLedDriver 独立；基础 LED 控制与 fnpack 打包 |

---

## 许可证与致谢

- LED 驱动：[miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller)
- 维护者：[WXFffff666](https://github.com/WXFffff666)
