---
name: fnpack
description: Build, package, test, and manage fnOS (飞牛 NAS) native applications using fnpack and appcenter-cli
---

# fnpack & appcenter-cli — fnOS Application Development Toolkit

Two CLI tools work together for fnOS app development:

| Tool | Runs on | Purpose |
|------|---------|---------|
| `fnpack` | Local dev machine (Mac/Windows/Linux) | Create project scaffold, build `.fpk` packages |
| `appcenter-cli` | fnOS device only (SSH) | Install, test, start/stop, debug apps on the NAS |

`fnpack` is at `/Users/zuoteng/Documents/Projects/FnXUGreenLed/fnpack` (v1.2.0, darwin-arm64). `appcenter-cli` is pre-installed on fnOS.

## Commands

### `fnpack create <appname>` — Create a new app project

Creates a standard directory structure for a native app.

Flags:
- `-t, --template string` — `native` (default) or `docker`
- `-w, --without-ui` — Skip browser UI config (default: false)

The generated structure:

```
<appname>/
├── app/
│   └── ui/
│       ├── config          # Desktop entry configuration (JSON)
│       └── images/         # UI icons (icon_64.png, icon_256.png)
├── cmd/
│   ├── main                # Start/stop/status lifecycle script
│   ├── install_init        # Pre-install hook
│   ├── install_callback    # Post-install hook
│   ├── uninstall_init      # Pre-uninstall hook
│   ├── uninstall_callback  # Post-uninstall hook
│   ├── upgrade_init        # Pre-upgrade hook
│   ├── upgrade_callback    # Post-upgrade hook
│   ├── config_init         # Pre-config-change hook
│   └── config_callback     # Post-config-change hook
├── config/
│   ├── privilege           # Permission configuration (JSON)
│   └── resource            # Resource/capability declaration (JSON)
├── wizard/                 # User interaction wizards (install/uninstall/upgrade/config)
├── manifest                # App metadata (key=value format)
├── ICON.PNG                # 64x64 icon
└── ICON_256.PNG            # 256x256 icon
```

### `fnpack build` — Package app into .fpk file

```bash
cd <appname>
fnpack build
# or specify directory:
fnpack build --directory <path>
```

Build validation checks:
- `manifest` exists with required fields
- `config/privilege` exists and is valid JSON
- `config/resource` exists and is valid JSON
- `ICON.PNG` exists
- `ICON_256.PNG` exists
- `app/` directory exists
- `cmd/` directory exists
- `wizard/` directory exists
- If `desktop_uidir` is defined in manifest, `app/{desktop_uidir}/` must exist

---

## appcenter-cli — On-Device Testing & Management

`appcenter-cli` runs **on the fnOS NAS device** (via SSH). It is pre-installed on fnOS and is the primary tool for local testing and debugging.

### Commands

#### `appcenter-cli install-local` — Install from project directory (dev mode)

**This is the most important command for development.** Run it from the app project directory on the NAS. It auto-packages and installs in one step — no need to build an fpk first.

```bash
# On the NAS: copy project to device, then
cd /path/to/<appname>
appcenter-cli install-local
```

This is much faster for iteration than building fpk → transferring → installing.

#### `appcenter-cli install-fpk` — Install from fpk file

```bash
appcenter-cli install-fpk <filename.fpk>

# Silent install with env vars (skips interactive wizard):
appcenter-cli install-fpk myapp.fpk --env config.env
```

The `--env` file uses `key=value` format for wizard variables:
```
wizard_admin_username=admin
wizard_admin_password=secret123
wizard_database_type=sqlite
wizard_app_port=8080
wizard_agree_terms=true
```

#### `appcenter-cli start/stop` — Start/stop an installed app

```bash
appcenter-cli start <appname>
appcenter-cli stop <appname>
```

#### `appcenter-cli list` — List installed apps

```bash
appcenter-cli list
```

#### `appcenter-cli default-volume` — View/set default storage volume

```bash
appcenter-cli default-volume        # view current
appcenter-cli default-volume 1      # set to volume 1
```

#### `appcenter-cli manual-install` — Enable/disable manual installation

```bash
appcenter-cli manual-install        # view status
appcenter-cli manual-install enable  # allow third-party installs
appcenter-cli manual-install disable # block third-party installs
```

**Important:** Enable this before installing third-party apps; disable after for security.

### There is no `uninstall`, `restart`, `logs`, or `status` subcommand

Uninstall is done through the fnOS web UI (App Center). For logs, check the app's `var/` directory on the NAS:
```bash
cat /var/apps/<appname>/var/info.log
```

---

## Development & Testing Workflow

### Recommended Iteration Loop

