#!/usr/bin/env python3
"""Unit tests for UGreenLedPilot v2.2."""

import hashlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from pilot_core import (
    map_disks_by_profile, disk_signature, net_signature, list_net_ifaces,
    DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE,
    DXP4800_PLUS_PROFILE, EFFECT_PRIORITY, PilotController, StatusBroadcaster,
    IDENTIFY_DURATION,
)
from auth_manager import AuthManager, PBKDF2_ITERATIONS
from http_handler import PilotHandler
from uevent_watcher import UeventWatcher
import app_context as app_context_mod


class _PilotTestCase(unittest.TestCase):
    """Per-test temp files + settings-timer cleanup.

    Every controller gets its own state/settings file in a fresh temp dir,
    and pending 0.8s save timers are cancelled on teardown — no shared
    /tmp files leak state between tests (F2/F4).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state_file = os.path.join(self._tmp.name, 'state.json')
        self._settings_file = os.path.join(self._tmp.name, 'settings.json')
        self._ctrls = []

    def tearDown(self):
        for ctrl in self._ctrls:
            if ctrl._settings_timer:
                ctrl._settings_timer.cancel()
        self._tmp.cleanup()

    def _make_ctrl(self, disk_count=2, **kwargs):
        run = MagicMock(return_value=(True, '', ''))
        ctrl = PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run, self._state_file, self._settings_file,
            disk_count=disk_count, **kwargs,
        )
        self._ctrls.append(ctrl)
        return ctrl


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


class TestModeAwareHotplug(_PilotTestCase):
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


class TestCliDedup(_PilotTestCase):
    def test_apply_skips_duplicate_cli(self):
        run = MagicMock(return_value=(True, '', ''))
        ctrl = PilotController(
            ['power'], run, self._state_file, self._settings_file, disk_count=0,
        )
        self._ctrls.append(ctrl)
        ctrl.modes['power'] = 'on'
        ok1, _ = ctrl._apply('power', 'on', False)
        ok2, _ = ctrl._apply('power', 'on', False)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(run.call_count, 1)


class TestAllOff(_PilotTestCase):
    """Batch all-off via single CLI call with full bookkeeping (E12)."""

    def test_all_off_single_cli_call_and_modes(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'on', 'disk1': 'on', 'disk2': 'on'}
        ok, msg = ctrl.all_off()
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        ctrl.run.assert_called_once_with('all', '-off')
        self.assertEqual(ctrl.get_status()['modes'],
                         {'power': 'off', 'netdev': 'off', 'disk1': 'off', 'disk2': 'off'})

    def test_all_off_then_single_on_not_deduped(self):
        ctrl = self._make_ctrl()
        ctrl.set_color('power', 255, 255, 255)  # deterministic color, ignores stale settings file
        ok, _ = ctrl.set_mode('power', 'on')  # seeds _last_applied with an ('on', ...) key
        self.assertTrue(ok)
        ctrl.run.reset_mock()
        ok, _ = ctrl.all_off()
        self.assertTrue(ok)
        ctrl.run.reset_mock()
        ok, _ = ctrl.set_mode('power', 'on')  # stale 'on' key must have been replaced
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with(
            'power', '-on', '-color', '255', '255', '255', '-brightness', '255')

    def test_all_off_failure_keeps_modes(self):
        ctrl = self._make_ctrl()
        ctrl.run.return_value = (False, '', 'cli boom')
        ctrl.modes = {'power': 'on', 'netdev': 'on', 'disk1': 'on', 'disk2': 'on'}
        ok, msg = ctrl.all_off()
        self.assertFalse(ok)
        self.assertEqual(msg, 'cli boom')
        self.assertEqual(ctrl.modes['power'], 'on')


class TestSetColorOffSemantics(_PilotTestCase):
    def test_set_color_off_persists_without_cli(self):
        ctrl = self._make_ctrl()
        ctrl.modes['power'] = 'off'
        before = ctrl.get_status()
        ok, msg = ctrl.set_color('power', 0, 255, 0)
        self.assertTrue(ok)
        self.assertEqual(ctrl.settings['power']['color'], [0, 255, 0])
        ctrl.run.assert_not_called()
        after = ctrl.get_status()
        self.assertEqual(after['modes']['power'], 'off')
        self.assertEqual(after['activity'], before['activity'])
        self.assertEqual(after['presence'], before['presence'])

    def test_set_color_off_then_mode_on_applies_new_color(self):
        ctrl = self._make_ctrl()
        ctrl.modes['power'] = 'off'
        ctrl.set_color('power', 0, 255, 0)
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_mode('power', 'on')
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with(
            'power', '-on', '-color', '0', '255', '0', '-brightness', '255')


class TestBreathEffect(_PilotTestCase):
    """F7a (Todo 16): hardware-native breath mode with color/brightness."""

    def test_breath_blue_applies_full_cli(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_color('power', 0, 0, 255)
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        ctrl.run.assert_called_once_with(
            'power', '-breath', '800', '1200',
            '-color', '0', '0', '255', '-brightness', '255')

    def test_set_effect_breath_triggers_cli(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.run.reset_mock()
        ctrl.set_effect('power', 'breath')
        ctrl.run.assert_called_once_with(
            'power', '-breath', '800', '1200',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_breath_timing_change_retriggers_cli(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        ctrl.run.reset_mock()
        ctrl.set_effect('power', 'breath', t_on=500, t_off=800)
        ctrl.run.assert_called_once_with(
            'power', '-breath', '500', '800',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_breath_to_off_calls_off_and_keeps_state(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_color('power', 0, 0, 255)
        ctrl.set_effect('power', 'breath', t_on=500, t_off=800)
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_mode('power', 'off')
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        ctrl.run.assert_called_once_with('power', '-off')
        self.assertEqual(ctrl.settings['power']['color'], [0, 0, 255])
        self.assertEqual(ctrl.settings['power']['breath_t_on'], 500)
        self.assertEqual(ctrl.settings['power']['breath_t_off'], 800)
        self.assertEqual(ctrl.effects['power'], 'breath')

    def test_effect_off_mode_stored_without_cli(self):
        ctrl = self._make_ctrl()
        ctrl.modes['power'] = 'off'
        ok, msg = ctrl.set_effect('power', 'breath', t_on=400, t_off=900)
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        ctrl.run.assert_not_called()
        self.assertEqual(ctrl.effects['power'], 'breath')
        self.assertEqual(ctrl.settings['power']['breath_t_on'], 400)
        self.assertEqual(ctrl.settings['power']['breath_t_off'], 900)
        # switching on later applies breath with the stored timing
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_mode('power', 'on')
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with(
            'power', '-breath', '400', '900',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_breath_dedup_same_params_skips_cli(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        ctrl.run.reset_mock()
        ok1, _ = ctrl._apply('power', 'on', False)
        ok2, _ = ctrl._apply('power', 'on', False)
        self.assertTrue(ok1 and ok2)
        ctrl.run.assert_not_called()

    def test_manual_blink_effect(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.run.reset_mock()
        ctrl.set_effect('power', 'manual-blink')
        ctrl.run.assert_called_once_with(
            'power', '-on', '-blink', '400', '400',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_invalid_effect_rejected(self):
        ctrl = self._make_ctrl()
        ok, msg = ctrl.set_effect('power', 'chase')
        self.assertFalse(ok)
        self.assertIn('Invalid effect', msg)


class TestManualBlinkEffect(_PilotTestCase):
    """F7b (Todo 17): manual blink as a standalone effect (400/400 ms)."""

    def test_manual_blink_cli_full_args(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'manual-blink')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_color('power', 0, 0, 255)
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with(
            'power', '-on', '-blink', '400', '400',
            '-color', '0', '0', '255', '-brightness', '255')

    def test_manual_blink_to_off_calls_off(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'manual-blink')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_mode('power', 'off')
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        ctrl.run.assert_called_once_with('power', '-off')


class TestDiskIoCounterReset(_PilotTestCase):
    """Disk IO counter reset must not be treated as activity (delta=0)."""

    def _make_ctrl(self):
        ctrl = super()._make_ctrl()
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.activity['disk1'] = False
        ctrl._prev_disk_io[1] = 100
        return ctrl

    def test_counter_reset_not_treated_as_activity(self):
        ctrl = self._make_ctrl()
        # First read matches prev (steady state), second read is a counter reset.
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', side_effect=[100, 5]):
            self.assertFalse(ctrl._check_disks())
            self.assertFalse(ctrl._check_disks())
        self.assertFalse(ctrl.activity['disk1'])
        self.assertEqual(ctrl._prev_disk_io[1], 5)
        for call in ctrl.run.call_args_list:
            self.assertNotIn('-blink', call.args)

    def test_normal_activity_still_triggers_blink(self):
        ctrl = self._make_ctrl()
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', side_effect=[100, 150]):
            ctrl._check_disks()
            self.assertTrue(ctrl._check_disks())
        self.assertTrue(ctrl.activity['disk1'])
        self.assertEqual(ctrl._prev_disk_io[1], 150)
        self.assertTrue(any('-blink' in call.args for call in ctrl.run.call_args_list))

    def test_absent_disk_clears_prev_io(self):
        ctrl = self._make_ctrl()
        with patch('pilot_core.disk_present', return_value=False):
            self.assertFalse(ctrl._check_disks())
        self.assertNotIn(1, ctrl._prev_disk_io)


class TestSpeedAwareBlink(_PilotTestCase):
    """F7b (Todo 17): rate-tiered blink — steady on / 400ms / 100ms by bytes-per-s."""

    def _make_ctrl(self, speed_blink=True):
        return super()._make_ctrl(speed_blink=speed_blink)

    def _seed_net(self, ctrl):
        ctrl.modes['netdev'] = 'auto'
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0']
        ctrl._prev_net_rx = {'eth0': 100000}
        ctrl._prev_net_tx = {'eth0': 0}
        ctrl._last_net_check = time.monotonic() - 1.0
        ctrl.activity['netdev'] = False

    def test_speed_blink_default_off(self):
        ctrl = self._make_ctrl(speed_blink=False)
        self.assertFalse(ctrl.speed_blink_enabled)

    def test_low_rate_below_100k_stays_on(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        # 30KB/s in 1s — below 100KB/s → steady on, no -blink
        with patch('pilot_core.read_stats', side_effect=[130000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-color', '255', '165', '0', '-brightness', '255')

    def test_mid_rate_uses_400_400(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        # 500KB/s in 1s → 400/400
        with patch('pilot_core.read_stats', side_effect=[600000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '400', '400',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_high_rate_uses_100_100(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        # 2.4MB/s in 1s → 100/100
        with patch('pilot_core.read_stats', side_effect=[2500000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '100', '100',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_rate_tier_change_retriggers_cli(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            ctrl._check_network()  # 500KB/s → 400/400
        ctrl.run.reset_mock()
        ctrl._last_net_check = time.monotonic() - 1.0
        with patch('pilot_core.read_stats', side_effect=[2500000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertTrue(ctrl._check_network())  # 2.4MB/s → 100/100
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '100', '100',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_same_rate_tier_deduped(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            ctrl._check_network()  # 500KB/s → 400/400
        ctrl.run.reset_mock()
        ctrl._last_net_check = time.monotonic() - 1.0
        with patch('pilot_core.read_stats', side_effect=[1150000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertFalse(ctrl._check_network())  # 550KB/s → still 400/400
        ctrl.run.assert_not_called()

    def test_speed_blink_disabled_keeps_fixed_blink(self):
        ctrl = self._make_ctrl(speed_blink=False)
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '80', '120',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_disk_rate_tier_uses_blink(self):
        ctrl = self._make_ctrl()
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.activity['disk1'] = False
        ctrl._prev_disk_io[1] = 100000
        ctrl._last_disk_check[1] = time.monotonic() - 1.0
        # 500KB/s in 1s → 400/400
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', side_effect=[600000]):
            self.assertTrue(ctrl._check_disks())
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-blink', '400', '400',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_set_speed_blink_disable_reapplies_fixed_blink(self):
        ctrl = self._make_ctrl()
        ctrl.modes['netdev'] = 'auto'
        ctrl.activity['netdev'] = True
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0']
        ctrl._apply('netdev', 'auto', activity=True, blink_params=(400, 400))
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_speed_blink(False)
        self.assertTrue(ok)
        self.assertFalse(ctrl.speed_blink_enabled)
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '80', '120',
            '-color', '255', '165', '0', '-brightness', '255')


class _FakeChaseClock:
    """Injectable monotonic clock: tests control time for chase stepping."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class _FakeMonotonic(_FakeChaseClock):
    """Fake time.monotonic — same call/advance shape as _FakeChaseClock."""


