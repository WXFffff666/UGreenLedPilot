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
VALID_EFFECTS = ['breath', 'manual-blink']
# Effect priority matrix (F7): when several effect requests target
# the same LED the highest-priority one wins.
#   off > on > manual-blink > breath > activity-blink > chase
#   - off:            mode-level, outranks every effect (checked first in _apply)
#   - on:             mode-level solid on, the base layer effects refine
#   - manual-blink:   UI blink effect (-blink 400/400); never demoted once set
#   - breath:         hardware-native breath; outranks auto activity-blink
#   - activity-blink: auto-mode IO blink (only effective in 'auto'); yields to
#                     user effects (manual-blink/breath)
#   - chase:          demo running light; paused while any activity blinks
EFFECT_PRIORITY = {'manual-blink': 3, 'breath': 2, 'activity-blink': 1, 'chase': 0}
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
MANUAL_BLINK_ON_MS = 400
MANUAL_BLINK_OFF_MS = 400
BREATH_ON_MS = 800
BREATH_OFF_MS = 1200
SPEED_BLINK_LOW_RATE = 100_000      # bytes/s — below: steady on
SPEED_BLINK_HIGH_RATE = 1_000_000   # bytes/s — at or above: fast blink
SPEED_BLINK_MID_ON_MS = 400
SPEED_BLINK_MID_OFF_MS = 400
SPEED_BLINK_FAST_ON_MS = 100
SPEED_BLINK_FAST_OFF_MS = 100
ACTIVITY_IO_THRESHOLD = 1
CHASE_STEP_INTERVAL = 0.2  # seconds — minimum gap between chase steps (>= 200ms)
SETTINGS_SAVE_DELAY = 0.8
ACTIVITY_FAST_INTERVAL = 0.5
ACTIVITY_SLOW_INTERVAL = 3.0
HOTPLUG_FALLBACK_INTERVAL = 120.0
SLEEP_WAKE_TIMEOUT = 3600.0
IDENTIFY_DURATION = 10.0  # seconds — identify() blink self-expires (C4)
NIGHT_CHECK_INTERVAL = 60.0      # seconds — night-schedule evaluation cadence
NIGHT_DEFAULT_BRIGHTNESS = 13    # ≈5% dim level when the night window is active
ALERT_DEBOUNCE_CHECKS = 3        # consecutive checks before a failure is alerted
ALERT_BLINK_ON_MS = 200          # red alert blink timing
ALERT_BLINK_OFF_MS = 200
ALERT_RED = [255, 64, 64]        # distinctive alert colour

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
    keys = []
    for iface in list_net_ifaces():
        carrier = ''
        try:
            with open(f'/sys/class/net/{iface}/carrier') as f:
                carrier = f.read().strip()
        except IOError:
            pass
        keys.append((iface, carrier))
    return tuple(keys)


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
                 activity_blink=True, speed_blink=False, bay_bindings=None,
                 night_schedule=None, alert_cfg=None):
        self.leds = led_names
        self.run = run
        self.state_file = state_file
        self.settings_file = settings_file
        self.ata_map = ata_map or DXP4800_PLUS_PROFILE['ata_map']
        self.hctl_map = hctl_map or DXP4800_PLUS_PROFILE['hctl_map']
        self.bay_bindings = dict(bay_bindings or {})
        self.disk_count = min(disk_count, MAX_DISK_LEDS)
        self.broadcaster = broadcaster or StatusBroadcaster()
        self.activity_blink_enabled = bool(activity_blink)
        self.speed_blink_enabled = bool(speed_blink)
        self.modes = {}
        self.activity = {}
        self.effects = {}
        self.presence = {}
        self.settings = self._load_settings()
        self._disk_map = {}
        self._net_iface = None
        self._net_ifaces = []
        self._prev_net_rx = {}
        self._prev_net_tx = {}
        self._prev_disk_io = {}
        self._last_net_check = None
        self._last_disk_check = {}
        self._disk_sig = None
        self._net_sig = None
        self._last_applied = {}
        self._last_blink_params = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._hotplug_pending = threading.Event()
        self._thread = None
        self._rescan_count = 0
        self._settings_dirty = False
        self._settings_timer = None
        self._settings_epoch = 0  # R6: bumped on stop so stale save timers skip
        self._last_hotplug_check = 0.0
        self._activity_idle_rounds = 0
        self.chase_enabled = False
        self._chase_leds = []
        self._chase_index = 0
        self._chase_current = None
        self._chase_clock = time.monotonic  # injectable for tests
        self._chase_next_at = 0.0
        self._identify_until = 0.0
        self._identify_led = None
        self._uevent = UeventWatcher(self._on_uevent_hotplug)
        self.night_schedule = {
            'enabled': False, 'start_hour': 22, 'end_hour': 7,
            'night_brightness': NIGHT_DEFAULT_BRIGHTNESS,
        }
        ns = night_schedule or {}
        try:
            self.night_schedule['enabled'] = bool(ns.get('enabled', False))
            self.night_schedule['start_hour'] = int(ns.get('start_hour', 22))
            self.night_schedule['end_hour'] = int(ns.get('end_hour', 7))
            self.night_schedule['night_brightness'] = int(
                ns.get('night_brightness', NIGHT_DEFAULT_BRIGHTNESS))
        except (TypeError, ValueError):
            pass  # keep defaults on malformed config
        self.night_active = False
        self._now = time.localtime  # injectable wall clock for tests
        ac = alert_cfg or {}
        self.alert_cfg = {
            'enabled': bool(ac.get('enabled', False)),
            'disk_failure': bool(ac.get('disk_failure', True)),
            'network_down': bool(ac.get('network_down', True)),
        }
        self.alerts = {'disk_lost': {}, 'network_down': False}
        self._alert_absent = {}     # slot -> consecutive-absent count
        self._net_down_count = 0

    def _load_settings(self):
        saved = load_json(self.settings_file, {})
        self.effects = {}
        saved_effects = saved.get('effects') if isinstance(saved, dict) else None
        if isinstance(saved_effects, dict):
            for led, effect in saved_effects.items():
                if led in self.leds and effect in VALID_EFFECTS:
                    self.effects[led] = effect
        settings = {}
        for led in self.leds:
            base = dict(DEFAULT_SETTINGS.get(led, {'color': list(WHITE), 'brightness': 255}))
            if led in saved:
                base.update(saved[led])
            settings[led] = base
        return settings

    @staticmethod
    def _bay_slot(key):
        """Slot number from a bay_bindings key ('disk1' / '1' / 1) or None."""
        if isinstance(key, int):
            return key
        m = re.match(r'^disk(\d+)$', str(key))
        if m:
            return int(m.group(1))
        try:
            return int(str(key))
        except ValueError:
            return None

    def _merged_hctl_map(self):
        """hctl_map with bay_bindings applied (bound slots win, others default)."""
        merged = list(self.hctl_map)
        for key, hctl in self.bay_bindings.items():
            slot = self._bay_slot(key)
            if slot is not None and 1 <= slot <= len(merged):
                merged[slot - 1] = str(hctl)
        return merged

    def set_bay_bindings(self, bindings):
        """Replace the slot -> hctl bay-binding map; applies on next remap/scan."""
        with self._lock:
            self.bay_bindings = dict(bindings or {})
        return True, 'OK'

    def _schedule_settings_save(self):
        self._settings_dirty = True
        if self._settings_timer:
            self._settings_timer.cancel()
        # R6: the timer captures the current epoch; _flush_settings skips
        # when it fires after stop_monitor bumped the epoch (old controller
        # must not write stale settings over a rebuilt controller's file)
        self._settings_timer = threading.Timer(
            SETTINGS_SAVE_DELAY, self._flush_settings, args=(self._settings_epoch,))
        self._settings_timer.daemon = True
        self._settings_timer.start()

    def _flush_settings(self, epoch=None):
        # R6: a timer from before the last stop (stale epoch) must not write
        if epoch is not None and epoch != self._settings_epoch:
            return
        with self._lock:
            if self._settings_dirty:
                # additive-only: never drop top-level keys written by other
                # features (e.g. upgrade from v2.1.0 files without 'effects')
                saved = load_json(self.settings_file, {})
                if not isinstance(saved, dict):
                    saved = {}
                saved.update(self.settings)
                saved['effects'] = dict(self.effects)
                save_json(self.settings_file, saved)
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
            # C6: keep a speed-aware blink's rhythm — reapply with the LED's
            # last blink_params instead of collapsing to a transient solid-on
            return self._apply_if_active(led, blink_params=self._last_blink_params.get(led))

    def set_brightness(self, led, brightness):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            brightness = max(0, min(255, int(brightness)))
            self.settings.setdefault(led, {})['brightness'] = brightness
            self._schedule_settings_save()
            # C6: see set_color — keep the active blink rhythm on brightness changes
            return self._apply_if_active(led, blink_params=self._last_blink_params.get(led))

    def set_effect(self, led, effect, t_on=None, t_off=None):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            if effect is not None and effect not in VALID_EFFECTS:
                return False, f'Invalid effect: {effect}'
            if effect is None:
                self.effects.pop(led, None)
            else:
                current = self.effects.get(led)
                if current is not None and EFFECT_PRIORITY[effect] < EFFECT_PRIORITY[current]:
                    # priority matrix (F7): never demote the active
                    # effect with a lower-priority one (e.g. breath onto
                    # manual-blink). Same effect still updates its parameters.
                    return False, (f'Rejected {effect}: {current} has higher priority')
                self.effects[led] = effect
            self._schedule_settings_save()
            if effect == 'breath':
                if t_on is not None:
                    self.settings.setdefault(led, {})['breath_t_on'] = max(0, int(t_on))
                if t_off is not None:
                    self.settings.setdefault(led, {})['breath_t_off'] = max(0, int(t_off))
            # C6: keep the active blink rhythm on effect changes
            return self._apply_if_active(led, blink_params=self._last_blink_params.get(led))

    def identify(self, led):
        """Locate an LED by blinking it with its current color (F8, Todo 20).

        Runs inside the controller lock — unlike the old handler-level run()
        call which bypassed it (F2 finding 4) — and records the manual-blink
        apply key so a later _apply of the same blink state is deduped.
        """
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            cfg = self.get_settings(led)
            cr, cg, cb = map(str, cfg['color'])
            brightness = str(cfg['brightness'])
            ok, _, err = self.run(
                led, '-on', '-blink', str(MANUAL_BLINK_ON_MS), str(MANUAL_BLINK_OFF_MS),
                '-color', cr, cg, cb, '-brightness', brightness,
            )
            if ok:
                self._last_applied[led] = self._apply_key(
                    led, 'on', False, effect='manual-blink')
                # C4: the identify blink self-expires — _monitor_loop
                # restores the LED's mode once IDENTIFY_DURATION elapses
                # (off/steady LEDs must not blink forever)
                self._identify_until = time.monotonic() + IDENTIFY_DURATION
                self._identify_led = led
            return ok, err or 'OK'

    def set_global_brightness(self, brightness):
        with self._lock:
            brightness = max(0, min(255, int(brightness)))
            for led in self.leds:
                self.settings.setdefault(led, {})['brightness'] = brightness
            self._schedule_settings_save()
            errors = []
            for led in self.leds:
                # C6: keep each active LED's blink rhythm; single notify below
                ok, err = self._apply_if_active(
                    led, blink_params=self._last_blink_params.get(led), notify=False)
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
        return self.chase_enabled or self._needs_disk_hotplug() or self._needs_net_hotplug()

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
        if self.chase_enabled:
            # chase is a live demo: it must run on the fast activity cadence,
            # never stall in the sleep/hotplug branches
            return 'activity'
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
                            # priority matrix (F7): reapply through
                            # _apply so user effects (manual-blink/breath) are
                            # kept — only the activity-blink state is reset
                            self._apply(led, 'auto', activity=False)
            self._sync_watchers()
            self._wake_monitor()
            self._notify()
            return True, 'OK'

    def set_speed_blink(self, enabled):
        """Toggle rate-aware blink (independent of activity_blink)."""
        with self._lock:
            self.speed_blink_enabled = bool(enabled)
            if not enabled:
                for led in self.leds:
                    if self.modes.get(led) == 'auto' and self.activity.get(led):
                        self._apply(led, 'auto', activity=True)
            self._notify()
            return True, 'OK'

    def set_night_schedule(self, schedule):
        """Configure the night window (auto-dim) and evaluate immediately.

        schedule keys: enabled(bool), start_hour(0-23), end_hour(0-23),
        night_brightness(0-255). 0 = fully off during the window.
        """
        try:
            enabled = bool(schedule.get('enabled', False))
            start = int(schedule.get('start_hour', 22))
            end = int(schedule.get('end_hour', 7))
            bright = int(schedule.get('night_brightness', NIGHT_DEFAULT_BRIGHTNESS))
        except (TypeError, ValueError):
            return False, '无效参数'
        if not (0 <= start <= 23 and 0 <= end <= 23 and 0 <= bright <= 255):
            return False, '无效参数'
        with self._lock:
            self.night_schedule = {
                'enabled': enabled, 'start_hour': start, 'end_hour': end,
                'night_brightness': bright,
            }
            self._evaluate_night_mode()
            self._notify()
            return True, 'OK'

    def _in_night_window(self, lt=None):
        """True when the wall clock falls in [start_hour, end_hour).

        Supports cross-midnight windows (start > end, e.g. 22 -> 7):
        in-window iff hour >= start or hour < end. Non-crossing windows
        (start <= end) are in-window iff start <= hour < end.
        """
        lt = lt or self._now()
        hour = lt.tm_hour
        s, e = self.night_schedule['start_hour'], self.night_schedule['end_hour']
        if s == e:
            return False
        if s > e:
            return hour >= s or hour < e
        return s <= hour < e

    def _evaluate_night_mode(self):
        """Recompute night_active from the schedule; re-emit LEDs on change.

        Returns True when the active state flipped (callers then _notify()).
        """
        if not self.night_schedule.get('enabled'):
            if not self.night_active:
                return False
            self.night_active = False
            self._reapply_all()
            return True
        active = self._in_night_window()
        if active == self.night_active:
            return False
        self.night_active = active
        self._reapply_all()
        return True

    def _reapply_all(self):
        """Force a CLI re-emit of every non-off LED (night dim/restore)."""
        for led in self.leds:
            mode = self.modes.get(led, 'off')
            if mode == 'off':
                continue
            self._last_applied.pop(led, None)
            self._apply(led, mode, activity=self.activity.get(led, False))

    def _night_effective_brightness(self, cfg_brightness):
        """Day brightness unless night window is active and dimmer."""
        if self.night_active:
            night_b = self.night_schedule.get('night_brightness', NIGHT_DEFAULT_BRIGHTNESS)
            return min(cfg_brightness, night_b)
        return cfg_brightness

    def set_alert_config(self, cfg):
        """Configure failure alerts (disk loss / network down).

        cfg keys: enabled(bool), disk_failure(bool), network_down(bool).
        Changing the config resets debounce counters so a fresh condition
        is required before the next alert.
        """
        if not isinstance(cfg, dict):
            return False, '无效参数'
        for key in ('enabled', 'disk_failure', 'network_down'):
            if key in cfg and not isinstance(cfg[key], bool):
                return False, '无效参数'
        with self._lock:
            self.alert_cfg = {
                'enabled': bool(cfg.get('enabled', False)),
                'disk_failure': bool(cfg.get('disk_failure', True)),
                'network_down': bool(cfg.get('network_down', True)),
            }
            self._alert_absent = {}
            self._net_down_count = 0
            self.alerts = {'disk_lost': {}, 'network_down': False}
            self._notify()
            return True, 'OK'

    def set_chase(self, enabled):
        """Demo chase (running light) over the disk LEDs.

        chase is an independent demo effect: it forces the monitor into the
        'activity' tier, keeps the fast interval (idle backoff skipped) and
        yields to live activity (activity-blink outranks the demo). Any
        set_mode() call stops the chase and restores the per-LED modes.
        """
        with self._lock:
            if not enabled:
                self._disable_chase()
            else:
                self.chase_enabled = True
                self._chase_leds = [f'disk{i}' for i in range(1, self.disk_count + 1)
                                    if f'disk{i}' in self.leds]
                self._chase_index = 0
                self._chase_current = None
                self._chase_next_at = 0.0
            self._activity_idle_rounds = 0
            self._sync_watchers()
            self._wake_monitor()
            self._notify()
            return True, 'OK'

    def _disable_chase(self):
        """Stop the chase demo and restore hardware to the per-LED modes."""
        self.chase_enabled = False
        chase_leds, self._chase_leds = self._chase_leds, []
        self._chase_current = None
        for led in chase_leds:
            if led in self.leds:
                self._apply(led, self.modes.get(led, 'off'))

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

    def _apply_key(self, led, mode, activity, effect=None, t_on=None, t_off=None,
                   blink_params=None, alert=False):
        # Dedup key contract (K2): the key must encode every input that
        # changes the emitted CLI command — mode, activity, effect timing and
        # presence (disk present / NIC carrier). presence must be part of the
        # key: without it a pull/replug while idle produces the same key as
        # the steady-on state and the dedup skips the -off/-on transition,
        # leaving the LED lit / dark (H1). night_active and alert also change
        # the emitted CLI, so they are part of the key too.
        cfg = self.get_settings(led)
        presence = self.presence.get(led, False)
        night = self.night_active
        if effect == 'breath':
            return (mode, activity, effect, t_on, t_off,
                    tuple(cfg['color']), cfg['brightness'], presence, night, alert)
        return (mode, activity, effect, blink_params,
                tuple(cfg['color']), cfg['brightness'], presence, night, alert)

    def restore_state(self, hardware_modes=None, apply_hardware=True):
        hardware_modes = hardware_modes or {}
        with self._lock:
            saved = load_json(self.state_file, {})
            self._disk_map = detect_disks(self.ata_map, self._merged_hctl_map(), self.disk_count)
            self._net_iface = detect_net_iface()
            self._net_ifaces = list_net_ifaces()
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
            # T3: apply night dimming at boot if the window is active
            self._evaluate_night_mode()
            self._notify()

    def set_mode(self, led, mode):
        with self._lock:
            if led not in self.leds:
                return False, f'Invalid LED: {led}'
            if mode not in VALID_MODES:
                return False, f'Invalid mode: {mode}'
            if self.chase_enabled:
                # any user mode change exits the demo and restores real modes
                self._disable_chase()
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

    def all_off(self):
        with self._lock:
            # F2: stop the chase demo first — otherwise the monitor
            # loop would keep stepping and re-light the LEDs after the batch off
            self._disable_chase()
            ok, _, err = self.run('all', '-off')
            if not ok:
                return False, err or 'OK'
            for led in self.leds:
                self.modes[led] = 'off'
                self.activity[led] = False
                self._last_applied[led] = self._apply_key(led, 'off', False)
            self._persist()
            self._sync_watchers()
            self._wake_monitor()
            self._notify()
            return True, 'OK'

    def _apply_if_active(self, led, blink_params=None, notify=True):
        """M6: shared tail of set_color/set_brightness/set_effect/global
        brightness — apply the LED's current mode when it is not 'off' and
        notify. Returns (ok, err) with (True, 'OK') for inactive LEDs."""
        mode = self.modes.get(led, 'off')
        if mode == 'off':
            return True, 'OK'
        ok, err = self._apply(led, mode, self.activity.get(led, False), blink_params=blink_params)
        if notify:
            self._notify()
        return ok, err

    def _apply(self, led, mode, activity=False, blink_params=None, alert=False):
        # priority matrix (F7): mode 'off' outranks every effect and
        # user effects (manual-blink/breath) outrank auto activity-blink —
        # both are enforced by the branch order below
        effect = self.effects.get(led)
        t_on = t_off = None
        if effect == 'breath':
            cfg0 = self.get_settings(led)
            t_on = cfg0.get('breath_t_on', BREATH_ON_MS)
            t_off = cfg0.get('breath_t_off', BREATH_OFF_MS)
        key = self._apply_key(led, mode, activity, effect, t_on, t_off, blink_params,
                          alert=alert)
        if self._last_applied.get(led) == key:
            return True, 'OK'

        cfg = self.get_settings(led)
        cr, cg, cb = map(str, cfg['color'])
        brightness = str(self._night_effective_brightness(cfg['brightness']))

        if mode == 'off':
            ok, _, err = self.run(led, '-off')
        elif alert:
            # T5: failure alert — red blink, distinct from normal activity blink
            ok, _, err = self.run(
                led, '-on', '-blink', str(ALERT_BLINK_ON_MS), str(ALERT_BLINK_OFF_MS),
                '-color', str(ALERT_RED[0]), str(ALERT_RED[1]), str(ALERT_RED[2]),
                '-brightness', brightness,
            )
        elif self.night_active and self.night_schedule.get('night_brightness') == 0:
            # night window with night_brightness=0 → turn the LED fully off
            ok, _, err = self.run(led, '-off')
        elif effect == 'breath':
            ok, _, err = self.run(
                led, '-breath', str(t_on), str(t_off),
                '-color', cr, cg, cb, '-brightness', brightness,
            )
        elif effect == 'manual-blink':
            ok, _, err = self.run(
                led, '-on', '-blink', str(MANUAL_BLINK_ON_MS), str(MANUAL_BLINK_OFF_MS),
                '-color', cr, cg, cb, '-brightness', brightness,
            )
        elif mode == 'on':
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)
        elif not self._is_present(led):
            ok, _, err = self.run(led, '-off')
        elif activity:
            if blink_params is not None:
                ok, _, err = self.run(
                    led, '-on', '-blink', str(blink_params[0]), str(blink_params[1]),
                    '-color', cr, cg, cb, '-brightness', brightness,
                )
            elif self.speed_blink_enabled:
                # speed-aware: rate below low threshold → steady on
                ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)
            else:
                ok, _, err = self.run(
                    led, '-on', '-blink', str(BLINK_ON_MS), str(BLINK_OFF_MS),
                    '-color', cr, cg, cb, '-brightness', brightness,
                )
        else:
            ok, _, err = self.run(led, '-on', '-color', cr, cg, cb, '-brightness', brightness)

        if ok:
            self._last_applied[led] = key
            if blink_params is not None:
                # C6: remember the LED's blink rhythm so set_color/set_brightness/
                # set_effect reapply it instead of flashing the LED solid-on;
                # cleared when the LED's last apply was not a blink (steady-on)
                self._last_blink_params[led] = blink_params
            else:
                self._last_blink_params.pop(led, None)
        return ok, err or 'OK'

    def _persist(self):
        save_json(self.state_file, dict(self.modes))

    def force_remap(self):
        # R1: _on_hardware_changed takes the lock itself AFTER probing —
        # do not hold the lock across the lsblk subprocess
        self._on_hardware_changed()
        self._notify()
        return True, 'OK'

    def start_monitor(self):
        with self._lock:
            self._disk_map = detect_disks(self.ata_map, self._merged_hctl_map(), self.disk_count)
            self._net_iface = detect_net_iface()
            self._net_ifaces = list_net_ifaces()
            self._refresh_signatures()
            self._update_presence()
            # C5: seed the speed-blink clock so the first activity tick
            # computes a real elapsed instead of a 0-elapsed steady-on flash
            self._last_net_check = time.monotonic()
        print(f'DXP4800 Plus — Disks: {self._disk_map}, Network: {self._net_iface or "none"}')
        tier = self._monitor_tier()
        print(f'Monitor tier: {tier} (hotplug={"on" if self.needs_hotplug_monitor() else "off"})')

        self._prev_net_rx = {}
        self._prev_net_tx = {}
        for iface in self._net_ifaces:
            base = f'/sys/class/net/{iface}/statistics'
            self._prev_net_rx[iface] = read_stats(f'{base}/rx_bytes')
            self._prev_net_tx[iface] = read_stats(f'{base}/tx_bytes')
        for slot, dev in self._disk_map.items():
            if disk_present(dev):
                self._prev_disk_io[slot] = read_disk_io(f'/sys/block/{dev}/stat')
                # C5: reseed the per-disk blink clock alongside the IO counter
                self._last_disk_check[slot] = time.monotonic()

        self._sync_watchers()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name='pilot-monitor')
        self._thread.start()

    def stop_monitor(self):
        self._stop.set()
        self._wake_monitor()
        self._uevent.stop()
        # R6: invalidate pending settings-save timers (they captured the old
        # epoch); the direct flush below still persists the current state
        self._settings_epoch += 1
        if self._settings_timer:
            self._settings_timer.cancel()
        self._flush_settings()

    def _on_hardware_changed(self):
        # R1: probe hardware OUTSIDE the lock — detect_disks spawns an lsblk
        # subprocess (5s timeout); holding the lock across it would block
        # every UI mutation (set_color/set_mode/...) while it hangs.
        old_map = dict(self._disk_map)
        disk_map = detect_disks(self.ata_map, self._merged_hctl_map(), self.disk_count)
        net_iface = detect_net_iface()
        net_ifaces = list_net_ifaces()
        with self._lock:
            self._disk_map = disk_map
            self._net_iface = net_iface
            self._net_ifaces = net_ifaces
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
                    # reapply via _apply: user effects (manual-blink/breath)
                    # outrank activity-blink and survive the remap (F7)
                    self._apply(led, 'auto', self.activity.get(led, False))

            for slot, dev in self._disk_map.items():
                if disk_present(dev):
                    if slot not in self._prev_disk_io:
                        self._prev_disk_io[slot] = read_disk_io(f'/sys/block/{dev}/stat')
                    # C5: hotplug resets the IO counter — reseed the blink clock
                    # so speed-blink does not recompute from a stale elapsed window
                    self._last_disk_check[slot] = time.monotonic()

            self._prev_net_rx = {}
            self._prev_net_tx = {}
            # C5: reseed the net blink clock alongside the reset counters
            self._last_net_check = time.monotonic()
            for iface in self._net_ifaces:
                base = f'/sys/class/net/{iface}/statistics'
                self._prev_net_rx[iface] = read_stats(f'{base}/rx_bytes')
                self._prev_net_tx[iface] = read_stats(f'{base}/tx_bytes')

            # P1: refresh the fallback-scan signatures so _maybe_rescan sees
            # the post-remap state and does not schedule a second full remap
            self._refresh_signatures()

    def _maybe_rescan(self):
        if not self.needs_hotplug_monitor():
            return False

        changed = False
        if self._needs_disk_hotplug():
            sig = disk_signature()
            # compare-and-set stays atomic under the lock; the probe itself
            # (glob + file reads, no subprocess) runs lock-free (R1)
            with self._lock:
                if sig != self._disk_sig:
                    self._disk_sig = sig
                    changed = True
        if self._needs_net_hotplug():
            sig = net_signature()
            with self._lock:
                if sig != self._net_sig:
                    self._net_sig = sig
                    changed = True
        if changed:
            self._on_hardware_changed()
            return True
        return False

    def _any_led_active(self):
        return any(self.activity.get(led) for led in self.leds)

    def _step_chase(self):
        """Advance the chase demo one LED (running light). No-op until the
        CHASE_STEP_INTERVAL has elapsed on the injectable _chase_clock.
        Must be called with self._lock held. Returns True when a CLI apply
        was issued."""
        if not self._chase_leds:
            return False
        now = self._chase_clock()
        if now < self._chase_next_at:
            return False
        self._chase_next_at = now + CHASE_STEP_INTERVAL
        led = self._chase_leds[self._chase_index % len(self._chase_leds)]
        self._chase_index = (self._chase_index + 1) % len(self._chase_leds)
        if self._chase_current is not None and self._chase_current != led:
            self._apply(self._chase_current, 'off')
        self._chase_current = led
        self._apply(led, 'on')
        return True

    def _monitor_loop(self):
        while not self._stop.is_set():
            tier = self._monitor_tier()
            self._sync_watchers()

            # C4: expire a pending identify blink and restore the LED's mode
            if self._identify_until and time.monotonic() >= self._identify_until:
                with self._lock:
                    led, self._identify_led = self._identify_led, None
                    self._identify_until = 0.0
                    if led is not None:
                        self._last_applied.pop(led, None)
                        self._apply(led, self.modes.get(led, 'off'))

            # T3: re-evaluate the night window (dim/restore) on every tick
            with self._lock:
                if self._evaluate_night_mode():
                    self._notify()

            if tier == 'sleep':
                timeout = SLEEP_WAKE_TIMEOUT
                if self._identify_until:
                    # C4: don't sleep past a pending identify expiry
                    timeout = min(timeout, self._identify_until - time.monotonic())
                if self.night_schedule.get('enabled'):
                    # T3: wake at least every minute to re-evaluate the night
                    # window so the dim/restore transition happens on time
                    # even while the monitor is sleeping.
                    timeout = min(timeout, NIGHT_CHECK_INTERVAL)
                self._wake_event.wait(timeout=timeout)
                self._wake_event.clear()
                continue

            interval = ACTIVITY_FAST_INTERVAL
            notified = False
            try:
                if self._hotplug_pending.is_set():
                    self._hotplug_pending.clear()
                    # R1: _on_hardware_changed probes hardware outside the
                    # lock — holding the lock across lsblk would block all UI
                    self._on_hardware_changed()
                    notified = True
                elif tier == 'hotplug':
                    now = time.monotonic()
                    if (now - self._last_hotplug_check) >= HOTPLUG_FALLBACK_INTERVAL:
                        self._last_hotplug_check = now
                        # R1: signature probes run lock-free; the compare-and-set
                        # stays atomic inside _maybe_rescan
                        if self._maybe_rescan():
                            notified = True
                else:
                    with self._lock:
                        # C3/M2: when activity blink is off (or no LED is in
                        # auto mode) the IO checks must not run — traffic
                        # would otherwise -blink an LED the user told to stay
                        # put. _has_auto_activity_targets() gates them; the
                        # chase demo below still steps.
                        if self._has_auto_activity_targets() and (
                                self._check_network() or self._check_disks()):
                            notified = True
                            self._activity_idle_rounds = 0
                            interval = ACTIVITY_FAST_INTERVAL
                        elif self.chase_enabled:
                            # chase demo: keep the fast cadence (no idle backoff)
                            # and step the lights unless live activity is showing
                            # (activity-blink outranks the demo)
                            self._activity_idle_rounds = 0
                            if not self._any_led_active():
                                if self._step_chase():
                                    notified = True
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

    def _blink_for_rate(self, rate):
        """Map bytes/s to blink timing in ms; None → steady on (speed-aware)."""
        if rate < SPEED_BLINK_LOW_RATE:
            return None
        if rate < SPEED_BLINK_HIGH_RATE:
            return (SPEED_BLINK_MID_ON_MS, SPEED_BLINK_MID_OFF_MS)
        return (SPEED_BLINK_FAST_ON_MS, SPEED_BLINK_FAST_OFF_MS)

    def _apply_speed_blink(self, led, delta, elapsed):
        """Map an activity rate (delta bytes over elapsed s) to blink params.

        None when speed blink is off, the LED has a user effect, or the
        window is zero (first tick) — shared by _check_network/_check_disks.
        """
        if not self.speed_blink_enabled or self.effects.get(led) is not None:
            return None
        if elapsed <= 0:
            return None
        return self._blink_for_rate(delta / elapsed)

    def _check_network(self):
        led = 'netdev'
        if self.modes.get(led) != 'auto':
            return False
        # H2: a cable unplug is not a block-uevent, so _net_iface would stay
        # stale until the next rescan — refresh NIC state every activity tick
        self._net_ifaces = list_net_ifaces()
        self._net_iface = detect_net_iface()
        if not self._net_iface:
            # all NICs down -> the netdev LED must go dark even when already
            # idle. Flip presence first so the dedup key changes and -off is
            # not skipped (same mechanism as _check_disks, H1).
            # T5: network-down alert — debounce consecutive ticks, then mark.
            if self.alert_cfg.get('enabled') and self.alert_cfg.get('network_down'):
                self._net_down_count += 1
                if (self._net_down_count >= ALERT_DEBOUNCE_CHECKS
                        and not self.alerts['network_down']):
                    self.alerts['network_down'] = True
                    self._last_applied.pop(led, None)
                    self._apply(led, 'auto', activity=False, alert=True)
            self.presence[led] = False
            if self.activity.get(led):
                self.activity[led] = False
                self._apply(led, 'auto', activity=False)
                return True
            key = self._apply_key(led, 'auto', False)
            if self._last_applied.get(led) != key:
                self._apply(led, 'auto', activity=False)
                return True
            return False
        # NIC(s) up — clear a pending network alert
        if self._net_down_count or self.alerts.get('network_down'):
            self._net_down_count = 0
            self.alerts['network_down'] = False
            self._last_applied.pop(led, None)
        self.presence[led] = True
        delta = 0
        for iface in self._net_ifaces:
            base = f'/sys/class/net/{iface}/statistics'
            rx = read_stats(f'{base}/rx_bytes')
            tx = read_stats(f'{base}/tx_bytes')
            # unseeded (new) iface contributes 0 on first sight — no false activity
            delta += (rx - self._prev_net_rx.get(iface, rx)) + (tx - self._prev_net_tx.get(iface, tx))
            self._prev_net_rx[iface] = rx
            self._prev_net_tx[iface] = tx
        active = delta >= ACTIVITY_IO_THRESHOLD
        now = time.monotonic()
        elapsed = (now - self._last_net_check) if self._last_net_check else 0.0
        self._last_net_check = now
        blink_params = self._apply_speed_blink(led, delta, elapsed)
        if active != self.activity.get(led):
            self.activity[led] = active
            self._apply(led, 'auto', activity=active, blink_params=blink_params)
            return True
        if self.speed_blink_enabled and self.effects.get(led) is None:
            key = self._apply_key(led, 'auto', active, None, None, None, blink_params)
            if key != self._last_applied.get(led):
                self._apply(led, 'auto', activity=active, blink_params=blink_params)
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
                # T5: disk failure alert — debounce consecutive absences so a
                # brief /sys hiccup does not fire a false alert, then blink red.
                if (self.alert_cfg.get('enabled')
                        and self.alert_cfg.get('disk_failure')
                        and self.modes.get(led) != 'off'):
                    count = self._alert_absent.get(slot, 0) + 1
                    self._alert_absent[slot] = count
                    if count >= ALERT_DEBOUNCE_CHECKS:
                        if not self.alerts['disk_lost'].get(led):
                            self.alerts['disk_lost'][led] = True
                            self._last_applied.pop(led, None)
                            self._apply(led, 'auto', activity=False, alert=True)
                            changed = True
                if self.activity.get(led) or self.presence.get(led):
                    self.activity[led] = False
                    self.presence[led] = False
                    self._apply(led, 'auto', activity=False)
                    changed = True
                self._prev_disk_io.pop(slot, None)
                self._last_disk_check.pop(slot, None)
                continue
            # disk present — clear any pending alert for this slot
            cleared = self._alert_absent.pop(slot, None) is not None
            if self.alerts['disk_lost'].pop(led, None) is not None:
                cleared = True
            if cleared:
                self._last_applied.pop(led, None)
                changed = True
            self.presence[led] = True
            io = read_disk_io(f'/sys/block/{dev}/stat')
            prev = self._prev_disk_io.get(slot, 0)
            delta = io - prev if io >= prev else 0
            active = delta >= ACTIVITY_IO_THRESHOLD
            self._prev_disk_io[slot] = io
            now = time.monotonic()
            elapsed = now - self._last_disk_check.get(slot, now)
            self._last_disk_check[slot] = now
            blink_params = self._apply_speed_blink(led, delta, elapsed)
            if active != self.activity.get(led):
                self.activity[led] = active
                self._apply(led, 'auto', activity=active, blink_params=blink_params)
                changed = True
                continue
            if self.speed_blink_enabled and self.effects.get(led) is None:
                key = self._apply_key(led, 'auto', active, None, None, None, blink_params)
                if key != self._last_applied.get(led):
                    self._apply(led, 'auto', activity=active, blink_params=blink_params)
                    changed = True
        return changed

    def get_live_status(self):
        """Lightweight snapshot for SSE / API."""
        with self._lock:
            return {
                'modes': dict(self.modes),
                'activity': dict(self.activity),
                'presence': dict(self.presence),
                'effects': dict(self.effects),
                'net_iface': self._net_iface,
                'net_ifaces': list(self._net_ifaces),
                'hotplug_monitor': self.needs_hotplug_monitor(),
                'monitor_tier': self._monitor_tier(),
                'activity_blink': self.activity_blink_enabled,
                'speed_blink': self.speed_blink_enabled,
                'night_active': self.night_active,
                'alerts': {'disk_lost': dict(self.alerts['disk_lost']),
                           'network_down': self.alerts['network_down']},
                'uevent_available': self._uevent.available,
                'rescan_count': self._rescan_count,
                'version': self.broadcaster.version,
            }

    def get_status(self):
        with self._lock:
            return {
                'modes': dict(self.modes),
                'activity': dict(self.activity),
                'presence': dict(self.presence),
                'effects': dict(self.effects),
                'settings': {k: dict(v) for k, v in self.settings.items() if k in self.leds},
                'disk_map': {str(k): v for k, v in self._disk_map.items()},
                'net_iface': self._net_iface,
                'net_ifaces': list(self._net_ifaces),
                'leds': self.leds,
                'presets': COLOR_PRESETS,
                'hotplug_monitor': self.needs_hotplug_monitor(),
                'monitor_tier': self._monitor_tier(),
                'activity_blink': self.activity_blink_enabled,
                'speed_blink': self.speed_blink_enabled,
                'night_schedule': dict(self.night_schedule),
                'night_active': self.night_active,
                'alerts': {'disk_lost': dict(self.alerts['disk_lost']),
                           'network_down': self.alerts['network_down']},
                'alert_cfg': dict(self.alert_cfg),
                'uevent_available': self._uevent.available,
                'rescan_count': self._rescan_count,
                'model': DXP4800_PLUS_PROFILE,
                'version': self.broadcaster.version,
            }

    def _notify(self):
        self.broadcaster.publish(self.get_live_status())
