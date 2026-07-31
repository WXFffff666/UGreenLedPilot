"""Application bootstrap and shared runtime context."""

import os

from auth_manager import AuthManager
from pilot_core import (
    DXP4800_PLUS_PROFILE, LED_BASE, StatusBroadcaster,
    PilotController, detect_model, probe_leds, disk_count_from_leds,
    make_cli_runner,
)
from utils import load_json, save_json

APP_VERSION = '2.1.0'

_BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ugreen_leds_cli')
CLI = _BUNDLED if os.path.exists(_BUNDLED) else '/usr/local/bin/ugreen_leds_cli'
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'www')

PORT = int(os.environ.get('TRIM_SERVICE_PORT', 19580))
VAR = os.environ.get('TRIM_PKGVAR', '/tmp')
STATE_FILE = os.path.join(VAR, 'led_state.json')
SETTINGS_FILE = os.path.join(VAR, 'led_settings.json')
CONFIG_FILE = os.path.join(VAR, 'device_config.json')
AUTH_FILE = os.path.join(VAR, 'auth.json')


class AppContext:
    """Mutable app state shared by HTTP handlers."""

    def __init__(self):
        self.run = make_cli_runner(CLI)
        self.auth = AuthManager(AUTH_FILE)
        self.broadcaster = StatusBroadcaster()
        self.model_info = detect_model()
        self.led_statuses, self.probe_error = probe_leds(self.run)
        self.detected = disk_count_from_leds(self.led_statuses)
        self.hardware_modes = {led: d['mode'] for led, d in self.led_statuses.items()}
        self.initialized = os.path.exists(CONFIG_FILE)
        self.default_disk_count = DXP4800_PLUS_PROFILE['disk_count']
        self.cfg = load_json(CONFIG_FILE, self._default_config())
        if not self.initialized:
            save_json(CONFIG_FILE, self.cfg)
            self.initialized = True
        self.disk_count = self.cfg['disk_count']
        self.led_names = LED_BASE + [f'disk{i}' for i in range(1, self.disk_count + 1)]
        self.ctrl = PilotController(
            self.led_names, self.run, STATE_FILE, SETTINGS_FILE,
            ata_map=self.cfg.get('ata_map', DXP4800_PLUS_PROFILE['ata_map']),
            hctl_map=self.cfg.get('hctl_map', DXP4800_PLUS_PROFILE['hctl_map']),
            disk_count=self.disk_count,
            broadcaster=self.broadcaster,
            activity_blink=self.cfg.get('activity_blink', True),
        )
        self.ctrl.restore_state(hardware_modes=self.hardware_modes, apply_hardware=True)
        self.ctrl.start_monitor()

    def _default_config(self):
        return {
            'disk_count': self.default_disk_count,
            'model': DXP4800_PLUS_PROFILE['id'],
            'model_name': DXP4800_PLUS_PROFILE['name'],
            'product_name': self.model_info.get('product_name', ''),
            'auto_detected': True,
            'ata_map': DXP4800_PLUS_PROFILE['ata_map'],
            'hctl_map': DXP4800_PLUS_PROFILE['hctl_map'],
            'activity_blink': True,
        }

    def reset_controller(self):
        self.ctrl.stop_monitor()
        self.disk_count = self.default_disk_count
        self.led_names = LED_BASE + [f'disk{i}' for i in range(1, self.disk_count + 1)]
        self.cfg = self._default_config()
        save_json(CONFIG_FILE, self.cfg)
        self.ctrl = PilotController(
            self.led_names, self.run, STATE_FILE, SETTINGS_FILE,
            ata_map=self.cfg['ata_map'], hctl_map=self.cfg['hctl_map'],
            disk_count=self.disk_count, broadcaster=self.broadcaster,
            activity_blink=self.cfg.get('activity_blink', True),
        )
        self.ctrl.restore_state(apply_hardware=True)
        self.ctrl.start_monitor()
