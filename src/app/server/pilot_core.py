"""UGreenLedPilot — LED control core for UGREEN DXP4800 Plus on fnOS."""

import glob
import os
import re
import subprocess
import threading
import time

from utils import load_json, save_json
from uevent_watcher import UeventWatcher

VALID_MODES = ['off', 'on', 'auto']
MAX_DISK_LEDS = 4
LED_BASE = ['power', 'netdev']
LED_STATUS_RE = re.compile(
    r'^(?P<led>power|netdev|disk[1-4]): status = (?P<status>off|on|blink|breath), '
    r'brightness = (?P<brightness>\d+), color = RGB\((?P<r>\d+), (?P<g>\d+), (?P<b>\d+)\)'
)

DXP4800_PLUS_PROFILE = {
    'id': 'dxp4800plus',
    'name': 'DXP4800 Plus',
    'product_name': 'DXP4800 Plus',
    'disk_count': 4,
    'hctl_map': ['0:0:0:0', '1:0:0:0', '2:0:0:0', '3:0:0:0'],
    'ata_map': ['ata1', 'ata2', 'ata3', 'ata4'],
}

BLINK_ON_MS = 80
BLINK_OFF_MS = 120
ACTIVITY_IO_THRESHOLD = 1
SETTINGS_SAVE_DELAY = 0.8
ACTIVITY_FAST_INTERVAL = 0.5
ACTIVITY_SLOW_INTERVAL = 3.0
HOTPLUG_FALLBACK_INTERVAL = 120.0
SLEEP_WAKE_TIMEOUT = 3600.0

WHITE = [255, 255, 255]
NETDEV_ORANGE = [255, 165, 0]

DEFAULT_SETTINGS = {
    'power': {'color': list(WHITE), 'brightness': 255},
    'netdev': {'color': list(NETDEV_ORANGE), 'brightness': 255},
}
for _i in range(1, MAX_DISK_LEDS + 1):
    DEFAULT_SETTINGS[f'disk{_i}'] = {'color': list(WHITE), 'brightness': 255}

COLOR_PRESETS = [
    {'id': 'white', 'name': '纯白', 'color': [255, 255, 255]},
    {'id': 'green', 'name': '翠绿', 'color': [0, 255, 136]},
    {'id': 'blue', 'name': '天蓝', 'color': [0, 170, 255]},
    {'id': 'orange', 'name': '琥珀', 'color': [255, 165, 0]},
    {'id': 'yellow', 'name': '暖黄', 'color': [255, 255, 0]},
    {'id': 'red', 'name': '红色', 'color': [255, 64, 64]},
    {'id': 'purple', 'name': '紫色', 'color': [180, 80, 255]},
    {'id': 'cyan', 'name': '青色', 'color': [0, 255, 255]},
]


def run_cmd(*args):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=5)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, '', str(e)


def make_cli_runner(cli_path):
    """All LED hardware control goes through ugreen_leds_cli only."""
    if not os.path.isfile(cli_path):
        print(f'WARNING: ugreen_leds_cli not found at {cli_path}')

    def run(*args):
        try:
            r = subprocess.run([cli_path] + list(args), capture_output=True, text=True, timeout=5)
            return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
        except FileNotFoundError:
            return False, '', f'CLI not found: {cli_path}'
        except Exception as e:
            return False, '', str(e)
    return run


def led_status_to_mode(status):
    if status == 'off':
        return 'off'
    if status == 'on':
        return 'on'
    if status in ('blink', 'breath'):
        return 'auto'
    return 'off'


def parse_led_status(text):
    statuses = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or 'unavailable or non-existent' in line:
            continue
        m = LED_STATUS_RE.match(line)
        if not m:
            continue
        led = m.group('led')
        statuses[led] = {
            'status': m.group('status'),
            'mode': led_status_to_mode(m.group('status')),
            'brightness': int(m.group('brightness')),
            'color': [int(m.group('r')), int(m.group('g')), int(m.group('b'))],
        }
    return statuses


def probe_leds(run):
    ok, out, err = run('all', '-status')
    if ok:
        statuses = parse_led_status(out)
        if statuses:
            return statuses, ''
    statuses = {}
    for led in LED_BASE + [f'disk{i}' for i in range(1, MAX_DISK_LEDS + 1)]:
        ok, out, _ = run(led, '-status')
        if ok:
            parsed = parse_led_status(out)
            if led in parsed:
                statuses[led] = parsed[led]
    return statuses, err


