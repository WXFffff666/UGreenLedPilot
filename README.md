# UGreenLedPilot

绿联 **DXP4800 Plus** 专用机箱 LED 控制应用，面向飞牛 fnOS。私有项目，仅适配本机型。

基于 [miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller) 的 `ugreen_leds_cli` 驱动，通过 I2C (`0x3a`) 控制前面板电源、网络、磁盘指示灯。

## 功能

| 功能 | 说明 |
|------|------|
| 三档模式 | 关闭 / 常亮 / 自动 |
| 模式感知热插拔 | **仅自动模式**检测硬件变化并重映射；常亮/关闭不扫描 |
| 常亮模式 | 插拔盘不影响灯状态，始终保持亮起 |
| 自动模式 | 插盘亮灯、拔盘关灯；网口有载波则亮；读写/流量时闪烁 |
| 颜色与亮度 | 每灯独立调色盘 + 8 种预设色 + 全局亮度滑块 |
| 默认配色 | 电源/磁盘：白色；网口：琥珀橙 `RGB(255,165,0)` |
| 盘位映射 | HCTL `X:0:0:0 → disk(X+1)`，DXP4800 Plus 固定 4 盘位 |
| 手动重映射 | Web 界面「重映射盘位」按钮 |
| 管理员鉴权 | PBKDF2-SHA256 + HttpOnly 会话 + CSRF + 登录限流 |

## 适配机型

**仅 UGREEN DXP4800 Plus**（4 盘位，x86_64）

## 项目结构

```
src/
├── app/server/
│   ├── main.py           # HTTP 服务 + UI
│   ├── pilot_core.py     # LED 控制核心
│   └── auth_manager.py   # 管理员鉴权
├── cmd/                  # fnOS 生命周期脚本
├── config/               # 权限与资源
├── manifest              # 应用元数据
└── wizard/
tests/
└── test_pilot_core.py
```

## 构建

### Windows

```powershell
.\build.ps1
```

### Linux / macOS

```bash
chmod +x build.sh
./build.sh
```

产物：`build/UGreenLedPilot-1.1.0.x86_64.fpk`

### 依赖

- [fnpack](https://static2.fnnas.com/fnpack/) 打包工具
- Docker（可选，用于编译 `ugreen_leds_cli`）
- fnOS ≥ 0.9.27，x86_64，root/I2C 权限

## 安装

1. 飞牛应用中心 → 设置 → 手动安装应用
2. 选择 `UGreenLedPilot-1.1.0.x86_64.fpk`
3. 打开应用，使用默认密码 `admin123` 登录
4. **首次登录后请立即修改密码**（至少 8 位，不可使用默认密码）

## 安全说明

- 仅管理员可访问（`allUsers: false` + 应用内密码）
- 会话 Cookie：`pilot_session`，7 天有效，`HttpOnly; SameSite=Strict`
- 密码以 PBKDF2-SHA256（200000 轮）存储
- 所有 POST API 需 CSRF Token + Origin 校验
- 登录失败 5 次锁定 15 分钟
- 安全响应头：CSP、X-Frame-Options、nosniff 等

## 热插拔策略

| 模式 | 热插拔扫描 | 行为 |
|------|-----------|------|
| 关闭 | 不扫描 | 灯已关闭，无需检测 |
| 常亮 | 不扫描 | 始终保持亮起，插拔盘不影响 |
| 自动 | 事件驱动 | 仅硬件拓扑变化时重映射 |

监控循环每 0.5 秒检查活动闪烁（IO/流量），但硬件指纹比对仅在存在自动模式 LED 时执行。

## 许可证

MIT（驱动部分遵循上游 ugreen_leds_controller 许可证）
