#!/usr/bin/env python3
"""Unit tests for UGreenLedPilot v2.1."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from pilot_core import (
    map_disks_by_profile, disk_signature, net_signature,
    quick_hardware_signature, DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE,
    DXP4800_PLUS_PROFILE, PilotController, StatusBroadcaster,
)
from auth_manager import AuthManager


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


class TestDefaults(unittest.TestCase):
    def test_netdev_orange_power_white(self):
        self.assertEqual(DEFAULT_SETTINGS['power']['color'], WHITE)
        self.assertEqual(DEFAULT_SETTINGS['netdev']['color'], NETDEV_ORANGE)


class TestHotplugSignature(unittest.TestCase):
    def test_signatures_are_hashable(self):
        self.assertIsInstance(disk_signature(), tuple)
        self.assertIsInstance(net_signature(), tuple)
        self.assertIsInstance(quick_hardware_signature(), tuple)


class TestModeAwareHotplug(unittest.TestCase):
    def _make_ctrl(self):
        run = MagicMock(return_value=(True, '', ''))
        return PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run, '/tmp/test_state.json', '/tmp/test_settings.json',
            disk_count=2,
        )

    def test_no_hotplug_when_all_on(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'on', 'disk1': 'on', 'disk2': 'on'}
        self.assertFalse(ctrl.needs_hotplug_monitor())
        self.assertEqual(ctrl._monitor_tier(), 'sleep')

    def test_hotplug_when_any_auto(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        self.assertTrue(ctrl.needs_hotplug_monitor())

    def test_hotplug_tier_without_activity_blink(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'auto', 'disk1': 'off', 'disk2': 'off'}
        ctrl.activity_blink_enabled = False
        self.assertEqual(ctrl._monitor_tier(), 'hotplug')

    def test_activity_tier_with_blink(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'auto', 'disk1': 'off', 'disk2': 'off'}
        ctrl.activity_blink_enabled = True
        self.assertEqual(ctrl._monitor_tier(), 'activity')


class TestCliDedup(unittest.TestCase):
    def test_apply_skips_duplicate_cli(self):
        run = MagicMock(return_value=(True, '', ''))
        ctrl = PilotController(
            ['power'], run, '/tmp/t_state.json', '/tmp/t_settings.json', disk_count=0,
        )
        ctrl.modes['power'] = 'on'
        ok1, _ = ctrl._apply('power', 'on', False)
        ok2, _ = ctrl._apply('power', 'on', False)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(run.call_count, 1)


class TestBroadcaster(unittest.TestCase):
    def test_publish_notify(self):
        bc = StatusBroadcaster()
        q, ev = bc.subscribe()
        bc.publish({'modes': {'power': 'on'}})
        self.assertEqual(len(q), 1)
        self.assertTrue(ev.is_set())


class TestAuthSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        self.tmp.close()
        self.auth = AuthManager(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_csrf_on_session(self):
        _, csrf = self.auth.create_session()
        self.assertTrue(self.auth.validate_csrf(csrf))

    def test_password_max_length(self):
        ok, msg = self.auth.change_password('admin123', 'a' * 129)
        self.assertFalse(ok)
        self.assertIn('128', msg)


if __name__ == '__main__':
    unittest.main()