def disk_count_from_leds(statuses):
    disk_nums = [int(m.group(1)) for led in statuses if (m := re.match(r'^disk([1-4])$', led))]
    return max(disk_nums) if disk_nums else 0


def detect_model():
    product = ''
    ok, out, _ = run_cmd('dmidecode', '--string', 'system-product-name')
    if ok and out:
        product = out.strip()
    if not product:
        for path in ('/sys/class/dmi/id/product_name', '/sys/devices/virtual/dmi/id/product_name'):
            try:
                with open(path) as f:
                    product = f.read().strip()
                if product:
                    break
            except IOError:
                pass
    profile = dict(DXP4800_PLUS_PROFILE)
    profile['product_name'] = product or DXP4800_PLUS_PROFILE['product_name']
    if product and 'DXP4800' not in product:
        profile['warning'] = f'非 DXP4800 系列机型 ({product})，仍按 DXP4800 Plus 配置运行'
    return profile


def block_device_info(sdp):
    dev = os.path.basename(sdp)
    real = os.path.realpath(sdp)
    ata = None
    hctl = None
    m = re.search(r'/ata(\d+)/', real)
    if m:
        ata = f'ata{m.group(1)}'
    device_path = os.path.join(sdp, 'device')
    try:
        hctl = os.path.basename(os.path.realpath(device_path))
    except OSError:
        pass
    serial = ''
    for path in (os.path.join(device_path, 'serial'), os.path.join(device_path, 'wwid')):
        try:
            with open(path) as f:
                serial = f.read().strip()
            if serial:
                break
        except IOError:
            pass
    return {'dev': dev, 'ata': ata, 'hctl': hctl, 'serial': serial}


