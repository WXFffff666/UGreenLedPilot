# UGreenLedPilot

绿联 NAS 机箱 LED 智能控制应用，面向飞牛 fnOS。与原 FnOSxUGreenLedDriver 完全独立。

基于 [miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller) 的 `ugreen_leds_cli` 驱动，通过 I2C 控制前面板电源、网络、磁盘指示灯。

## 功能

| 功能 | 说明 |
|------|------|
| 三档模式 | 关闭 / 常亮 / 自动 |
| 自动模式 | 插盘亮灯、拔盘关灯；网口有载波则亮；读写/流量时闪烁 |
| 颜色与亮度 | 每灯独立调色盘 + 8 种预设色 + 全局亮度滑块 |
| 默认配色 | 电源/磁盘：白色；网口：琥珀橙 `RGB(255,165,0)`（UGOS 默认风格） |
| 盘位映射 | ATA 机型映射，DXP6800 等特殊顺序已内置 |
| 热插拔 | **事件驱动**：仅硬件拓扑变化时重映射，无定时全量扫描 |
| 管理员鉴权 | PBKDF2 密码哈希 + HttpOnly 会话，所有 API 需登录 |

## 适配机型

DXP2800 / DXP4800 / DXP4800 Plus / DXP6800 / DXP8800 / DX4600 / DX4700 等 x86_64 机型。

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

产物：`build/UGreenLedPilot-1.0.0.x86_64.fpk`

### 依赖

- [fnpack](https://static2.fnnas.com/fnpack/) 打包工具
- Docker（可选，用于编译 `ugreen_leds_cli`）
- fnOS ≥ 0.9.27，x86_64，root/I2C 权限

## 安装

1. 飞牛应用中心 → 设置 → 手动安装应用
2. 选择 `UGreenLedPilot-1.0.0.x86_64.fpk`
3. 打开应用，使用默认密码 `admin123` 登录
4. **首次登录后请立即修改密码**（至少 6 位）

## 安全说明

- 仅管理员可访问（`allUsers: false` + 应用内密码）
- 会话 Cookie：`pilot_session`，7 天有效
- 密码以 PBKDF2-SHA256 存储，不明文保存
- 所有控制 API（含状态查询）均需有效会话

## 热插拔策略

监控循环每 0.5 秒执行轻量指纹比对（磁盘 ATA/HCTL/序列号 + 网口载波）。**仅当指纹变化时**才触发完整盘位重映射，避免无意义的持续扫描。

## 许可证

MIT（驱动部分遵循上游 ugreen_leds_controller 许可证）
