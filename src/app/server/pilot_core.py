"""UGreenLedPilot — LED control core for UGREEN DXP4800 Plus on fnOS."""

import glob
import json
import os
import re
import subprocess
import threading
import time

VALID_MODES = ['off', 'on', 'auto']
MAX_DISK_LEDS = 4
LED_BASE = ['power', 'netdev']
LED_STATUS_RE = re.compile(
    r'^(?P<led>power|netdev|disk[1-4]): status = (?P<status>off|on|blink|breath), '
    r'brightness = (?P<brightness>\d+), color = RGB\((?P<r>\d+), (?P<g>\d+), (?P<b>\d+)\)'
)

# DXP4800 Plus: 4 bays, HCTL X:0:0:0 -> disk(X+1)
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
MONITOR_INTERVAL = 0.5
ACTIVITY_IO_THRESHOLD = 1

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
    def run(*args):
        try:
            r = subprocess.run([cli_path] + list(args), capture_output=True, text=True, timeout=5)
            return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
        except FileNotFoundError:
            return False, '', f'CLI not found: {cli_path}'
        except Exception as e:
            return False, '', str(e)
    return run


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f'Load failed: {path} — {e}')
    return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f'Save FAILED: {path} — {e}')


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
    """Private build: always target DXP4800 Plus profile."""
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
    """Disk topology fingerprint for hotplug — only used when disk LEDs are in auto mode."""
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
    """Network carrier fingerprint — only used when netdev LED is in auto mode."""
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


