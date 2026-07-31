# Roadmap

## v1.0.x — 基础能力（已完成）

- [x] 手动开关 LED（电源、网络、磁盘）
- [x] 全部开启 / 全部关闭
- [x] fnpack 标准项目结构，一键构建
- [x] `ugreen_leds_cli` 静态编译打包
- [x] 非标端口 19580
- [x] LED 状态持久化，启动时自动恢复
- [x] LED 探测（`ugreen_leds_cli all -status`）

## v1.1 — 状态同步与安全（已完成）

- [x] 磁盘活动检测（自动模式闪烁）
- [x] 网络活动检测
- [x] 三档模式：关闭 / 常亮 / 自动
- [x] 模式感知热插拔（常亮/关闭不扫描）
- [x] 管理员鉴权、CSRF、登录限流、PBKDF2

## v1.2 — DXP4800 Plus 专用（已完成）

- [x] 锁定 DXP4800 Plus（4 盘位，HCTL 映射）
- [x] 移除多型号配置向导，简化部署

## v2.0 — 架构重构（已完成）

- [x] 模块化拆分（`pilot_core` / `http_handler` / `auth_manager` / `www`）
- [x] ThreadingHTTPServer 并发
- [x] SSE 替代前端 800ms 轮询
- [x] CLI 状态去重
- [x] 自适应监控间隔
- [x] 独立玻璃风格前端 UI

## v2.1 — 极致性能（已完成）

- [x] netlink uevent 事件驱动热插拔（零轮询）
- [x] 分层监控：sleep / hotplug / activity
- [x] 常亮/关闭时监控线程休眠（CPU ≈ 零）
- [x] 活动闪烁开关（关闭后仅事件驱动）
- [x] SSE 阻塞推送 + 30s keepalive
- [x] 玻璃拟态 UI 重绘
- [x] 密码长度上限 128 字符

## 未来计划

- [ ] **定时策略**：夜间自动调暗/关闭 LED
- [ ] **事件通知**：磁盘故障、网络断开时 LED 告警
- [ ] **fnOS 通知中心**：异常时推送系统通知
- [ ] **快捷面板**：桌面小组件显示 LED 状态
- [ ] **多语言**：英文 UI