def collect_block_devices():
    devices = {}
    ok, out, _ = run_cmd('lsblk', '-S', '-o', 'NAME,HCTL,SERIAL')
    if ok:
        for line in out.strip().split('\n')[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith('sd'):
                devices[parts[0]] = {
                    'hctl': parts[1], 'ata': None,
                    'serial': parts[2] if len(parts) > 2 else '',
                }
    for path in sorted(glob.glob('/sys/block/sd*')):
        info = block_device_info(path)
        dev = info['dev']
        if dev not in devices:
            devices[dev] = info
        else:
            devices[dev].update({k: v for k, v in info.items() if v and k != 'dev'})
    return devices


def map_disks_by_profile(devices, ata_map=None, hctl_map=None, disk_count=4, mapping_method='hctl'):
    disks = {}
    ata_map = ata_map or []
    hctl_map = hctl_map or DXP4800_PLUS_PROFILE['hctl_map']
    limit = min(disk_count, len(hctl_map))

    if mapping_method == 'ata' and ata_map:
        for slot in range(1, limit + 1):
            target = ata_map[slot - 1]
            for dev, info in devices.items():
                if info.get('ata') == target:
                    disks[slot] = dev
                    break
    else:
        for slot in range(1, limit + 1):
            target = hctl_map[slot - 1]
            for dev, info in devices.items():
                if info.get('hctl') == target:
                    disks[slot] = dev
                    break

    if not disks and devices:
        for idx, dev in enumerate(sorted(devices.keys()), start=1):
            if idx <= disk_count:
                disks[idx] = dev
    return disks


def detect_disks(ata_map=None, hctl_map=None, disk_count=4, mapping_method='hctl'):
    return map_disks_by_profile(
        collect_block_devices(), ata_map=ata_map, hctl_map=hctl_map,
        disk_count=disk_count, mapping_method=mapping_method,
    )


def list_net_ifaces():
    net_dir = '/sys/class/net'
    if not os.path.isdir(net_dir):
        return []
    ifaces = []
    for iface in sorted(os.listdir(net_dir)):
        if iface in ('lo', 'docker0') or iface.startswith(('br-', 'veth', 'vir', 'tun', 'wg')):
            continue
        ifaces.append(iface)
    return ifaces


def detect_net_iface():
    for iface in list_net_ifaces():
        try:
            with open(f'/sys/class/net/{iface}/carrier') as f:
                if f.read().strip() == '1':
                    return iface
        except IOError:
            pass
    return None


def disk_signature():
    keys = []
    for path in sorted(glob.glob('/sys/block/sd*')):
        info = block_device_info(path)
        keys.append((
            info['ata'] or '',
            info['hctl'] or '',
            info['serial'] or info['dev'],
        ))
    return tuple(keys)


def net_signature():
    iface = detect_net_iface()
    carrier = ''
    if iface:
        try:
            with open(f'/sys/class/net/{iface}/carrier') as f:
                carrier = f.read().strip()
        except IOError:
            pass
    return iface or '', carrier


def quick_hardware_signature():
    return (disk_signature(), net_signature())


def read_stats(path):
    try:
        with open(path) as f:
            return int(f.read().strip().split()[0])
    except (IOError, ValueError, IndexError):
        return 0


def read_disk_io(path):
    try:
        with open(path) as f:
            vals = [int(x) for x in f.read().strip().split()]
        return vals[0] + vals[4]
    except (IOError, ValueError, IndexError):
        return 0


def disk_present(dev):
    return bool(dev) and os.path.isfile(f'/sys/block/{dev}/stat')


class StatusBroadcaster:
    """Thread-safe pub/sub for SSE clients with blocking wait."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []
        self._version = 0

    def subscribe(self):
        q = []
        ev = threading.Event()
        with self._lock:
            self._subscribers.append((q, ev))
        return q, ev

    def unsubscribe(self, sub):
        q = sub[0] if isinstance(sub, tuple) else sub
        with self._lock:
            self._subscribers = [(sq, se) for sq, se in self._subscribers if sq is not q]

    def publish(self, payload):
        with self._lock:
            self._version += 1
            dead = []
            for i, (q, ev) in enumerate(self._subscribers):
                if len(q) > 20:
                    dead.append(i)
                else:
                    q.append(payload)
                    ev.set()
            for i in reversed(dead):
                self._subscribers.pop(i)

    @property
    def version(self):
        return self._version


class PilotController:
    """LED controller — ugreen_leds_cli only, tiered zero-poll monitoring."""

    def __init__(self, led_names, run, state_file, settings_file,
                 ata_map=None, hctl_map=None, disk_count=4, broadcaster=None,
                 activity_blink=True):
        self.leds = led_names
        self.run = run
        self.state_file = state_file
        self.settings_file = settings_file
        self.ata_map = ata_map or DXP4800_PLUS_PROFILE['ata_map']
        self.hctl_map = hctl_map or DXP4800_PLUS_PROFILE['hctl_map']
        self.disk_count = min(disk_count, MAX_DISK_LEDS)
        self.broadcaster = broadcaster or StatusBroadcaster()
        self.activity_blink_enabled = bool(activity_blink)
        self.modes = {}
        self.activity = {}
        self.presence = {}
        self.settings = self._load_settings()
        self._disk_map = {}
        self._net_iface = None
        self._prev_net_rx = 0
        self._prev_net_tx = 0
        self._prev_disk_io = {}
        self._disk_sig = None
        self._net_sig = None
        self._last_applied = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._hotplug_pending = threading.Event()
        self._thread = None
        self._rescan_count = 0
        self._settings_dirty = False
        self._settings_timer = None
        self._last_hotplug_check = 0.0
        self._activity_idle_rounds = 0
        self._uevent = UeventWatcher(self._on_uevent_hotplug)

    def _load_settings(self):
        saved = load_json(self.settings_file, {})
        settings = {}
        for led in self.leds:
            base = dict(DEFAULT_SETTINGS.get(led, {'color': list(WHITE), 'brightness': 255}))
            if led in saved:
                base.update(saved[led])
            settings[led] = base
        return settings

    def _schedule_settings_save(self):
        self._settings_dirty = True
        if self._settings_timer:
            self._settings_timer.cancel()
        self._settings_timer = threading.Timer(SETTINGS_SAVE_DELAY, self._flush_settings)
        self._settings_timer.daemon = True
        self._settings_timer.start()

    def _flush_settings(self):
        with self._lock:
            if self._settings_dirty:
                save_json(self.settings_file, self.settings)
                self._settings_dirty = False

    def get_settings(self, led):
        return dict(self.settings.get(led, DEFAULT_SETTINGS.get(led, {
            'color': list(WHITE), 'brightness': 255,
        })))

    def apply_preset(self, led, preset_id):
        preset = next((p for p in COLOR_PRESETS if p['id'] == preset_id), None)
        if not preset:
            return False, '未知预设'
        c = preset['color']
        return self.set_color(led, c[0], c[1], c[2])

    def set_color(self, led, r, g, b):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            r, g, b = max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))
            self.settings.setdefault(led, {})['color'] = [r, g, b]
            self._schedule_settings_save()
            mode = self.modes.get(led, 'off')
            if mode != 'off':
                ok, err = self._apply(led, mode, self.activity.get(led, False))
                self._notify()
                return ok, err
            return True, 'OK'

    def set_brightness(self, led, brightness):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            brightness = max(0, min(255, int(brightness)))
            self.settings.setdefault(led, {})['brightness'] = brightness
            self._schedule_settings_save()
            mode = self.modes.get(led, 'off')
            if mode != 'off':
                ok, err = self._apply(led, mode, self.activity.get(led, False))
                self._notify()
                return ok, err
            return True, 'OK'

    def set_global_brightness(self, brightness):
        with self._lock:
            brightness = max(0, min(255, int(brightness)))
            for led in self.leds:
                self.settings.setdefault(led, {})['brightness'] = brightness
            self._schedule_settings_save()
            errors = []
            for led in self.leds:
                if self.modes.get(led) != 'off':
                    ok, err = self._apply(led, self.modes[led], self.activity.get(led, False))
                    if not ok:
                        errors.append(f'{led}: {err}')
            self._notify()
            return not errors, '; '.join(errors) if errors else 'OK'

    def _needs_disk_hotplug(self):
        for slot in range(1, self.disk_count + 1):
            if self.modes.get(f'disk{slot}') == 'auto':
                return True
        return False

    def _needs_net_hotplug(self):
        return self.modes.get('netdev') == 'auto'

    def needs_hotplug_monitor(self):
        return self._needs_disk_hotplug() or self._needs_net_hotplug()

    def _has_auto_activity_targets(self):
        if not self.activity_blink_enabled:
            return False
        if self.modes.get('netdev') == 'auto':
            return True
        for slot in range(1, self.disk_count + 1):
            if self.modes.get(f'disk{slot}') == 'auto':
                return True
        return False

    def _monitor_tier(self):
        if not self.needs_hotplug_monitor():
            return 'sleep'
        if not self.activity_blink_enabled:
            return 'hotplug'
        return 'activity'

    def _wake_monitor(self):
        self._wake_event.set()

    def _on_uevent_hotplug(self):
        if not self.needs_hotplug_monitor():
            return
        self._hotplug_pending.set()
        self._wake_monitor()

    def _sync_watchers(self):
        tier = self._monitor_tier()
        if tier in ('hotplug', 'activity'):
            if not self._uevent.is_running():
                self._uevent.start()
        else:
            self._uevent.stop()

    def set_activity_blink(self, enabled):
        with self._lock:
            self.activity_blink_enabled = bool(enabled)
            if not enabled:
                for led in self.leds:
                    if self.activity.get(led):
                        self.activity[led] = False
                        if self.modes.get(led) == 'auto':
                            self._apply(led, 'auto', activity=False)
            self._sync_watchers()
            self._wake_monitor()
            self._notify()
            return True, 'OK'

    def _is_present(self, led):
        if led == 'power':
            return True
        if led == 'netdev':
            return bool(self._net_iface)
        m = re.match(r'disk(\d+)', led)
        if m:
            return disk_present(self._disk_map.get(int(m.group(1))))
        return True

    def _update_presence(self):
        presence = {'power': True, 'netdev': bool(self._net_iface)}
        for slot in range(1, self.disk_count + 1):
            presence[f'disk{slot}'] = disk_present(self._disk_map.get(slot))
        self.presence = presence

    def _refresh_signatures(self):
        self._disk_sig = disk_signature()
        self._net_sig = net_signature()

    def _apply_key(self, led, mode, activity):
        cfg = self.get_settings(led)
        return (mode, activity, tuple(cfg['color']), cfg['brightness'])

    def restore_state(self, hardware_modes=None, apply_hardware=True):
        hardware_modes = hardware_modes or {}
        with self._lock:
            saved = load_json(self.state_file, {})
            self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
            self._net_iface = detect_net_iface()
            self._refresh_signatures()
            self._update_presence()
            for led in self.leds:
                mode = saved.get(led, hardware_modes.get(led, 'auto'))
                if mode not in VALID_MODES:
                    mode = 'auto'
                if mode == 'auto' and led.startswith('disk') and not self._is_present(led):
                    mode = 'off'
                self.modes[led] = mode
                self.activity[led] = False
                if apply_hardware:
                    ok, msg = self._apply(led, mode, activity=False)
                    if not ok:
                        print(f'Restore {led} failed: {msg}')
            self._notify()

    def set_mode(self, led, mode):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            if mode not in VALID_MODES:
                return False, f'Invalid mode: {mode}'
            ok, msg = self._apply(led, mode, activity=False)
            if not ok:
                return False, msg
            self.modes[led] = mode
            self.activity[led] = False
            self._persist()
            self._sync_watchers()
            self._wake_monitor()
            self._notify()
            return True, 'OK'

    def _apply(self, led, mode, activity=False):
        key = self._apply_key(led, mode, activity)
        if self._last_applied.get(led) == key:
            return True, 'OK'

        cfg = self.get_settings(led)
        cr, cg, cb = map(str, cfg['color'])
        brightness = str(cfg['brightness'])

        if mode == 'off':
            ok, _, err = self.run(led, '-off')
        elif mode == 'on':
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)
        elif not self._is_present(led):
            ok, _, err = self.run(led, '-off')
        elif activity:
            ok, _, err = self.run(
                led, '-on', '-blink', str(BLINK_ON_MS), str(BLINK_OFF_MS),
                '-color', cr, cg, cb, '-brightness', brightness,
            )
        else:
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)

        if ok:
            self._last_applied[led] = key
        return ok, err or 'OK'

    def _persist(self):
        save_json(self.state_file, dict(self.modes))

    def force_remap(self):
        with self._lock:
            self._on_hardware_changed()
            self._notify()
            return True, 'OK'

    def start_monitor(self):
        with self._lock:
            self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
            self._net_iface = detect_net_iface()
            self._refresh_signatures()
            self._update_presence()
        print(f'DXP4800 Plus — Disks: {self._disk_map}, Network: {self._net_iface or "none"}')
        tier = self._monitor_tier()
        print(f'Monitor tier: {tier} (hotplug={"on" if self.needs_hotplug_monitor() else "off"})')

        if self._net_iface:
            base = f'/sys/class/net/{self._net_iface}/statistics'
            self._prev_net_rx = read_stats(f'{base}/rx_bytes')
            self._prev_net_tx = read_stats(f'{base}/tx_bytes')
        for slot, dev in self._disk_map.items():
            if disk_present(dev):
                self._prev_disk_io[slot] = read_disk_io(f'/sys/block/{dev}/stat')

        self._sync_watchers()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name='pilot-monitor')
        self._thread.start()

    def stop_monitor(self):
        self._stop.set()
        self._wake_monitor()
        self._uevent.stop()
        if self._settings_timer:
            self._settings_timer.cancel()
        self._flush_settings()

    def _on_hardware_changed(self):
        old_map = dict(self._disk_map)
        self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
        self._net_iface = detect_net_iface()
        self._update_presence()
        self._rescan_count += 1
        print(f'Hotplug remap #{self._rescan_count}: {old_map} -> {self._disk_map}, net={self._net_iface}')

        for led in self.leds:
            mode = self.modes.get(led, 'auto')
            if mode in ('off', 'on'):
                continue
            if led.startswith('disk') and not self._is_present(led):
                self._apply(led, 'auto', activity=False)
                continue
            if mode == 'auto':
                self._apply(led, 'auto', self.activity.get(led, False))

        for slot, dev in self._disk_map.items():
            if disk_present(dev) and slot not in self._prev_disk_io:
                self._prev_disk_io[slot] = read_disk_io(f'/sys/block/{dev}/stat')

        if self._net_iface:
            base = f'/sys/class/net/{self._net_iface}/statistics'
            self._prev_net_rx = read_stats(f'{base}/rx_bytes')
            self._prev_net_tx = read_stats(f'{base}/tx_bytes')

    def _maybe_rescan(self):
        if not self.needs_hotplug_monitor():
            return False

        changed = False
        if self._needs_disk_hotplug():
            sig = disk_signature()
            if sig != self._disk_sig:
                self._disk_sig = sig
                changed = True
        if self._needs_net_hotplug():
            sig = net_signature()
            if sig != self._net_sig:
                self._net_sig = sig
                changed = True
        if changed:
            self._on_hardware_changed()
            return True
        return False

    def _monitor_loop(self):
        while not self._stop.is_set():
            tier = self._monitor_tier()
            self._sync_watchers()

            if tier == 'sleep':
                self._wake_event.wait(timeout=SLEEP_WAKE_TIMEOUT)
                self._wake_event.clear()
                continue

            interval = ACTIVITY_FAST_INTERVAL
            notified = False
            try:
                if self._hotplug_pending.is_set():
                    self._hotplug_pending.clear()
                    with self._lock:
                        self._on_hardware_changed()
                    notified = True
                elif tier == 'hotplug':
                    now = time.monotonic()
                    if (now - self._last_hotplug_check) >= HOTPLUG_FALLBACK_INTERVAL:
                        self._last_hotplug_check = now
                        with self._lock:
                            if self._maybe_rescan():
                                notified = True
                else:
                    with self._lock:
                        if self._check_network() or self._check_disks():
                            notified = True
                            self._activity_idle_rounds = 0
                            interval = ACTIVITY_FAST_INTERVAL
                        else:
                            self._activity_idle_rounds += 1
                            if self._activity_idle_rounds > 20:
                                interval = ACTIVITY_SLOW_INTERVAL
                            elif self._activity_idle_rounds > 6:
                                interval = 1.5

                if notified:
                    self._notify()
            except Exception as e:
                print(f'Monitor error: {e}')

            if self._wake_event.wait(timeout=interval):
                self._wake_event.clear()

    def _check_network(self):
        led = 'netdev'
        if self.modes.get(led) != 'auto':
            return False
        if not self._net_iface:
            if self.activity.get(led):
                self.activity[led] = False
                self._apply(led, 'auto', activity=False)
                return True
            return False
        base = f'/sys/class/net/{self._net_iface}/statistics'
        rx = read_stats(f'{base}/rx_bytes')
        tx = read_stats(f'{base}/tx_bytes')
        delta = (rx - self._prev_net_rx) + (tx - self._prev_net_tx)
        active = delta >= ACTIVITY_IO_THRESHOLD
        self._prev_net_rx, self._prev_net_tx = rx, tx
        if active != self.activity.get(led):
            self.activity[led] = active
            self._apply(led, 'auto', activity=active)
            return True
        return False

    def _check_disks(self):
        changed = False
        for slot in range(1, self.disk_count + 1):
            led = f'disk{slot}'
            if self.modes.get(led) != 'auto' or led not in self.leds:
                continue
            dev = self._disk_map.get(slot)
            if not disk_present(dev):
                if self.activity.get(led) or self.presence.get(led):
                    self.activity[led] = False
                    self.presence[led] = False
                    self._apply(led, 'auto', activity=False)
                    changed = True
                continue
            self.presence[led] = True
            io = read_disk_io(f'/sys/block/{dev}/stat')
            prev = self._prev_disk_io.get(slot, 0)
            delta = io - prev if io >= prev else io
            active = delta >= ACTIVITY_IO_THRESHOLD
            self._prev_disk_io[slot] = io
            if active != self.activity.get(led):
                self.activity[led] = active
                self._apply(led, 'auto', activity=active)
                changed = True
        return changed

    def get_live_status(self):
        """Lightweight snapshot for SSE / API."""
        with self._lock:
            return {
                'modes': dict(self.modes),
                'activity': dict(self.activity),
                'presence': dict(self.presence),
                'net_iface': self._net_iface,
                'hotplug_monitor': self.needs_hotplug_monitor(),
                'monitor_tier': self._monitor_tier(),
                'activity_blink': self.activity_blink_enabled,
                'uevent_available': self._uevent.available,
                'rescan_count': self._rescan_count,
            }

    def get_status(self):
        with self._lock:
            return {
                'modes': dict(self.modes),
                'activity': dict(self.activity),
                'presence': dict(self.presence),
                'settings': {k: dict(v) for k, v in self.settings.items() if k in self.leds},
                'disk_map': {str(k): v for k, v in self._disk_map.items()},
                'net_iface': self._net_iface,
                'leds': self.leds,
                'presets': COLOR_PRESETS,
                'hotplug_monitor': self.needs_hotplug_monitor(),
                'monitor_tier': self._monitor_tier(),
                'activity_blink': self.activity_blink_enabled,
                'uevent_available': self._uevent.available,
                'rescan_count': self._rescan_count,
                'model': DXP4800_PLUS_PROFILE,
            }

    def _notify(self):
        self.broadcaster.publish(self.get_live_status())