class _FakeLoopEvents:
    """Drives N iterations of _monitor_loop synchronously: stop fires after
    the Nth is_set() call and wake never blocks (no real sleeps/threads)."""

    def __init__(self, iterations):
        self._left = iterations

    def is_set(self):
        self._left -= 1
        return self._left < 0

    def wait(self, timeout=None):
        return False

    def clear(self):
        pass

    def set(self):
        pass


class TestChaseEffect(_PilotTestCase):
    """F7c (Todo 18): demo chase (running light) over the disk LEDs.

    chase is an independent demo effect: forces the monitor into the
    'activity' tier (no sleep/hotplug stall), keeps the fast interval by
    skipping the idle backoff, and yields to live activity (activity-blink
    outranks the demo, Todo 19 matrix).
    """

    def _make_ctrl(self):
        ctrl = super()._make_ctrl()
        ctrl._uevent = MagicMock()
        ctrl._uevent.is_running.return_value = False
        return ctrl

    def _all_off(self, ctrl):
        for led in ctrl.leds:
            ctrl.modes[led] = 'off'

    def test_chase_forces_activity_tier_even_when_all_off(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        self.assertEqual(ctrl._monitor_tier(), 'sleep')  # baseline: nothing to monitor
        ok, msg = ctrl.set_chase(True)
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        self.assertTrue(ctrl.chase_enabled)
        self.assertEqual(ctrl._chase_leds, ['disk1', 'disk2'])
        self.assertTrue(ctrl.needs_hotplug_monitor())
        self.assertEqual(ctrl._monitor_tier(), 'activity')
        # activity-blink off must not demote the chase tier (hotplug branch would stall it)
        ctrl.activity_blink_enabled = False
        self.assertEqual(ctrl._monitor_tier(), 'activity')

    def test_chase_off_restores_normal_tier(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        ctrl.set_chase(True)
        self.assertEqual(ctrl._monitor_tier(), 'activity')
        ok, msg = ctrl.set_chase(False)
        self.assertTrue(ok)
        self.assertFalse(ctrl.chase_enabled)
        self.assertEqual(ctrl._chase_leds, [])
        self.assertFalse(ctrl.needs_hotplug_monitor())
        self.assertEqual(ctrl._monitor_tier(), 'sleep')

    def test_chase_steps_disk_leds_in_order_with_min_interval(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        clock = _FakeChaseClock()
        ctrl._chase_clock = clock
        ctrl.set_chase(True)
        ctrl.run.reset_mock()

        # t=0: first tick lights disk1 (order starts at the first disk LED)
        ctrl._stop = _FakeLoopEvents(2)
        ctrl._wake_event = _FakeLoopEvents(2)
        ctrl._monitor_loop()
        self.assertEqual([c.args[0] for c in ctrl.run.call_args_list], ['disk1'])
        ctrl.run.reset_mock()

        # +0.3s (> 200ms step interval): light moves to disk2, disk1 goes off
        clock.advance(0.3)
        ctrl._stop = _FakeLoopEvents(2)
        ctrl._wake_event = _FakeLoopEvents(2)
        ctrl._monitor_loop()
        on = [c for c in ctrl.run.call_args_list if '-off' not in c.args]
        self.assertEqual([c.args[0] for c in on], ['disk2'])
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' in c.args
                            for c in ctrl.run.call_args_list))
        ctrl.run.reset_mock()

        # +0.1s (below 200ms): step is throttled — no CLI calls
        clock.advance(0.1)
        ctrl._stop = _FakeLoopEvents(2)
        ctrl._wake_event = _FakeLoopEvents(2)
        ctrl._monitor_loop()
        self.assertEqual(ctrl.run.call_count, 0)

        # +0.2s: interval elapsed -> chase wraps back to disk1
        clock.advance(0.2)
        ctrl._stop = _FakeLoopEvents(2)
        ctrl._wake_event = _FakeLoopEvents(2)
        ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' not in c.args
                            for c in ctrl.run.call_args_list))
        self.assertTrue(any(c.args[0] == 'disk2' and '-off' in c.args
                            for c in ctrl.run.call_args_list))

    def test_set_mode_stops_chase_and_restores_original_modes(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        clock = _FakeChaseClock()
        ctrl._chase_clock = clock
        ctrl.set_chase(True)
        ctrl.run.reset_mock()
        # let the chase light disk1 first
        ctrl._stop = _FakeLoopEvents(2)
        ctrl._wake_event = _FakeLoopEvents(2)
        ctrl._monitor_loop()
        ctrl.run.reset_mock()

        ok, msg = ctrl.set_mode('power', 'on')
        self.assertTrue(ok)
        self.assertFalse(ctrl.chase_enabled)
        self.assertEqual(ctrl._chase_leds, [])
        # the chase-lit disk1 is restored to its mode (off) — no stray LED stays on
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' in c.args
                            for c in ctrl.run.call_args_list))
        # power follows the user's new mode
        self.assertTrue(any(c.args[0] == 'power' and '-on' in c.args
                            for c in ctrl.run.call_args_list))
        # tier back to normal (all off -> sleep)
        self.assertEqual(ctrl._monitor_tier(), 'sleep')

    def test_chase_skips_idle_backoff(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        clock = _FakeChaseClock()
        ctrl._chase_clock = clock
        ctrl.set_chase(True)
        ctrl.run.reset_mock()
        ctrl._activity_idle_rounds = 30  # would otherwise trigger the 3s backoff
        ctrl._stop = _FakeLoopEvents(3)
        ctrl._wake_event = _FakeLoopEvents(3)
        ctrl._monitor_loop()
        # chase keeps the fast interval: idle rounds stay pinned at 0
        self.assertEqual(ctrl._activity_idle_rounds, 0)

    def test_activity_pauses_chase_and_activity_blink_takes_over(self):
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        ctrl.modes['netdev'] = 'auto'
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0']
        ctrl._prev_net_rx = {'eth0': 1000}
        ctrl._prev_net_tx = {'eth0': 2000}
        ctrl.activity['netdev'] = False
        clock = _FakeChaseClock()
        ctrl._chase_clock = clock
        ctrl.set_chase(True)
        ctrl.run.reset_mock()

        # sustained traffic -> netdev activity-blink; chase must not advance
        with patch('pilot_core.read_stats', side_effect=[1100, 2000, 1300, 2000, 1500, 2000]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            ctrl._stop = _FakeLoopEvents(3)
            ctrl._wake_event = _FakeLoopEvents(3)
            ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'netdev' and '-blink' in c.args
                            for c in ctrl.run.call_args_list))
        self.assertEqual(ctrl._chase_index, 0)
        self.assertFalse(any(c.args[0] == 'disk1' for c in ctrl.run.call_args_list))
        self.assertEqual(ctrl._activity_idle_rounds, 0)

        # traffic stops -> chase resumes stepping
        with patch('pilot_core.read_stats', side_effect=[1500, 2000, 1500, 2000]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            ctrl._stop = _FakeLoopEvents(2)
            ctrl._wake_event = _FakeLoopEvents(2)
            ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' not in c.args
                            for c in ctrl.run.call_args_list))

    def test_c3_chase_blinkoff_no_activity_blink(self):
        """C3: chase + activity-blink off + auto netdev with traffic must not
        -blink. The IO checks are gated by _has_auto_activity_targets (M2);
        only the chase demo steps."""
        ctrl = self._make_ctrl()
        self._all_off(ctrl)
        ctrl.modes['netdev'] = 'auto'
        ctrl.activity_blink_enabled = False
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0']
        ctrl._prev_net_rx = {'eth0': 1000}
        ctrl._prev_net_tx = {'eth0': 2000}
        ctrl.activity['netdev'] = False
        clock = _FakeChaseClock()
        ctrl._chase_clock = clock
        ctrl.set_chase(True)
        ctrl.run.reset_mock()

        # sustained traffic would previously blink netdev despite blink off
        with patch('pilot_core.read_stats', side_effect=[1100, 2000, 1300, 2000]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            ctrl._stop = _FakeLoopEvents(2)
            ctrl._wake_event = _FakeLoopEvents(2)
            ctrl._monitor_loop()
        self.assertFalse(any('-blink' in c.args for c in ctrl.run.call_args_list))
        # chase still steps despite the blink-off gate
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' not in c.args
                            for c in ctrl.run.call_args_list))