```
┌─────────────────────────────────────────────────────────┐
│  1. Local: Edit code in project directory                │
│  2. Local: fnpack build (validate structure)             │
│  3. NAS:   rsync/scp project to NAS                      │
│  4. NAS:   appcenter-cli install-local (reinstall)       │
│  5. NAS:   Test in fnOS desktop (web UI)                 │
│  6. NAS:   Check logs at /var/apps/<appname>/var/        │
│  7. Fix → repeat from step 1                             │
│  8. Final: fnpack build → distribute .fpk                │
└─────────────────────────────────────────────────────────┘
```

### Step-by-step

```bash
# 1-2. Build locally to validate
cd /path/to/project/<appname>
fnpack build

# 3. Sync to NAS (replace IP with your NAS address)
rsync -avz --delete /path/to/project/<appname>/ admin@<nas-ip>:/path/to/<appname>/

# 4. SSH to NAS and install
ssh admin@<nas-ip>
cd /path/to/<appname>
appcenter-cli install-local

# 5. Open fnOS desktop → App Center → find your app → launch

# 6. If something fails, check logs
cat /var/apps/<appname>/var/info.log
# Also check system log for install errors:
cat /var/apps/<appname>/tmp/install_log  # if exists

# 7. Fix code locally, re-sync, re-install
```

### Enabling Third-Party Installs

On first use, you may need to enable manual installation on the NAS:

```bash
ssh admin@<nas-ip>
appcenter-cli manual-install enable
```

### Debugging Install Failures

When `install-local` fails:

1. **Check manifest validity**: Ensure all required fields are present
2. **Check JSON validity**: `config/privilege` and `config/resource` must be valid JSON
3. **Check cmd scripts exit codes**: All lifecycle scripts must `exit 0` on success
4. **Check error log**: The system writes errors to `$TRIM_TEMP_LOGFILE` during install
5. **Test scripts manually**: Run `bash cmd/install_init` directly to see if it fails
6. **Check file permissions**: `app/server/index.cgi` and all `cmd/*` scripts must be executable (`chmod +x`)

---



Key-value INI-like file. Required fields: `appname`, `version`, `display_name`, `desc`, `source`.

| Field | Required | Description |
|-------|----------|-------------|
| `appname` | Yes | Unique application identifier |
| `version` | Yes | Version number, e.g. `1.0.0` |
| `display_name` | Yes | Display name in App Center |
| `desc` | Yes | Description (supports HTML) |
| `arch` | No | Architecture, default `x86_64` |
| `platform` | No | `x86`, `arm`, or `all` (V1.1.8+) |
| `source` | Yes | Always `thirdparty` for third-party apps |
| `maintainer` | No | Developer/team name |
| `distributor` | No | Distributor name |
| `desktop_uidir` | No | UI directory relative to app root, default `ui` |
| `desktop_applaunchname` | No | Entry ID matching a key in `app/{desktop_uidir}/config` |
| `service_port` | No | Port number for the app's HTTP service |
| `checkport` | No | Enable port checking, default `true` |
| `ctl_stop` | No | Show start/stop controls, default `true`. Set `false` for CGI/serverless apps |
| `install_type` | No | Set to `root` for system partition install |
| `os_min_version` | No | Minimum fnOS version, e.g. `0.9.0` |
| `os_max_version` | No | Maximum fnOS version |
| `install_dep_apps` | No | Colon-separated dependency list, e.g. `python312:nodejs_v22` |
| `disable_authorization_path` | No | Hide authorization directory settings, default `false` |
| `changelog` | No | Update changelog (shown in App Center) |

## App Entry Configuration (`app/ui/config`)

JSON file defining desktop/web entry points under the `.url` key. Entry names must be prefixed with `appname`.

```json
{
    ".url": {
        "<appname>.Application": {
            "title": "App Title",
            "icon": "images/icon_{0}.png",
            "type": "url",
            "protocol": "http",
            "port": "8080",
            "url": "/",
            "allUsers": true
        }
    }
}
```

Entry fields:
- `title` — Display name
- `icon` — Icon path relative to UI dir; `{0}` = size (64/256)
- `type` — `"url"` (new tab) or `"iframe"` (embedded in desktop)
- `protocol` — `"http"`, `"https"`, or `""` for adaptive
- `port` — Port number (omit for CGI-based apps)
- `url` — Internal path; for CGI apps use `/cgi/ThirdParty/<appname>/index.cgi/`
- `allUsers` — `true` = all users, `false` = admin only
- `fileTypes` — Array of file extensions for right-click menu
- `noDisplay` — `true` = hide from desktop, right-click only
- `control.accessPerm` — `"editable"`, `"readonly"`, `"hidden"`

### Two App Types

**CGI-based (simpler, no persistent process):**
- Uses `type: "iframe"`, no `port` field
- `url` points to `/cgi/ThirdParty/<appname>/index.cgi/`
- `app/server/index.cgi` is the Python CGI script
- Set `ctl_stop: false` in manifest
- `cmd/main` is minimal (no process to manage)

