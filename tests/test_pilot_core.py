#!/usr/bin/env python3
"""Unit tests for UGreenLedPilot v2.1."""

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
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from pilot_core import (
    map_disks_by_profile, disk_signature, net_signature,
    quick_hardware_signature, DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE,
    DXP4800_PLUS_PROFILE, PilotController, StatusBroadcaster,
)
from auth_manager import AuthManager, PBKDF2_ITERATIONS
from http_handler import PilotHandler
from uevent_watcher import UeventWatcher


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


class TestSetColorOffSemantics(unittest.TestCase):
    def _make_ctrl(self):
        run = MagicMock(return_value=(True, '', ''))
        return PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run, '/tmp/test_state.json', '/tmp/test_settings.json',
            disk_count=2,
        )

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


class TestDiskIoCounterReset(unittest.TestCase):
    """Disk IO counter reset must not be treated as activity (delta=0)."""

    def _make_ctrl(self):
        run = MagicMock(return_value=(True, '', ''))
        ctrl = PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run, '/tmp/test_state.json', '/tmp/test_settings.json',
            disk_count=2,
        )
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


if __name__ == '__main__':
    unittest.main()