class TestIdentifyExpiry(_PilotTestCase):
    """C4: identify() self-expires — an off/steady LED is restored to its
    mode after IDENTIFY_DURATION instead of blinking forever."""

    def _make_ctrl(self):
        ctrl = super()._make_ctrl()
        ctrl._uevent = MagicMock()
        ctrl._uevent.is_running.return_value = False
        return ctrl

    def test_c4_identify_timeout_revert(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'off', 'disk1': 'off', 'disk2': 'off'}
        clock = _FakeMonotonic(now=100.0)
        with patch('pilot_core.time.monotonic', clock):
            ok, _ = ctrl.identify('disk1')  # deadline = 100 + IDENTIFY_DURATION
            self.assertTrue(ok)
            self.assertEqual(ctrl._identify_until, 100.0 + IDENTIFY_DURATION)
            self.assertEqual(ctrl._identify_led, 'disk1')
            ctrl.run.reset_mock()

            # before the deadline: no restore CLI
            ctrl._stop = _FakeLoopEvents(1)
            ctrl._wake_event = _FakeLoopEvents(1)
            ctrl._monitor_loop()
            self.assertFalse(any(c.args[0] == 'disk1' and '-off' in c.args
                                 for c in ctrl.run.call_args_list))

            # past the deadline: LED restored to its mode, timer reset
            clock.advance(IDENTIFY_DURATION + 1.0)
            ctrl.run.reset_mock()
            ctrl._stop = _FakeLoopEvents(1)
            ctrl._wake_event = _FakeLoopEvents(1)
            ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' in c.args
                            for c in ctrl.run.call_args_list))
        self.assertEqual(ctrl._identify_until, 0.0)


