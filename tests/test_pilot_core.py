#!/usr/bin/env python3
"""Unit tests for UGreenLedPilot — DXP4800 Plus."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from pilot_core import (
    map_disks_by_profile, disk_present, disk_signature, net_signature,
    DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE, DXP4800_PLUS_PROFILE,
    PilotController, VALID_MODES,
)
from auth_manager import AuthManager
import tempfile
import os


class TestDXP4800PlusMapping(unittest.TestCase):
    def test_hctl_mapping(self):
        devices = {
            'sda': {'hctl': '0:0:0:0'},
            'sdb': {'hctl': '1:0:0:0'},
            'sdc': {'hctl': '2:0:0:0'},
            'sdd': {'hctl': '3:0:0:0'},
        }
        result = map_disks_by_profile(
            devices,
            hctl_map=DXP4800_PLUS_PROFILE['hctl_map'],
            disk_count=4,
            mapping_method='hctl',
        )
        self.assertEqual(result, {1: 'sda', 2: 'sdb', 3: 'sdc', 4: 'sdd'})

    def test_empty_slot(self):
        devices = {'sda': {'hctl': '0:0:0:0'}, 'sdc': {'hctl': '2:0:0:0'}}
        result = map_disks_by_profile(
            devices,
            hctl_map=DXP4800_PLUS_PROFILE['hctl_map'],
            disk_count=4,
        )
        self.assertEqual(result[1], 'sda')
        self.assertEqual(result[3], 'sdc')
        self.assertNotIn(2, result)


class TestDefaults(unittest.TestCase):
    def test_netdev_orange_power_white(self):
        self.assertEqual(DEFAULT_SETTINGS['power']['color'], WHITE)
        self.assertEqual(DEFAULT_SETTINGS['netdev']['color'], NETDEV_ORANGE)
        self.assertEqual(DEFAULT_SETTINGS['disk1']['color'], WHITE)

    def test_profile_is_4_bay(self):
        self.assertEqual(DXP4800_PLUS_PROFILE['disk_count'], 4)
        self.assertEqual(len(DXP4800_PLUS_PROFILE['hctl_map']), 4)


class TestHotplugSignature(unittest.TestCase):
    def test_signatures_are_hashable(self):
        self.assertIsInstance(disk_signature(), tuple)
        self.assertIsInstance(net_signature(), tuple)


class TestModeAwareHotplug(unittest.TestCase):
    def _make_ctrl(self):
        run = MagicMock(return_value=(True, '', ''))
        return PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run, '/tmp/test_state.json', '/tmp/test_settings.json',
            disk_count=2,
        )

    def test_no_hotplug_when_all_off(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'off', 'disk2': 'off'}
        self.assertFalse(ctrl.needs_hotplug_monitor())

    def test_no_hotplug_when_all_on(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'on', 'disk1': 'on', 'disk2': 'on'}
        self.assertFalse(ctrl.needs_hotplug_monitor())

    def test_hotplug_when_any_auto(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        self.assertTrue(ctrl.needs_hotplug_monitor())

    def test_on_mode_ignores_presence(self):
        ctrl = self._make_ctrl()
        ctrl.modes['disk1'] = 'on'
        ctrl._disk_map = {1: None}
        ok, _ = ctrl._apply('disk1', 'on')
        self.assertTrue(ok)
        ctrl.run.assert_called()


class TestAuthSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        self.tmp.close()
        self.auth = AuthManager(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_password_hash_not_plaintext(self):
        self.assertNotIn('password', self.auth.cfg)
        self.assertIn('password_hash', self.auth.cfg)

    def test_csrf_token_on_session(self):
        session, csrf = self.auth.create_session()
        self.assertTrue(self.auth.validate_session(session))
        self.assertTrue(self.auth.validate_csrf(csrf))

    def test_lockout_after_failures(self):
        ip = '127.0.0.1'
        for _ in range(5):
            self.auth.record_failed_login(ip)
        self.assertTrue(self.auth.is_locked_out(ip))


if __name__ == '__main__':
    unittest.main()
