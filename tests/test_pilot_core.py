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
    quick_hardware_signature, DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE,
    DXP4800_PLUS_PROFILE, EFFECT_PRIORITY, PilotController, StatusBroadcaster,
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
        self.assertIsInstance(quick_hardware_signature(), tuple)


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
        with patch('pilot_core.read_stats', side_effect=[130000, 0]):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-color', '255', '165', '0', '-brightness', '255')

    def test_mid_rate_uses_400_400(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        # 500KB/s in 1s → 400/400
        with patch('pilot_core.read_stats', side_effect=[600000, 0]):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '400', '400',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_high_rate_uses_100_100(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        # 2.4MB/s in 1s → 100/100
        with patch('pilot_core.read_stats', side_effect=[2500000, 0]):
            self.assertTrue(ctrl._check_network())
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '100', '100',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_rate_tier_change_retriggers_cli(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]):
            ctrl._check_network()  # 500KB/s → 400/400
        ctrl.run.reset_mock()
        ctrl._last_net_check = time.monotonic() - 1.0
        with patch('pilot_core.read_stats', side_effect=[2500000, 0]):
            self.assertTrue(ctrl._check_network())  # 2.4MB/s → 100/100
        ctrl.run.assert_called_once_with(
            'netdev', '-on', '-blink', '100', '100',
            '-color', '255', '165', '0', '-brightness', '255')

    def test_same_rate_tier_deduped(self):
        ctrl = self._make_ctrl()
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]):
            ctrl._check_network()  # 500KB/s → 400/400
        ctrl.run.reset_mock()
        ctrl._last_net_check = time.monotonic() - 1.0
        with patch('pilot_core.read_stats', side_effect=[1150000, 0]):
            self.assertFalse(ctrl._check_network())  # 550KB/s → still 400/400
        ctrl.run.assert_not_called()

    def test_speed_blink_disabled_keeps_fixed_blink(self):
        ctrl = self._make_ctrl(speed_blink=False)
        self._seed_net(ctrl)
        with patch('pilot_core.read_stats', side_effect=[600000, 0]):
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
        with patch('pilot_core.read_stats', side_effect=[1100, 2000, 1300, 2000, 1500, 2000]):
            ctrl._stop = _FakeLoopEvents(3)
            ctrl._wake_event = _FakeLoopEvents(3)
            ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'netdev' and '-blink' in c.args
                            for c in ctrl.run.call_args_list))
        self.assertEqual(ctrl._chase_index, 0)
        self.assertFalse(any(c.args[0] == 'disk1' for c in ctrl.run.call_args_list))
        self.assertEqual(ctrl._activity_idle_rounds, 0)

        # traffic stops -> chase resumes stepping
        with patch('pilot_core.read_stats', side_effect=[1500, 2000, 1500, 2000]):
            ctrl._stop = _FakeLoopEvents(2)
            ctrl._wake_event = _FakeLoopEvents(2)
            ctrl._monitor_loop()
        self.assertTrue(any(c.args[0] == 'disk1' and '-off' not in c.args
                            for c in ctrl.run.call_args_list))


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
        with patch('pilot_core.read_stats', side_effect=[1000, 2000, 550, 600]):
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
        with patch('pilot_core.read_stats') as mock_read:
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


if __name__ == '__main__':
    unittest.main()