class TestEffectPriorityMatrix(_PilotTestCase):
    """F7d (Todo 19): off > on > manual-blink > breath > activity-blink > chase.

    User effects (manual-blink/breath) outrank auto activity-blink; a later
    lower-priority set_effect() call never demotes the active effect; effects
    persist in led_settings.json (additive-only); restore_state / reapply
    paths (set_activity_blink(False), _on_hardware_changed) keep user effects.
    """

    def _make_ctrl(self, disk_count=2):
        ctrl = super()._make_ctrl(disk_count=disk_count)
        ctrl._uevent = MagicMock()
        ctrl._uevent.is_running.return_value = False
        return ctrl

    def test_priority_matrix_ordering(self):
        self.assertEqual(EFFECT_PRIORITY['manual-blink'], 3)
        self.assertEqual(EFFECT_PRIORITY['breath'], 2)
        self.assertEqual(EFFECT_PRIORITY['activity-blink'], 1)
        self.assertEqual(EFFECT_PRIORITY['chase'], 0)
        self.assertGreater(EFFECT_PRIORITY['manual-blink'], EFFECT_PRIORITY['breath'])
        self.assertGreater(EFFECT_PRIORITY['breath'], EFFECT_PRIORITY['activity-blink'])
        self.assertGreater(EFFECT_PRIORITY['activity-blink'], EFFECT_PRIORITY['chase'])

    def test_manual_blink_rejects_lower_priority_breath(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'manual-blink')
        ok, msg = ctrl.set_effect('power', 'breath')
        self.assertFalse(ok)
        self.assertIn('priority', msg)
        self.assertEqual(ctrl.effects['power'], 'manual-blink')

    def test_breath_accepts_higher_priority_manual_blink(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        ok, msg = ctrl.set_effect('power', 'manual-blink')
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        self.assertEqual(ctrl.effects['power'], 'manual-blink')

    def test_same_effect_updates_params(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath', t_on=500, t_off=800)
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_effect('power', 'breath', t_on=300, t_off=600)
        self.assertTrue(ok)
        self.assertEqual(ctrl.effects['power'], 'breath')
        self.assertEqual(ctrl.settings['power']['breath_t_on'], 300)
        self.assertEqual(ctrl.settings['power']['breath_t_off'], 600)
        ctrl.run.assert_called_once_with(
            'power', '-breath', '300', '600',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_activity_does_not_override_manual_blink(self):
        ctrl = self._make_ctrl()
        ctrl.modes['disk1'] = 'auto'
        ctrl._disk_map = {1: 'sda'}
        ctrl.activity['disk1'] = False
        ctrl.set_effect('disk1', 'manual-blink')
        ctrl.run.reset_mock()
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', side_effect=[100, 500]):
            self.assertTrue(ctrl._check_disks())
        self.assertTrue(ctrl.activity['disk1'])
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-blink', '400', '400',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_activity_does_not_override_breath(self):
        ctrl = self._make_ctrl()
        ctrl.modes['disk1'] = 'auto'
        ctrl._disk_map = {1: 'sda'}
        ctrl.activity['disk1'] = False
        ctrl.set_effect('disk1', 'breath')
        ctrl.run.reset_mock()
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', side_effect=[100, 500]):
            self.assertTrue(ctrl._check_disks())
        self.assertTrue(ctrl.activity['disk1'])
        ctrl.run.assert_called_once_with(
            'disk1', '-breath', '800', '1200',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_set_activity_blink_false_keeps_manual_blink(self):
        ctrl = self._make_ctrl()
        ctrl.modes['disk1'] = 'auto'
        ctrl.activity['disk1'] = True
        ctrl.set_effect('disk1', 'manual-blink')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_activity_blink(False)
        self.assertTrue(ok)
        self.assertFalse(ctrl.activity_blink_enabled)
        self.assertFalse(ctrl.activity['disk1'])
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-blink', '400', '400',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_set_activity_blink_false_keeps_breath(self):
        ctrl = self._make_ctrl()
        ctrl.modes['disk1'] = 'auto'
        ctrl.activity['disk1'] = True
        ctrl.set_effect('disk1', 'breath')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_activity_blink(False)
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with(
            'disk1', '-breath', '800', '1200',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_off_mode_outranks_effect_and_keeps_setting(self):
        ctrl = self._make_ctrl()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'manual-blink')
        ctrl.run.reset_mock()
        ok, msg = ctrl.set_mode('power', 'off')
        self.assertTrue(ok)
        ctrl.run.assert_called_once_with('power', '-off')
        self.assertEqual(ctrl.effects['power'], 'manual-blink')

    def test_hardware_changed_reapply_keeps_effect(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        ctrl._disk_map = {1: 'sda'}
        ctrl.set_effect('disk1', 'manual-blink')
        ctrl.run.reset_mock()
        # simulate LED state lost on remap -> reapply must use the effect,
        # never demote it to solid-on (activity-blink level)
        ctrl._last_applied.pop('disk1', None)
        with patch('pilot_core.detect_disks', return_value={1: 'sda'}), \
                patch('pilot_core.detect_net_iface', return_value=None), \
                patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.disk_present', return_value=True):
            ctrl._on_hardware_changed()
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-blink', '400', '400',
            '-color', '255', '255', '255', '-brightness', '255')

    def test_effect_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            settings_file = os.path.join(d, 'led_settings.json')
            state_file = os.path.join(d, 'led_state.json')
            run = MagicMock(return_value=(True, '', ''))
            ctrl = PilotController(['power'], run, state_file, settings_file, disk_count=0)
            self._ctrls.append(ctrl)
            ctrl.set_mode('power', 'on')
            ctrl.set_effect('power', 'breath', t_on=500, t_off=800)
            ctrl._flush_settings()
            # simulate restart: rebuild controller from the same files
            run2 = MagicMock(return_value=(True, '', ''))
            ctrl2 = PilotController(['power'], run2, state_file, settings_file, disk_count=0)
            self._ctrls.append(ctrl2)
            with patch('pilot_core.detect_disks', return_value={}), \
                    patch('pilot_core.detect_net_iface', return_value=None), \
                    patch('pilot_core.list_net_ifaces', return_value=[]):
                ctrl2.restore_state(apply_hardware=True)
            self.assertEqual(ctrl2.effects['power'], 'breath')
            self.assertEqual(ctrl2.settings['power']['breath_t_on'], 500)
            self.assertEqual(ctrl2.settings['power']['breath_t_off'], 800)
            self.assertTrue(any(
                c.args[0] == 'power' and '-breath' in c.args and '500' in c.args
                for c in run2.call_args_list))

    def test_v210_settings_without_effects_key_loads(self):
        with tempfile.TemporaryDirectory() as d:
            settings_file = os.path.join(d, 'led_settings.json')
            with open(settings_file, 'w') as f:
                json.dump({'power': {'color': [1, 2, 3], 'brightness': 128}}, f)
            ctrl = PilotController(
                ['power'], MagicMock(return_value=(True, '', '')),
                os.path.join(d, 'led_state.json'), settings_file, disk_count=0)
            self.assertEqual(ctrl.effects, {})
            self.assertEqual(ctrl.settings['power']['brightness'], 128)
            self.assertEqual(ctrl.settings['power']['color'], [1, 2, 3])

    def test_flush_settings_is_additive(self):
        with tempfile.TemporaryDirectory() as d:
            settings_file = os.path.join(d, 'led_settings.json')
            with open(settings_file, 'w') as f:
                json.dump({'custom_key': 'keep-me'}, f)
            ctrl = PilotController(
                ['power'], MagicMock(return_value=(True, '', '')),
                os.path.join(d, 'led_state.json'), settings_file, disk_count=0)
            self._ctrls.append(ctrl)
            ctrl.set_effect('power', 'manual-blink')
            ctrl._flush_settings()
            with open(settings_file) as f:
                saved = json.load(f)
            self.assertEqual(saved['custom_key'], 'keep-me')
            self.assertEqual(saved['effects'], {'power': 'manual-blink'})

    def test_r6_stale_timer_epoch(self):
        """R6: a settings-save timer captured before a stop must not write.

        stop_monitor bumps _settings_epoch so a pending 0.8s timer from the
        old controller (which captured the old epoch) skips its flush —
        otherwise it would overwrite the settings file with stale state
        after a reset/rebuild.
        """
        ctrl = self._make_ctrl()
        ctrl.set_color('power', 10, 20, 30)  # dirty + schedules a save (epoch 0)
        stale_epoch = ctrl._settings_epoch
        ctrl._settings_epoch += 1  # what stop_monitor does before a rebuild
        with patch('pilot_core.save_json') as save_mock:
            ctrl._flush_settings(stale_epoch)  # stale timer fires
            save_mock.assert_not_called()
        self.assertTrue(ctrl._settings_dirty)  # pending change not dropped
        ctrl._flush_settings()  # a fresh flush still persists
        with open(self._settings_file) as f:
            saved = json.load(f)
        self.assertEqual(saved['power']['color'], [10, 20, 30])


class TestMultiNicMonitoring(_PilotTestCase):
    """E13/F2: aggregate rx/tx across all NICs (DXP4800 Plus dual 2.5G)."""

    def test_any_nic_traffic_triggers_active(self):
        ctrl = self._make_ctrl()
        ctrl.modes['netdev'] = 'auto'
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0', 'eth1']
        ctrl._prev_net_rx = {'eth0': 1000, 'eth1': 500}
        ctrl._prev_net_tx = {'eth0': 2000, 'eth1': 600}
        ctrl.activity['netdev'] = False
        # eth0 idle; only eth1 sees traffic (rx 500 -> 550)
        with patch('pilot_core.read_stats', side_effect=[1000, 2000, 550, 600]), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0', 'eth1']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'):
            changed = ctrl._check_network()
        self.assertTrue(changed)
        self.assertTrue(ctrl.activity['netdev'])
        self.assertTrue(any('-blink' in call.args for call in ctrl.run.call_args_list))
        self.assertEqual(ctrl._prev_net_rx, {'eth0': 1000, 'eth1': 550})

    def test_all_nics_down_activity_false_and_led_off(self):
        ctrl = self._make_ctrl()
        ctrl.modes['netdev'] = 'auto'
        ctrl._net_iface = None
        ctrl._net_ifaces = []
        ctrl.activity['netdev'] = True
        # _check_network() re-detects NICs every tick; pin them down so the
        # "all NICs down" branch is exercised regardless of the host's real
        # network state (CI runners have a live eth0 which previously leaked
        # MagicMock stats into the `delta >= threshold` comparison).
        with patch('pilot_core.read_stats') as mock_read, \
                patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.detect_net_iface', return_value=None):
            changed = ctrl._check_network()
        self.assertTrue(changed)
        self.assertFalse(ctrl.activity['netdev'])
        self.assertTrue(any('-off' in call.args for call in ctrl.run.call_args_list))
        mock_read.assert_not_called()

    def test_net_iface_compat_first_and_net_ifaces_all(self):
        ctrl = self._make_ctrl()
        with patch('pilot_core.detect_net_iface', return_value='eth0'), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0', 'eth1']), \
                patch('pilot_core.detect_disks', return_value={}):
            ctrl.restore_state(apply_hardware=False)
        self.assertEqual(ctrl._net_iface, 'eth0')
        self.assertEqual(ctrl._net_ifaces, ['eth0', 'eth1'])
        status = ctrl.get_status()
        self.assertEqual(status['net_iface'], 'eth0')
        self.assertEqual(status['net_ifaces'], ['eth0', 'eth1'])
        self.assertEqual(ctrl.get_live_status()['net_ifaces'], ['eth0', 'eth1'])

    def test_start_monitor_seeds_all_ifaces(self):
        ctrl = self._make_ctrl()
        with patch('pilot_core.detect_net_iface', return_value='eth0'), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0', 'eth1']), \
                patch('pilot_core.detect_disks', return_value={}), \
                patch('pilot_core.read_stats', side_effect=[10, 20, 30, 40]):
            ctrl.start_monitor()
            ctrl.stop_monitor()
        self.assertEqual(ctrl._net_iface, 'eth0')
        self.assertEqual(ctrl._net_ifaces, ['eth0', 'eth1'])
        self.assertEqual(ctrl._prev_net_rx, {'eth0': 10, 'eth1': 30})
        self.assertEqual(ctrl._prev_net_tx, {'eth0': 20, 'eth1': 40})

    def test_net_signature_aggregates_all_ifaces(self):
        eth0 = mock_open(read_data='1')
        eth1 = mock_open(read_data='0')
        with patch('pilot_core.list_net_ifaces', return_value=['eth0', 'eth1']), \
                patch('pilot_core.open', side_effect=[eth0.return_value, eth1.return_value]):
            sig = net_signature()
        self.assertEqual(sig, (('eth0', '1'), ('eth1', '0')))
        self.assertIsInstance(sig, tuple)

    def test_hotplug_unplug_one_nic_rescan_no_false_activity(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'auto', 'disk1': 'off', 'disk2': 'off'}
        ctrl.activity['netdev'] = False
        ctrl._net_ifaces = ['eth0', 'eth1']
        ctrl._net_iface = 'eth0'
        ctrl._prev_net_rx = {'eth0': 1000, 'eth1': 500}
        ctrl._prev_net_tx = {'eth0': 2000, 'eth1': 600}
        ctrl._net_sig = (('eth0', '1'), ('eth1', '1'))
        ctrl._disk_sig = ()
        # eth1 unplugged -> carrier gone -> net signature changes -> rescan
        with patch('pilot_core.detect_disks', return_value={}), \
                patch('pilot_core.disk_signature', return_value=()), \
                patch('pilot_core.net_signature', return_value=(('eth0', '1'),)), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'), \
                patch('pilot_core.read_stats', side_effect=[1100, 2100, 1100, 2100]):
            self.assertTrue(ctrl._maybe_rescan())
            # reseeded prev counters -> stale eth1 counters must not cause activity
            self.assertFalse(ctrl._check_network())
        self.assertFalse(ctrl.activity['netdev'])
        self.assertEqual(ctrl._net_ifaces, ['eth0'])
        self.assertNotIn('eth1', ctrl._prev_net_rx)
        self.assertFalse(any('-blink' in call.args for call in ctrl.run.call_args_list))


class TestH1PresenceDedup(_PilotTestCase):
    """H1: the dedup key must encode disk presence.

    Otherwise a pull/replug while the disk is idle (steady-on) produces the
    identical key as the idle state, the dedup skips _apply, and the CLI
    -off/-on transition never reaches the hardware (LED stays lit / dark).
    """

    def test_h1_remove_while_idle_off(self):
        ctrl = self._make_ctrl()
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.activity['disk1'] = False
        ctrl.presence['disk1'] = True
        # Disk present + idle -> steady-on applied (seeds _last_applied)
        with patch('pilot_core.disk_present', return_value=True):
            ctrl._apply('disk1', 'auto', activity=False)
        ctrl.run.reset_mock()
        # Disk removed while idle -> -off must NOT be deduped away
        with patch('pilot_core.disk_present', return_value=False):
            self.assertTrue(ctrl._check_disks())
        ctrl.run.assert_called_once_with('disk1', '-off')
        self.assertFalse(ctrl.presence['disk1'])

    def test_h1_replug_while_idle_on(self):
        ctrl = self._make_ctrl()
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        ctrl.activity['disk1'] = False
        ctrl.presence['disk1'] = False
        # Disk absent + idle -> -off applied (seeds _last_applied with absent key)
        with patch('pilot_core.disk_present', return_value=False):
            ctrl._apply('disk1', 'auto', activity=False)
        ctrl.run.reset_mock()
        # Disk re-plugged while idle -> steady -on must be re-issued
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.detect_disks', return_value={1: 'sda'}), \
                patch('pilot_core.detect_net_iface', return_value=None), \
                patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.read_disk_io', return_value=0):
            ctrl._on_hardware_changed()
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-color', '255', '255', '255', '-brightness', '255')


class TestH2AllNicsDown(_PilotTestCase):
    """H2: all NICs down must turn the netdev LED off even when idle.

    The old _check_network returned early (no -off) whenever _net_iface was
    None AND activity was already False, so a cable unplug while the netdev
    LED was steady-on left it lit forever.
    """

    def test_h2_all_nics_down_netdev_off(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'auto', 'disk1': 'off', 'disk2': 'off'}
        ctrl._net_iface = 'eth0'
        ctrl._net_ifaces = ['eth0']
        ctrl.activity['netdev'] = False
        ctrl.presence['netdev'] = True
        # NICs up + idle -> steady-on applied (seeds _last_applied)
        ctrl._apply('netdev', 'auto', activity=False)
        # All NICs go down (cable unplug); activity is already False
        ctrl._net_iface = None
        ctrl.run.reset_mock()
        with patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.detect_net_iface', return_value=None):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with('netdev', '-off')
        self.assertFalse(ctrl.activity['netdev'])


class TestC5ClockSeeded(_PilotTestCase):
    """C5: speed-blink clocks must be seeded at monitor start / hotplug.

    Unseeded _last_net_check / _last_disk_check made the first activity tick
    compute elapsed=0 -> blink_params=None -> a spurious steady-on flash, and
    left the clocks stale after re-enabling speed-blink.
    """

    def test_c5_clock_seeded(self):
        ctrl = self._make_ctrl()
        with patch('pilot_core.detect_disks', return_value={1: 'sda'}), \
                patch('pilot_core.detect_net_iface', return_value='eth0'), \
                patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.read_stats', side_effect=[10, 20]), \
                patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', return_value=0):
            ctrl.start_monitor()
            ctrl.stop_monitor()
        self.assertIsNotNone(ctrl._last_net_check)
        self.assertIn(1, ctrl._last_disk_check)


class TestP1SingleRemap(_PilotTestCase):
    """P1: _on_hardware_changed must refresh _disk_sig/_net_sig.

    Otherwise the 120s fallback scan (_maybe_rescan) compares against the
    stale pre-hotplug signature and triggers a second full remap.
    """

    def test_p1_single_remap_per_hotplug(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        ctrl._disk_map = {1: 'sda'}
        with patch('pilot_core.detect_disks', return_value={1: 'sdb'}), \
                patch('pilot_core.detect_net_iface', return_value=None), \
                patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', return_value=0), \
                patch('pilot_core.disk_signature', return_value=(('sdb', '0:0:0:0', 'SN'),)), \
                patch('pilot_core.net_signature', return_value=()):
            # uevent-driven remap (sda -> sdb) — exactly one remap
            ctrl._on_hardware_changed()
            self.assertEqual(ctrl._rescan_count, 1)
            # the fallback scan must see the post-remap signature: no second remap
            self.assertFalse(ctrl._maybe_rescan())
            self.assertEqual(ctrl._rescan_count, 1)


class TestR1ProbeOutsideLock(_PilotTestCase):
    """R1: hardware probes (lsblk subprocess) must not run under _lock.

    A hung lsblk (5s timeout) under the lock would block every UI mutation
    (set_color/set_mode/...). force_remap is the API path that used to hold
    the lock across _on_hardware_changed's probes.
    """

    def test_r1_detect_outside_lock(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        ctrl._disk_map = {1: 'sda'}
        lock_held_at_probe = []

        def probing_detect(*args, **kwargs):
            # RLock._is_owned(): True when the calling thread holds the lock
            lock_held_at_probe.append(ctrl._lock._is_owned())
            return {1: 'sda'}

        with patch('pilot_core.detect_disks', side_effect=probing_detect), \
                patch('pilot_core.detect_net_iface', return_value=None), \
                patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', return_value=0), \
                patch('pilot_core.disk_signature', return_value=()), \
                patch('pilot_core.net_signature', return_value=()):
            ctrl.force_remap()
        self.assertEqual(lock_held_at_probe, [False])
        self.assertEqual(ctrl._disk_map, {1: 'sda'})


class TestC6BlinkParamsPreserved(_PilotTestCase):
    """C6: set_color/set_brightness/set_effect during a speed-aware blink
    must keep the blink rhythm.

    Dropping blink_params made _apply fall into the speed-blink branch and
    flash the LED steady-on until the next activity tick.
    """

    def test_c6_blink_params_preserved(self):
        ctrl = self._make_ctrl(speed_blink=True)
        ctrl.modes = {'power': 'off', 'netdev': 'off', 'disk1': 'auto', 'disk2': 'off'}
        ctrl.activity['disk1'] = True
        ctrl._disk_map = {1: 'sda'}
        with patch('pilot_core.disk_present', return_value=True):
            # speed-aware blink in progress (fast tier 100/100)
            ctrl._apply('disk1', 'auto', activity=True, blink_params=(100, 100))
        ctrl.run.reset_mock()
        with patch('pilot_core.disk_present', return_value=True), \
                patch.object(ctrl, '_apply', wraps=ctrl._apply) as spy:
            ok, _ = ctrl.set_color('disk1', 10, 20, 30)
        self.assertTrue(ok)
        self.assertEqual(spy.call_args.kwargs['blink_params'], (100, 100))
        ctrl.run.assert_called_once_with(
            'disk1', '-on', '-blink', '100', '100',
            '-color', '10', '20', '30', '-brightness', '255')


class TestBroadcaster(unittest.TestCase):
    def test_publish_notify(self):
        bc = StatusBroadcaster()
        q, ev = bc.subscribe()
        bc.publish({'modes': {'power': 'on'}})
        self.assertEqual(len(q), 1)
        self.assertTrue(ev.is_set())


class TestM7StatusVersion(_PilotTestCase):
    """M7-server: get_status/get_live_status expose the broadcaster version."""

    def test_m7_status_version_present(self):
        ctrl = self._make_ctrl()
        self.assertEqual(ctrl.get_live_status()['version'], 0)
        self.assertEqual(ctrl.get_status()['version'], 0)
        ctrl.broadcaster.publish({'modes': dict(ctrl.modes)})
        live1 = ctrl.get_live_status()['version']
        ctrl.broadcaster.publish({'modes': dict(ctrl.modes)})
        full2 = ctrl.get_status()['version']
        self.assertGreater(live1, 0)
        self.assertGreater(full2, live1)


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


class TestAuthUsername(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.dir, 'auth.json')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make_handler(self):
        handler = PilotHandler.__new__(PilotHandler)
        handler.app = MagicMock()
        handler.app.auth = self.auth
        handler.client_address = ('127.0.0.1', 5555)
        handler.headers = {}
        handler.rfile = MagicMock()
        handler.wfile = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _login_response(self, payload):
        handler = self._make_handler()
        handler._login(payload)
        status = handler.send_response.call_args_list[0][0][0]
        body = json.loads(handler.wfile.write.call_args[0][0].decode())
        return status, body

    def test_fresh_install_default_admin(self):
        auth = AuthManager(self.auth_file)
        self.assertEqual(auth.cfg['username'], 'admin')
        with open(self.auth_file) as f:
            self.assertEqual(json.load(f)['username'], 'admin')

    def test_v210_migration_adds_admin_keeps_hash(self):
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            'sha256', 'oldpass'.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
        with open(self.auth_file, 'w') as f:
            json.dump({
                'password_hash': digest,
                'password_salt': salt,
                'must_change_password': True,
                'session': '',
                'csrf_token': '',
                'session_created': 0,
            }, f)
        auth = AuthManager(self.auth_file)
        self.assertEqual(auth.cfg['username'], 'admin')
        self.assertEqual(auth.cfg['password_hash'], digest)
        self.assertEqual(auth.cfg['password_salt'], salt)
        self.assertTrue(auth.must_change_password())
        self.assertTrue(auth.verify_credentials('admin', 'oldpass'))

    def test_verify_wrong_username_fails(self):
        self.auth = AuthManager(self.auth_file)
        self.assertFalse(self.auth.verify_credentials('bad_user', 'admin123'))

    def test_verify_wrong_password_fails(self):
        self.auth = AuthManager(self.auth_file)
        self.assertFalse(self.auth.verify_credentials('admin', 'wrongpass'))

    def test_verify_empty_username_fails(self):
        self.auth = AuthManager(self.auth_file)
        self.assertFalse(self.auth.verify_credentials('', 'admin123'))

    def test_verify_anti_enumeration_indistinguishable(self):
        self.auth = AuthManager(self.auth_file)
        bad_user = self.auth.verify_credentials('bad_user', 'bad_pw')
        bad_pass = self.auth.verify_credentials('admin', 'bad_pw')
        self.assertFalse(bad_user)
        self.assertEqual(bad_user, bad_pass)

    def test_login_wrong_username_401(self):
        self.auth = AuthManager(self.auth_file)
        status, body = self._login_response({'username': 'bad_user', 'password': 'admin123'})
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')

    def test_login_missing_username_401(self):
        self.auth = AuthManager(self.auth_file)
        status, body = self._login_response({'password': 'admin123'})
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')

    def test_login_wrong_password_401_same_message(self):
        self.auth = AuthManager(self.auth_file)
        status, body = self._login_response({'username': 'admin', 'password': 'wrong'})
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')

    def test_login_success_admin_200(self):
        self.auth = AuthManager(self.auth_file)
        status, body = self._login_response({'username': 'admin', 'password': 'admin123'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertTrue(body['csrf_token'])


@unittest.skipIf(os.name == 'nt', 'POSIX file permission semantics')
class TestAuthFilePerms(unittest.TestCase):
    """auth.json must be chmod 0600 after every write (E-ADD-2)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.dir, 'auth.json')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_chmod_0600_called_on_init(self):
        with patch('auth_manager.os.chmod') as mock_chmod:
            AuthManager(self.auth_file)
        self.assertTrue(mock_chmod.called)
        mock_chmod.assert_any_call(self.auth_file, 0o600)

    def test_chmod_0600_called_after_password_change(self):
        with patch('auth_manager.os.chmod') as mock_chmod:
            auth = AuthManager(self.auth_file)
            before = mock_chmod.call_count
            ok, _ = auth.change_password('admin123', 'newpass123456')
            self.assertTrue(ok)
            self.assertGreater(mock_chmod.call_count, before)
            mock_chmod.assert_any_call(self.auth_file, 0o600)

    def test_posix_mode_600(self):
        AuthManager(self.auth_file)
        mode = os.stat(self.auth_file).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class _FakeNetlinkSocket:
    """Mimics a netlink socket with 3s timeout: recv blocks briefly, then raises socket.timeout."""

    def __init__(self):
        self.closed = False

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def recv(self, *args):
        time.sleep(0.2)
        raise socket.timeout('timeout')

    def close(self):
        self.closed = True


class TestUeventWatcher(unittest.TestCase):
    """UeventWatcher start/stop thread lifecycle (E9: stop->start must not double-thread)."""

    def _wait_running(self, watcher, wait=2.0):
        deadline = time.time() + wait
        while time.time() < deadline:
            if watcher.is_running():
                return True
            time.sleep(0.01)
        return False

    def _watcher_thread_count(self):
        return len([t for t in threading.enumerate() if t.name == 'uevent-watcher'])

    def test_stop_then_start_no_duplicate_thread(self):
        with patch('uevent_watcher.socket') as mock_sock:
            mock_sock.timeout = socket.timeout  # except-clause must see the real class
            fake = _FakeNetlinkSocket()
            mock_sock.socket.return_value = fake
            watcher = UeventWatcher(callback=MagicMock())
            watcher.start()
            self.assertTrue(self._wait_running(watcher))
            old_thread = watcher._thread
            watcher.stop()
            # stop() must join the old thread and drop its reference (bug: returns early)
            self.assertIsNone(watcher._thread)
            self.assertFalse(old_thread.is_alive())
            self.assertFalse(watcher.is_running())
            # immediate restart: old thread already joined -> no double netlink socket
            watcher.start()
            self.assertTrue(self._wait_running(watcher))
            self.assertIsNot(watcher._thread, old_thread)
            self.assertEqual(self._watcher_thread_count(), 1)
            self.assertEqual(mock_sock.socket.call_count, 2)  # one socket per start
            watcher.stop()
            self.assertIsNone(watcher._thread)
            self.assertTrue(fake.closed)

    def test_netlink_unavailable_falls_back(self):
        with patch('uevent_watcher.socket') as mock_sock:
            mock_sock.socket.side_effect = OSError('netlink unavailable')
            watcher = UeventWatcher(callback=MagicMock())
            watcher.start()  # must not raise
            self.assertFalse(self._wait_running(watcher))
            self.assertFalse(watcher.available)
            watcher.stop()  # safe even though the thread already exited
            self.assertIsNone(watcher._thread)
            self.assertFalse(watcher.is_running())


class TestAppContextAsyncProbe(unittest.TestCase):
    """AppContext must not block startup on LED probing, and all CLI
    (I2C) access must be serialized by a shared lock (probe thread,
    restore_state and set_mode never interleave on the bus)."""

    def _make(self, probe_result=({}, ''), probe_impl=None):
        """Build an AppContext with all heavy deps mocked.

        probe_impl replaces probe_leds: either a MagicMock (then
        probe_result is returned) or a plain function run on the daemon
        probe thread. Returns (ctx, fake_ctrl, runner, probe_mock).
        """
        runner = MagicMock(return_value=(True, '', ''))
        fake_ctrl = MagicMock()
        if probe_impl is None:
            probe_impl = MagicMock(return_value=probe_result)
        probe_patcher = patch.object(app_context_mod, 'probe_leds',
                                     side_effect=probe_impl)
        probe_mock = probe_patcher.start()
        self.addCleanup(probe_patcher.stop)
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patchers = [
            patch.object(app_context_mod, 'make_cli_runner', return_value=runner),
            patch.object(app_context_mod, 'AuthManager'),
            patch.object(app_context_mod, 'StatusBroadcaster'),
            patch.object(app_context_mod, 'detect_model', return_value={}),
            patch.object(app_context_mod, 'PilotController', return_value=fake_ctrl),
            patch.object(app_context_mod, 'load_json',
                         side_effect=lambda path, default: default),
            patch.object(app_context_mod, 'save_json'),
            patch.object(app_context_mod, 'CONFIG_FILE',
                         os.path.join(tmpdir.name, 'no_config.json')),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return app_context_mod.AppContext(), fake_ctrl, runner, probe_mock

    @staticmethod
    def _wait_probe(ctx, wait=2.0):
        deadline = time.time() + wait
        while ctx.probe_pending and time.time() < deadline:
            time.sleep(0.01)
        return not ctx.probe_pending

    def test_init_returns_before_slow_probe_finishes(self):
        release = threading.Event()

        def slow_probe(run):
            release.wait(5)
            return {'disk1': {'mode': 'on'}}, ''

        start = time.monotonic()
        ctx, fake_ctrl, runner, probe_mock = self._make(probe_impl=slow_probe)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0,
                        f'__init__ took {elapsed:.2f}s — probe blocked startup')
        # state stays empty + probe_pending while probing
        self.assertTrue(ctx.probe_pending)
        self.assertEqual(ctx.led_statuses, {})
        self.assertEqual(ctx.hardware_modes, {})
        self.assertEqual(ctx.detected, 0)
        self.assertEqual(ctx.probe_error, '')
        # probe not ready -> restore falls back to saved/auto (empty modes)
        fake_ctrl.restore_state.assert_called_once_with(
            hardware_modes={}, apply_hardware=True)
        release.set()
        self.assertTrue(self._wait_probe(ctx), 'probe should finish after release')
        self.assertFalse(ctx.probe_pending)
        self.assertEqual(ctx.led_statuses['disk1']['mode'], 'on')
        fake_ctrl.start_monitor.assert_called_once_with()

    def test_probe_results_populate_state(self):
        statuses = {
            'power': {'mode': 'auto'}, 'netdev': {'mode': 'auto'},
            'disk1': {'mode': 'on'}, 'disk2': {'mode': 'off'},
        }
        ctx, fake_ctrl, runner, probe_mock = self._make(probe_result=(statuses, ''))
        self.assertTrue(self._wait_probe(ctx))
        self.assertFalse(ctx.probe_pending)
        self.assertEqual(ctx.detected, 2)
        self.assertEqual(ctx.hardware_modes, {
            'power': 'auto', 'netdev': 'auto',
            'disk1': 'on', 'disk2': 'off',
        })
        self.assertEqual(ctx.probe_error, '')
        # probe ran on the background thread with the locked runner
        self.assertEqual(probe_mock.call_count, 1)
        self.assertIs(probe_mock.call_args.args[0], ctx.run)

    def test_probe_failure_reported_and_app_continues(self):
        ctx, fake_ctrl, runner, probe_mock = self._make(
            probe_result=({}, 'CLI not found'))
        self.assertTrue(self._wait_probe(ctx))
        self.assertFalse(ctx.probe_pending)
        self.assertEqual(ctx.probe_error, 'CLI not found')
        self.assertEqual(ctx.detected, 0)
        fake_ctrl.start_monitor.assert_called_once_with()

    def test_probe_exception_sets_probe_error(self):
        def boom(run):
            raise RuntimeError('i2c gone')

        ctx, fake_ctrl, runner, probe_mock = self._make(probe_impl=boom)
        self.assertTrue(self._wait_probe(ctx))
        self.assertFalse(ctx.probe_pending)
        self.assertIn('i2c gone', ctx.probe_error)

    def test_cli_lock_serializes_concurrent_run_calls(self):
        ctx, fake_ctrl, runner, probe_mock = self._make()
        started = threading.Event()
        done = threading.Event()
        calls = []

        def worker():
            started.set()
            ctx.run('disk1', '-status')
            calls.append('ran')
            done.set()

        ctx._cli_lock.acquire()
        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(done.wait(0.3),
                         'run() must block while the CLI lock is held')
        ctx._cli_lock.release()
        self.assertTrue(done.wait(2), 'run() must proceed after lock release')
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(calls, ['ran'])
        runner.assert_called_once_with('disk1', '-status')

    def test_run_delegates_to_underlying_runner(self):
        ctx, fake_ctrl, runner, probe_mock = self._make()
        ctx.run('all', '-status')
        runner.assert_called_once_with('all', '-status')

    def test_r7_corrupt_config_rewrite(self):
        """R7: a corrupt device_config.json must self-heal — AppContext
        falls back to defaults AND rewrites the file with valid content so
        the next boot no longer reads the broken file. Uses the real
        load_json/save_json against a real temp file (only heavy deps are
        mocked)."""
        runner = MagicMock(return_value=(True, '', ''))
        fake_ctrl = MagicMock()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        config_file = os.path.join(tmpdir.name, 'device_config.json')
        for corrupt in ('{invalid', '[1, 2]', '"oops"'):
            with self.subTest(corrupt=corrupt):
                with open(config_file, 'w') as f:
                    f.write(corrupt)
                patchers = [
                    patch.object(app_context_mod, 'make_cli_runner',
                                 return_value=runner),
                    patch.object(app_context_mod, 'AuthManager'),
                    patch.object(app_context_mod, 'StatusBroadcaster'),
                    patch.object(app_context_mod, 'detect_model',
                                 return_value={}),
                    patch.object(app_context_mod, 'PilotController',
                                 return_value=fake_ctrl),
                    patch.object(app_context_mod, 'probe_leds',
                                 return_value=({}, '')),
                    patch.object(app_context_mod, 'CONFIG_FILE', config_file),
                ]
                for p in patchers:
                    p.start()
                self.addCleanup(lambda: [p.stop() for p in patchers])
                ctx = app_context_mod.AppContext()
                self.assertTrue(self._wait_probe(ctx))
                # File must have been rewritten with a valid config dict.
                with open(config_file) as f:
                    saved = json.load(f)
                self.assertIsInstance(saved, dict)
                self.assertEqual(saved['disk_count'],
                                 ctx.default_disk_count)
                self.assertTrue(saved['activity_blink'])
                # Startup used the default config end to end.
                self.assertEqual(ctx.cfg, saved)
                self.assertTrue(ctx.initialized)
                fake_ctrl.start_monitor.assert_called_once_with()
                fake_ctrl.reset_mock()


class TestNightSchedule(_PilotTestCase):
    """定时策略（T3）：夜间窗口自动调暗/关闭，白天自动恢复。

    Uses an injectable wall clock (time.localtime shaped) so the window
    logic is testable without waiting for real wall time.
    """

    def _make_ctrl(self, disk_count=2):
        ctrl = super()._make_ctrl(disk_count=disk_count)
        ctrl._uevent = MagicMock()
        ctrl._uevent.is_running.return_value = False
        return ctrl

    def _clock_at(self, hour, minute=0):
        """Return a struct_time for the given wall-clock hour (today)."""
        return time.struct_time((2026, 1, 1, hour, minute, 0, 0, 1, -1))

    def test_default_disabled(self):
        ctrl = self._make_ctrl()
        self.assertFalse(ctrl.night_schedule['enabled'])
        self.assertFalse(ctrl.night_active)

    def test_in_night_window_cross_midnight(self):
        ctrl = self._make_ctrl()
        ctrl.night_schedule = {'enabled': True, 'start_hour': 22, 'end_hour': 7,
                               'night_brightness': 13}
        self.assertTrue(ctrl._in_night_window(self._clock_at(23)))
        self.assertTrue(ctrl._in_night_window(self._clock_at(6, 59)))
        self.assertFalse(ctrl._in_night_window(self._clock_at(21)))
        self.assertFalse(ctrl._in_night_window(self._clock_at(7)))
        self.assertFalse(ctrl._in_night_window(self._clock_at(12)))

    def test_in_night_window_same_day(self):
        ctrl = self._make_ctrl()
        ctrl.night_schedule = {'enabled': True, 'start_hour': 13, 'end_hour': 17,
                               'night_brightness': 13}
        self.assertTrue(ctrl._in_night_window(self._clock_at(14)))
        self.assertFalse(ctrl._in_night_window(self._clock_at(18)))

    def test_set_night_schedule_validates(self):
        ctrl = self._make_ctrl()
        ok, _ = ctrl.set_night_schedule(
            {'enabled': True, 'start_hour': 24, 'end_hour': 7, 'night_brightness': 13})
        self.assertFalse(ok, 'start_hour out of range must be rejected')
        ok, _ = ctrl.set_night_schedule(
            {'enabled': True, 'start_hour': 22, 'end_hour': 7, 'night_brightness': 300})
        self.assertFalse(ok, 'night_brightness out of range must be rejected')

    def test_night_active_applies_dim_brightness(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'on', 'disk1': 'on', 'disk2': 'on'}
        ctrl.night_schedule = {'enabled': True, 'start_hour': 22, 'end_hour': 7,
                               'night_brightness': 13}
        ctrl.night_active = True
        ctrl.run.reset_mock()
        ok, _ = ctrl._apply('power', 'on')
        self.assertTrue(ok)
        call = ctrl.run.call_args
        self.assertIsNotNone(call)
        args = call.args
        self.assertIn('-brightness', args)
        self.assertEqual(args[args.index('-brightness') + 1], '13',
                         'night dimming must override brightness to 13')

    def test_night_brightness_zero_turns_off(self):
        ctrl = self._make_ctrl()
        ctrl.modes = {'power': 'on', 'netdev': 'off', 'disk1': 'off', 'disk2': 'off'}
        ctrl.night_schedule = {'enabled': True, 'start_hour': 22, 'end_hour': 7,
                               'night_brightness': 0}
        ctrl.night_active = True
        ctrl.run.reset_mock()
        ok, _ = ctrl._apply('power', 'on')
        self.assertTrue(ok)
        args = ctrl.run.call_args.args
        self.assertIn('-off', args, 'night_brightness=0 must emit -off')

    def test_evaluate_night_mode_state_transition_notifies(self):
        ctrl = self._make_ctrl()
        ctrl.night_schedule = {'enabled': True, 'start_hour': 22, 'end_hour': 7,
                               'night_brightness': 13}
        ctrl._now = lambda: self._clock_at(23)   # in-window
        changed = ctrl._evaluate_night_mode()
        self.assertTrue(changed)
        self.assertTrue(ctrl.night_active)
        changed = ctrl._evaluate_night_mode()    # same window, no change
        self.assertFalse(changed)
        ctrl._now = lambda: self._clock_at(8)    # out-of-window
        changed = ctrl._evaluate_night_mode()
        self.assertTrue(changed)
        self.assertFalse(ctrl.night_active)

    def test_status_exposes_night_schedule(self):
        ctrl = self._make_ctrl()
        status = ctrl.get_status()
        self.assertIn('night_schedule', status)
        self.assertIn('night_active', status)
        self.assertFalse(status['night_active'])


class TestAlerts(_PilotTestCase):
    """故障告警（T5）：磁盘消失/断网 debounce 后触发红色闪烁告警。"""

    def _make_ctrl(self, disk_count=2):
        ctrl = super()._make_ctrl(disk_count=disk_count)
        ctrl._uevent = MagicMock()
        ctrl._uevent.is_running.return_value = False
        return ctrl

    def _enable(self, ctrl):
        ctrl.set_alert_config({'enabled': True, 'disk_failure': True,
                               'network_down': True})

    def test_alert_config_default_off(self):
        ctrl = self._make_ctrl()
        self.assertFalse(ctrl.alert_cfg['enabled'])
        self.assertEqual(ctrl.alerts, {'disk_lost': {}, 'network_down': False})

    def test_set_alert_config_validates(self):
        ctrl = self._make_ctrl()
        ok, _ = ctrl.set_alert_config({'enabled': 'not-bool'})
        self.assertFalse(ok)

    def test_disk_loss_debounced_triggers_alert(self):
        ctrl = self._make_ctrl()
        self._enable(ctrl)
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.presence['disk1'] = True
        with patch('pilot_core.disk_present', return_value=False):
            for _ in range(2):
                ctrl._check_disks()          # below threshold
            self.assertNotIn('disk1', ctrl.alerts['disk_lost'])
            ctrl._check_disks()              # 3rd check -> alert
        self.assertIn('disk1', ctrl.alerts['disk_lost'])
        self.assertTrue(ctrl.alerts['disk_lost']['disk1'])
        # red blink CLI emitted
        self.assertTrue(any('-blink' in c.args and '255' in c.args and '64' in c.args
                            for c in ctrl.run.call_args_list),
                        'alert must emit a red blink CLI')

    def test_disk_replug_clears_alert(self):
        ctrl = self._make_ctrl()
        self._enable(ctrl)
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.presence['disk1'] = True
        with patch('pilot_core.disk_present', return_value=False):
            for _ in range(3):
                ctrl._check_disks()
        self.assertIn('disk1', ctrl.alerts['disk_lost'])
        with patch('pilot_core.disk_present', return_value=True), \
                patch('pilot_core.read_disk_io', return_value=0):
            ctrl._check_disks()
        self.assertNotIn('disk1', ctrl.alerts['disk_lost'])

    def test_network_down_debounced_alert(self):
        ctrl = self._make_ctrl()
        self._enable(ctrl)
        ctrl.modes['netdev'] = 'auto'
        ctrl.activity['netdev'] = True
        with patch('pilot_core.list_net_ifaces', return_value=[]), \
                patch('pilot_core.detect_net_iface', return_value=None):
            for _ in range(2):
                ctrl._check_network()
            self.assertFalse(ctrl.alerts['network_down'])
            ctrl._check_network()
        self.assertTrue(ctrl.alerts['network_down'])

    def test_network_up_clears_alert(self):
        ctrl = self._make_ctrl()
        self._enable(ctrl)
        ctrl.modes['netdev'] = 'auto'
        ctrl.alerts['network_down'] = True
        ctrl._net_down_count = 3
        with patch('pilot_core.list_net_ifaces', return_value=['eth0']), \
                patch('pilot_core.detect_net_iface', return_value='eth0'), \
                patch('pilot_core.read_stats', return_value=0):
            ctrl._check_network()
        self.assertFalse(ctrl.alerts['network_down'])
        self.assertEqual(ctrl._net_down_count, 0)

    def test_disabled_alert_is_noop(self):
        ctrl = self._make_ctrl()          # alert_cfg disabled by default
        ctrl._disk_map = {1: 'sda'}
        ctrl.modes['disk1'] = 'auto'
        ctrl.presence['disk1'] = True
        with patch('pilot_core.disk_present', return_value=False):
            for _ in range(5):
                ctrl._check_disks()
        self.assertEqual(ctrl.alerts['disk_lost'], {},
                         'disabled alert config must not raise alerts')

    def test_status_exposes_alerts(self):
        ctrl = self._make_ctrl()
        status = ctrl.get_status()
        self.assertIn('alerts', status)
        self.assertIn('alert_cfg', status)


class TestI2CEnsure(unittest.TestCase):
    """I2C 自愈：CLI 报 'fail to open the I2C device' 时自动 modprobe 重试。

    Root cause: install_init only loads i2c-dev at install time; after a
    reboot the module is gone, /dev/i2c-* nodes vanish, and every CLI call
    fails. The app runs as root, so a runtime modprobe + retry is possible.
    """

    def _mock_subprocess(self, side_effect):
        patcher = patch('pilot_core.subprocess.run', side_effect=side_effect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ensure_i2c_loads_module_when_devices_missing(self):
        """No /dev/i2c-* initially -> modprobe i2c-dev is attempted."""
        import pilot_core
        self._mock_subprocess(MagicMock(return_value=MagicMock()))
        with patch('pilot_core.glob.glob', side_effect=[[], ['/dev/i2c-0']]) as mock_glob:
            ok = pilot_core.ensure_i2c_dev()
        self.assertTrue(ok, 'ensure_i2c_dev should succeed once devices appear')
        modprobe_calls = [c for c in pilot_core.subprocess.run.call_args_list
                          if c.args and c.args[0] and c.args[0][0] == 'modprobe']
        self.assertTrue(modprobe_calls, 'modprobe should be invoked when devices are missing')
        self.assertEqual(mock_glob.call_count, 2, 'glob checked before and after modprobe')

    def test_ensure_i2c_noop_when_devices_present(self):
        """/dev/i2c-* already present -> no modprobe needed."""
        import pilot_core
        self._mock_subprocess(MagicMock(return_value=MagicMock()))
        with patch('pilot_core.glob.glob', return_value=['/dev/i2c-0']):
            ok = pilot_core.ensure_i2c_dev()
        self.assertTrue(ok)
        modprobe_calls = [c for c in pilot_core.subprocess.run.call_args_list
                          if c.args and c.args[0] and c.args[0][0] == 'modprobe']
        self.assertFalse(modprobe_calls,
                         'modprobe must not run when /dev/i2c-* already exists')

    def test_ensure_i2c_false_when_still_missing(self):
        """modprobe attempted but no devices -> False (heal failed)."""
        import pilot_core
        self._mock_subprocess(MagicMock(return_value=MagicMock()))
        with patch('pilot_core.glob.glob', return_value=[]):
            ok = pilot_core.ensure_i2c_dev()
        self.assertFalse(ok)

    def test_cli_runner_selfheals_on_i2c_error(self):
        """First CLI call fails with I2C error, heal succeeds, retry works."""
        import pilot_core
        fake = MagicMock()
        fake.stdout = ''
        fake.stderr = 'Err: fail to open the I2C device. Please check...'
        fake.returncode = 1
        good = MagicMock()
        good.stdout = 'power: status = on, brightness = 255, color = RGB(255,255,255)'
        good.stderr = ''
        good.returncode = 0
        # CLI is invoked exactly twice; ensure_i2c_dev internals are covered
        # by the TestI2CEnsure.ensure_* tests, so stub it to succeed here.
        self._mock_subprocess([fake, good])
        with patch('pilot_core.ensure_i2c_dev', return_value=True):
            runner = pilot_core.make_cli_runner('/usr/local/bin/ugreen_leds_cli')
            ok, out, err = runner('power', '-on')
        self.assertTrue(ok, 'runner should retry and succeed after heal')
        self.assertIn('power', out)
        self.assertEqual(pilot_core.subprocess.run.call_count, 2,
                         'CLI invoked twice: once failing, once after heal')

    def test_cli_runner_no_selfheal_on_other_error(self):
        """Non-I2C errors (e.g. CLI not a valid binary) must NOT trigger heal."""
        import pilot_core
        fake = MagicMock()
        fake.stdout = ''
        fake.stderr = 'some other error'
        fake.returncode = 2
        self._mock_subprocess([fake])
        with patch('pilot_core.glob.glob', return_value=[]):
            runner = pilot_core.make_cli_runner('/usr/local/bin/ugreen_leds_cli')
            ok, out, err = runner('power', '-on')
        self.assertFalse(ok)
        self.assertEqual(pilot_core.subprocess.run.call_count, 1,
                         'no retry for non-I2C errors')

    def test_cli_runner_selfheal_fails_returns_error(self):
        """I2C error and heal fails -> return the CLI error."""
        import pilot_core
        fake = MagicMock()
        fake.stdout = ''
        fake.stderr = 'Err: fail to open the I2C device. Please check...'
        fake.returncode = 1
        self._mock_subprocess([fake])
        with patch('pilot_core.ensure_i2c_dev', return_value=False):
            runner = pilot_core.make_cli_runner('/usr/local/bin/ugreen_leds_cli')
            ok, out, err = runner('power', '-on')
        self.assertFalse(ok)
        self.assertIn('I2C', err, 'error surfaced to caller')


if __name__ == '__main__':
    unittest.main()
