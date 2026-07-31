# UGreenLedPilot — Development Specification

## Overview

UGreenLedPilot is a native fnOS application for **UGREEN DXP4800 Plus** LED control. Users manage power, network, and disk bay indicators through a web UI with per-LED modes: off, solid on, and automatic (presence + activity).

All hardware access goes through `ugreen_leds_cli` from [miskcoo/ugreen_leds_controller](https://github.com/miskcoo/ugreen_leds_controller). No kernel module or alternate driver path is used at runtime.

**Current version:** 2.1.0

## Project History

| Version | Summary |
|---------|---------|
| v1.0.0 | Forked from FnOSxUGreenLedDriver; basic control, fnpack packaging |
| v1.1.0 | Mode-aware hotplug; DXP4800 Plus only; CSRF, rate limit, PBKDF2 |
| v2.0.0 | Modular architecture; SSE; CLI dedup; separate `www` frontend |
| v2.1.0 | netlink uevent hotplug; tiered monitor sleep; activity-blink toggle; glassmorphism UI |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Browser  →  http://127.0.0.1:19580                     │
│    ├── GET  /static/*     → www/ (HTML/CSS/JS)          │
│    ├── GET  /api/status   → JSON state                  │
│    ├── GET  /api/events   → SSE push                    │
│    └── POST /api/*        → REST (auth + CSRF)          │
│                          │                               │
│                          ▼                               │
│  main.py (ThreadingHTTPServer)                          │
│    ├── http_handler.py                                  │
│    ├── app_context.py                                   │
│    ├── pilot_core.py                                    │
│    │     ├── uevent_watcher.py  (netlink hotplug)       │
│    │     └── make_cli_runner()  → ugreen_leds_cli      │
│    └── auth_manager.py                                  │
└─────────────────────────────────────────────────────────┘
```

## Monitor Tiers

| Tier | Condition | Behavior |
|------|-----------|----------|
| `sleep` | No LED in `auto` mode | Monitor thread blocks on `Event.wait()`; uevent stopped |
| `hotplug` | `auto` mode, activity blink off | uevent on block add/remove; 120s signature fallback |
| `activity` | `auto` mode, activity blink on | uevent + sysfs IO poll with idle backoff (0.5s → 3s) |

## Device Profile

```python
DXP4800_PLUS_PROFILE = {
    'id': 'dxp4800plus',
    'disk_count': 4,
    'hctl_map': ['0:0:0:0', '1:0:0:0', '2:0:0:0', '3:0:0:0'],
    'ata_map': ['ata1', 'ata2', 'ata3', 'ata4'],
}
```

## API Endpoints

Authenticated POST routes require session cookie + `X-CSRF-Token`.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| POST | `/api/login` | `{password}` | Create session |
| GET | `/api/status` | — | Full state snapshot |
| GET | `/api/events` | — | SSE stream |
| POST | `/api/control` | `{led, action}` | Set mode: off/on/auto |
| POST | `/api/color` | `{led, r, g, b}` | Set RGB |
| POST | `/api/brightness` | `{led?, brightness}` | Per-LED or global brightness |
| POST | `/api/preset` | `{led, preset}` | Apply color preset |
| POST | `/api/activity-blink` | `{enabled}` | Toggle IO activity polling |
| POST | `/api/all/{off\|on\|auto}` | — | Batch mode |
| POST | `/api/remap` | — | Force disk remap |
| POST | `/api/change-password` | `{old_password, new_password}` | Change admin password |
| POST | `/api/reset` | — | Clear config and state |

LED names: `power`, `netdev`, `disk1`–`disk4`.

## Security

| Control | Implementation |
|---------|----------------|
| Authentication | Single admin, session cookie (`HttpOnly`, `SameSite=Strict`, 7d) |
| CSRF | `X-CSRF-Token` on all state-changing POST |
| Rate limiting | 5 failed logins / 15 min lockout per IP |
| Password | PBKDF2-SHA256, 200k iterations; min 8, max 128 chars |
| Origin check | Host must match `Origin` / `Referer` |
| Headers | `X-Content-Type-Options`, `X-Frame-Options`, CSP, `X-Robots-Tag` |
| Static files | Path traversal blocked (`..` rejected) |
| CLI | Fixed path, no shell, LED name regex validated |

## Project Structure

```
UGreenLedPilot/
├── src/
│   ├── app/server/          # Python backend + www frontend
│   ├── app/ui/              # fnOS desktop entry
│   ├── cmd/                 # Lifecycle scripts
│   ├── config/              # privilege, resource
│   ├── wizard/
│   └── manifest
├── build.ps1 / build.sh
├── tests/test_pilot_core.py
└── README.md
```

## Manifest

| Field | Value |
|-------|-------|
| `appname` | UGreenLedPilot |
| `version` | 2.1.0 |
| `service_port` | 19580 |
| `arch` | x86_64 |

## Known Limitations

1. **DXP4800 Plus only** — other UGREEN models are not supported
2. **Hardware truth is write-biased** — external CLI changes may diverge from app state
3. **Auto mode is heuristic** — activity inferred from `/sys` counters
4. **uevent fallback** — if netlink unavailable, 120s signature poll is used
5. **x86_64 only** — ARM NAS not supported