class PilotController:
    """LED controller — mode-aware hotplug, activity blink."""

    def __init__(self, led_names, run, state_file, settings_file,
                 ata_map=None, hctl_map=None, disk_count=4):
        self.leds = led_names
        self.run = run
        self.state_file = state_file
        self.settings_file = settings_file
        self.ata_map = ata_map or DXP4800_PLUS_PROFILE['ata_map']
        self.hctl_map = hctl_map or DXP4800_PLUS_PROFILE['hctl_map']
        self.disk_count = min(disk_count, MAX_DISK_LEDS)
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._rescan_count = 0

    def _load_settings(self):
        saved = load_json(self.settings_file, {})
        settings = {}
        for led in self.leds:
            base = dict(DEFAULT_SETTINGS.get(led, {'color': list(WHITE), 'brightness': 255}))
            if led in saved:
                base.update(saved[led])
            settings[led] = base
        return settings

    def _save_settings(self):
        save_json(self.settings_file, self.settings)

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
        if led not in self.leds:
            return False, f'Invalid LED: {led}'
        r, g, b = max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))
        self.settings.setdefault(led, {})['color'] = [r, g, b]
        self._save_settings()
        mode = self.modes.get(led, 'off')
        if mode != 'off':
            ok, err = self._apply(led, mode, self.activity.get(led, False))
            return ok, err
        ok, _, err = self.run(led, '-on', '-color', str(r), str(g), str(b))
        return ok, err

    def set_brightness(self, led, brightness):
        if led not in self.leds:
            return False, f'Invalid LED: {led}'
        brightness = max(0, min(255, int(brightness)))
        self.settings.setdefault(led, {})['brightness'] = brightness
        self._save_settings()
        mode = self.modes.get(led, 'off')
        if mode != 'off':
            ok, err = self._apply(led, mode, self.activity.get(led, False))
            return ok, err
        return True, 'OK'

    def set_global_brightness(self, brightness):
        brightness = max(0, min(255, int(brightness)))
        for led in self.leds:
            self.settings.setdefault(led, {})['brightness'] = brightness
        self._save_settings()
        errors = []
        for led in self.leds:
            if self.modes.get(led) != 'off':
                ok, err = self._apply(led, self.modes[led], self.activity.get(led, False))
                if not ok:
                    errors.append(f'{led}: {err}')
        return not errors, '; '.join(errors) if errors else 'OK'

    def _needs_disk_hotplug(self):
        for slot in range(1, self.disk_count + 1):
            if self.modes.get(f'disk{slot}') == 'auto':
                return True
        return False

    def _needs_net_hotplug(self):
        return self.modes.get('netdev') == 'auto'

    def needs_hotplug_monitor(self):
        """Only auto-mode LEDs need hardware topology monitoring."""
        return self._needs_disk_hotplug() or self._needs_net_hotplug()

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

    def restore_state(self, hardware_modes=None, apply_hardware=True):
        hardware_modes = hardware_modes or {}
        saved = load_json(self.state_file, {})
        self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
        self._net_iface = detect_net_iface()
        self._refresh_signatures()
        self._update_presence()
        for led in self.leds:
            mode = saved.get(led, hardware_modes.get(led, 'auto'))
            if mode not in VALID_MODES:
                mode = 'auto'
            # Only auto mode cares about presence at restore time
            if mode == 'auto' and led.startswith('disk') and not self._is_present(led):
                mode = 'off'
            self.modes[led] = mode
            self.activity[led] = False
            if apply_hardware:
                ok, msg = self._apply(led, mode, activity=False)
                if not ok:
                    print(f'Restore {led} failed: {msg}')

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
            return True, 'OK'

    def _apply(self, led, mode, activity=False):
        cfg = self.get_settings(led)
        cr, cg, cb = map(str, cfg['color'])
        brightness = str(cfg['brightness'])

        if mode == 'off':
            ok, _, err = self.run(led, '-off')
            return ok, err or 'OK'

        # on mode: always light up regardless of disk/net presence
        if mode == 'on':
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)
            return ok, err or 'OK'

        # auto mode: respect presence
        if not self._is_present(led):
            ok, _, err = self.run(led, '-off')
            return ok, err or 'OK'

        if activity:
            ok, _, err = self.run(
                led, '-on', '-blink', str(BLINK_ON_MS), str(BLINK_OFF_MS),
                '-color', cr, cg, cb, '-brightness', brightness,
            )
        else:
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)
        return ok, err or 'OK'

    def _persist(self):
        save_json(self.state_file, dict(self.modes))

    def force_remap(self):
        """Manual disk remap — only useful when auto-mode disks are configured."""
        with self._lock:
            self._on_hardware_changed(force=True)
            return True, 'OK'

    def start_monitor(self):
        self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
        self._net_iface = detect_net_iface()
        self._refresh_signatures()
        self._update_presence()
        print(f'DXP4800 Plus — Disks: {self._disk_map}, Network: {self._net_iface or "none"}')
        print(f'Hotplug monitor: {"enabled" if self.needs_hotplug_monitor() else "disabled (no auto-mode LEDs)"}')

        if self._net_iface:
            base = f'/sys/class/net/{self._net_iface}/statistics'
            self._prev_net_rx = read_stats(f'{base}/rx_bytes')
            self._prev_net_tx = read_stats(f'{base}/tx_bytes')
        for slot, dev in self._disk_map.items():
            if disk_present(dev):
                self._prev_disk_io[slot] = read_disk_io(f'/sys/block/{dev}/stat')

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop_monitor(self):
        self._stop.set()

    def _on_hardware_changed(self, force=False):
        """Full remap — only when auto-mode hardware topology changes."""
        old_map = dict(self._disk_map)
        self._disk_map = detect_disks(self.ata_map, self.hctl_map, self.disk_count)
        self._net_iface = detect_net_iface()
        self._update_presence()
        self._rescan_count += 1
        print(f'Hotplug remap #{self._rescan_count}: {old_map} -> {self._disk_map}, net={self._net_iface}')

        for led in self.leds:
            mode = self.modes.get(led, 'auto')
            if mode == 'off' or mode == 'on':
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
        """Skip entirely when no LED is in auto mode — on/off don't need hotplug scans."""
        if not self.needs_hotplug_monitor():
            return

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

    def _monitor_loop(self):
        while not self._stop.wait(MONITOR_INTERVAL):
            try:
                with self._lock:
                    if self.needs_hotplug_monitor():
                        self._maybe_rescan()
                    self._check_network()
                    self._check_disks()
            except Exception as e:
                print(f'Monitor error: {e}')

    def _check_network(self):
        led = 'netdev'
        if self.modes.get(led) != 'auto':
            return
        if not self._net_iface:
            if self.activity.get(led):
                self.activity[led] = False
                self._apply(led, 'auto', activity=False)
            return
        base = f'/sys/class/net/{self._net_iface}/statistics'
        rx = read_stats(f'{base}/rx_bytes')
        tx = read_stats(f'{base}/tx_bytes')
        delta = (rx - self._prev_net_rx) + (tx - self._prev_net_tx)
        active = delta >= ACTIVITY_IO_THRESHOLD
        self._prev_net_rx, self._prev_net_tx = rx, tx
        if active != self.activity.get(led):
            self.activity[led] = active
            self._apply(led, 'auto', activity=active)

    def _check_disks(self):
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

    def get_status(self):
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
            'rescan_count': self._rescan_count,
            'model': DXP4800_PLUS_PROFILE,
        }
