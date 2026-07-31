# UGreenLedPilot v2

绿联 **DXP4800 Plus** 专用机箱 LED 控制，面向飞牛 fnOS。

## v2 架构

```
src/app/server/
├── main.py           # 入口（ThreadingHTTPServer）
├── app_context.py    # 启动与全局状态
├── http_handler.py   # REST API + SSE + 静态文件
├── pilot_core.py     # LED 控制核心（优化版）
├── auth_manager.py   # 鉴权
├── utils.py          # 工具函数
└── www/              # 独立前端
    ├── index.html
    ├── login.html
    ├── app.css
    ├── app.js
    └── login.js
```

## 技术选型

| 层 | 选择 | 原因 |
|----|------|------|
| 后端 | Python 3 标准库 | fnOS 原生支持，无需额外运行时 |
| HTTP | ThreadingHTTPServer | 并发处理 API，不阻塞监控 |
| 实时 | SSE (`/api/events`) | 替代 800ms 轮询，降低 CPU/网络开销 |
| 前端 | 原生 HTML/CSS/JS | 无构建链，fpk 直接打包 |
| 硬件 | ugreen_leds_cli | I2C 控制，不可替换 |

## 性能优化

- **CLI 去重**：相同 LED 状态不重复调用子进程
- **自适应监控**：有自动模式时 0.4s，否则 2s 空闲间隔
- **热插拔降频**：指纹扫描从 0.5s 降至 2s
- **设置防抖写**：颜色/亮度变更 0.8s 批量落盘
- **窄锁范围**：监控读 sysfs 不长时间持锁
- **SSE 推送**：状态变化才通知前端，无持续轮询

## 功能（完整保留）

三档模式、模式感知热插拔、颜色/亮度/预设、全局亮度、全部开关、手动重映射、管理员鉴权、CSRF、登录限流。

## 构建

```powershell
.\build.ps1
```

产物：`build/UGreenLedPilot-2.0.0.x86_64.fpk`

## 安装

飞牛应用中心 → 手动安装 → 默认密码 `admin123`（请立即修改，至少 8 位）