**Self-hosted server (for persistent services):**
- Uses `type: "url"` with `protocol` and `port`
- `cmd/main` starts the HTTP server process
- Static files and API endpoints served by the app's own server
- `service_port` declared in manifest

## Privilege Configuration (`config/privilege`)

```json
{
    "defaults": {
        "run-as": "package"
    },
    "username": "myapp",
    "groupname": "myapp"
}
```

- `defaults.run-as`: `"package"` (app-specific user) or `"root"` (root privileges; enterprise partners only)
- `username`/`groupname`: Custom user/group names (default: appname)

## Resource Configuration (`config/resource`)

Three optional sections:

### data-share — Shared data directories
```json
{
    "data-share": {
        "shares": [
            {
                "name": "myapp",
                "permission": { "rw": ["myapp"] }
            }
        ]
    }
}
```

### usr-local-linker — Symlink to /usr/local/
```json
{
    "usr-local-linker": {
        "bin": ["bin/my-cli"],
        "lib": ["lib/mylib.so"],
        "etc": ["etc/myapp.conf"]
    }
}
```

### docker-project — Docker Compose support
```json
{
    "docker-project": {
        "projects": [
            { "name": "myapp-stack", "path": "docker" }
        ]
    }
}
```

## Wizard Configuration (`wizard/*`)

Each wizard file is a JSON array of step objects. Supported field types: `text`, `password`, `radio`, `checkbox`, `select`, `switch`, `tips`.

```json
[
    {
        "stepTitle": "Step Title",
        "items": [
            {
                "type": "text",
                "field": "wizard_username",
                "label": "Username",
                "initValue": "admin",
                "rules": [
                    { "required": true, "message": "Required field" }
                ]
            }
        ]
    }
]
```

## Cmd Scripts Specification

### `cmd/main` — Start/Stop/Status

Receives `start`, `stop`, or `status` as `$1`. Exit codes:
- `start`: 0=success, 1=failure
- `stop`: 0=success, 1=failure
- `status`: 0=running, 3=not running

Key environment variables available in scripts:
- `TRIM_PKGVAR` — Runtime data directory (logs, PID files)
- `TRIM_PKGETC` — Config directory
- `TRIM_APPDEST` — Install target directory
- `TRIM_PKGTMP` — Temp directory
- `TRIM_PKGHOME` — User data directory
- `TRIM_SERVICE_PORT` — Service port from manifest
- `TRIM_USERNAME` / `TRIM_GROUPNAME` — App user/group
- `TRIM_TEMP_LOGFILE` — User-visible log file (write errors here, then `exit 1`)
- `TRIM_DATA_SHARE_PATHS` — Colon-separated shared data paths
- `TRIM_APPNAME` — App name
- `TRIM_APPVER` — App version
- `TRIM_SYS_VERSION` — fnOS version
- `TRIM_SYS_ARCH` — System architecture

Error handling (V1.1.8+):
```bash
if ! some_check; then
    echo "Descriptive error message" > "${TRIM_TEMP_LOGFILE}"
    exit 1
fi
```

### Other lifecycle hooks
- `install_init` / `install_callback` — Before/after installation
- `uninstall_init` / `uninstall_callback` — Before/after uninstallation
- `upgrade_init` / `upgrade_callback` — Before/after upgrade
- `config_init` / `config_callback` — Before/after config changes

## Quick Reference: Creating a New App

```bash
# 1. Scaffold project locally
fnpack create <appname>

# 2. Customize: manifest, configs, cmd scripts, app files, icons

# 3. Validate packaging locally
cd <appname>
fnpack build

# 4. Sync to NAS and test
rsync -avz ./ admin@<nas-ip>:/home/admin/<appname>/
ssh admin@<nas-ip> "cd /home/admin/<appname> && appcenter-cli install-local"

# 5. Iterate: fix → rsync → install-local → test → repeat

# 6. Final build for distribution
fnpack build
# Output: <appname>-<version>.fpk
```

## Quick Reference: Building for Distribution

```bash
cd <app_directory>
fnpack build
# Output: ../<appname>-<version>.fpk
```

## Important Notes

- All cmd scripts must be executable (`chmod +x`)
- `app/server/index.cgi` must be executable for CGI-based apps
- The `TRIM_` prefix is reserved for system env vars — wizard fields should NOT use this prefix
- For root-run apps, set `"run-as": "root"` in privilege (enterprise partners only)
- Icon files: ICON.PNG (64x64) and ICON_256.PNG (256x256) at app root
- The `wizard/` directory must exist even if empty
- `fnpack build` output appears in the parent directory as `<appname>-<version>.fpk`
- **Always test with `appcenter-cli install-local` on a real NAS before distributing the fpk** — `fnpack build` passing does not guarantee the app works on fnOS
