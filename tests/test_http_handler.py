#!/usr/bin/env python3
"""HTTP handler layer unit tests (Todo 24).

Covers PilotHandler auth (login / cookie / lockout), CSRF protection,
origin checking (E4 exact-host regression), int validation (E3), and
static-file serving — all with mocked hardware, no third-party deps.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from auth_manager import AuthManager, MAX_LOGIN_ATTEMPTS
from http_handler import PilotHandler, _header_host, MAX_BODY_BYTES
from pilot_core import PilotController
from utils import save_json


class _HandlerMixin:
    """Shared harness: real AuthManager on a temp file, mocked app/ctrl/I-O."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.auth = AuthManager(os.path.join(self.dir, 'auth.json'))
        self.ctrl = MagicMock()
        self.ctrl.disk_count = 4
        self.ctrl.set_mode.return_value = (True, 'OK')
        self.ctrl.set_color.return_value = (True, 'OK')
        self.ctrl.set_brightness.return_value = (True, 'OK')
        self.ctrl.set_global_brightness.return_value = (True, 'OK')
        self.ctrl.get_status.return_value = {'modes': {}}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_handler(self, path='/', headers=None):
        """PilotHandler without __init__; _json stubbed to capture (status, data)."""
        handler = PilotHandler.__new__(PilotHandler)
        handler.app = MagicMock()
        handler.app.auth = self.auth
        handler.app.ctrl = self.ctrl
        handler.client_address = ('127.0.0.1', 5555)
        handler.path = path
        handler.headers = dict(headers or {})
        handler.rfile = MagicMock()
        handler.wfile = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        responses = []
        handler._json = lambda status, data: responses.append((status, data))
        handler._responses = responses
        return handler

    def auth_headers(self):
        """Valid session cookie + CSRF token (authenticated + csrf-passing)."""
        session, csrf = self.auth.create_session()
        return {'Cookie': f'pilot_session={session}', 'X-CSRF-Token': csrf}

    def session_headers(self):
        """Valid session cookie but no CSRF token."""
        session, _ = self.auth.create_session()
        return {'Cookie': f'pilot_session={session}'}

    def post_json(self, handler, payload):
        with patch.object(handler, '_body', return_value=json.dumps(payload)):
            handler.do_POST()

    def set_cookie_headers(self, handler):
        return [c.args[1] for c in handler.send_header.call_args_list
                if c.args[0] == 'Set-Cookie']

    def content_type_headers(self, handler):
        return [c.args[1] for c in handler.send_header.call_args_list
                if c.args[0] == 'Content-Type']


class TestLogin(_HandlerMixin, unittest.TestCase):
    """E1: POST /api/login — success 200 + Set-Cookie, unified 401, lockout 429."""

    def test_login_success_200_sets_cookie(self):
        handler = self.make_handler(path='/api/login')
        self.post_json(handler, {'username': 'admin', 'password': 'admin123'})
        handler.send_response.assert_called_once_with(200)
        cookies = self.set_cookie_headers(handler)
        self.assertEqual(len(cookies), 1)
        self.assertIn('pilot_session=', cookies[0])
        self.assertIn('HttpOnly', cookies[0])
        self.assertIn('SameSite=Strict', cookies[0])
        body = json.loads(handler.wfile.write.call_args[0][0].decode())
        self.assertTrue(body['success'])
        self.assertTrue(body['csrf_token'])
        # the returned session cookie must actually authenticate
        session = cookies[0].split('pilot_session=', 1)[1].split(';', 1)[0]
        self.assertTrue(self.auth.is_authenticated(
            {'Cookie': f'pilot_session={session}'}))

    def test_login_wrong_password_401_unified(self):
        handler = self.make_handler(path='/api/login')
        self.post_json(handler, {'username': 'admin', 'password': 'wrong'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')
        self.assertEqual(self.set_cookie_headers(handler), [],
                         'failed login must not issue a session cookie')

    def test_login_wrong_username_401_same_message(self):
        handler = self.make_handler(path='/api/login')
        self.post_json(handler, {'username': 'bad_user', 'password': 'admin123'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')

    def test_login_missing_password_401(self):
        handler = self.make_handler(path='/api/login')
        self.post_json(handler, {'username': 'admin'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '用户名或密码错误')

    def test_login_lockout_429_after_max_attempts(self):
        handler = self.make_handler(path='/api/login')
        for _ in range(MAX_LOGIN_ATTEMPTS):
            self.post_json(handler, {'username': 'admin', 'password': 'wrong'})
            self.assertEqual(handler._responses[-1][0], 401)
        # correct credentials are rejected too while locked out
        self.post_json(handler, {'username': 'admin', 'password': 'admin123'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 429)
        self.assertIn('登录尝试过多', body['message'])


class TestAuthRequired(_HandlerMixin, unittest.TestCase):
    """E1: API endpoints require a valid session."""

    def test_api_status_without_auth_401(self):
        handler = self.make_handler(path='/api/status')
        handler.do_GET()
        status, body = handler._responses[-1]
        self.assertEqual(status, 401)
        self.assertEqual(body['message'], '请先登录')

    def test_api_status_authenticated_200(self):
        handler = self.make_handler(path='/api/status', headers=self.auth_headers())
        handler.do_GET()
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.ctrl.get_status.assert_called_once_with()
        self.assertIn('csrf_token', body)

    def test_unknown_api_route_404_authenticated(self):
        handler = self.make_handler(path='/api/nope', headers=self.auth_headers())
        handler.do_GET()
        self.assertEqual(handler._responses[-1][0], 404)


class TestCsrfProtection(_HandlerMixin, unittest.TestCase):
    """E2: write operations require X-CSRF-Token."""

    def test_post_without_csrf_403(self):
        handler = self.make_handler(
            path='/api/color', headers=self.session_headers())
        self.post_json(handler, {'led': 'power', 'r': 1, 'g': 2, 'b': 3})
        status, body = handler._responses[-1]
        self.assertEqual(status, 403)
        self.assertEqual(body['message'], 'CSRF 校验失败，请刷新页面')
        self.ctrl.set_color.assert_not_called()

    def test_logout_without_csrf_403(self):
        handler = self.make_handler(
            path='/api/logout', headers=self.session_headers())
        self.post_json(handler, {})
        self.assertEqual(handler._responses[-1][0], 403)

    def test_post_with_valid_csrf_passes(self):
        handler = self.make_handler(path='/api/color', headers=self.auth_headers())
        self.post_json(handler, {'led': 'power', 'r': 1, 'g': 2, 'b': 3})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.ctrl.set_color.assert_called_once_with('power', 1, 2, 3)

    def test_wrong_csrf_token_403(self):
        headers = self.auth_headers()
        headers['X-CSRF-Token'] = 'forged-token'
        handler = self.make_handler(path='/api/color', headers=headers)
        self.post_json(handler, {'led': 'power', 'r': 1, 'g': 2, 'b': 3})
        self.assertEqual(handler._responses[-1][0], 403)


class TestOriginCheck(_HandlerMixin, unittest.TestCase):
    """E4 regression: exact host comparison — suffix domains must be rejected."""

    def test_header_host_exact_host(self):
        self.assertEqual(_header_host('http://127.0.0.1:19580'), '127.0.0.1')
        self.assertEqual(_header_host('http://127.0.0.1:19580/login.html'), '127.0.0.1')

    def test_header_host_rejects_port_suffix_spoofing(self):
        # urlparse would yield hostname 127.0.0.1; the non-numeric port
        # segment must make the whole header invalid (E4 fix).
        self.assertIsNone(_header_host('http://127.0.0.1:19580.evil.com'))

    def test_header_host_invalid_url_none(self):
        self.assertIsNone(_header_host(''))
        self.assertIsNone(_header_host('not a url'))

    def test_same_origin_allowed(self):
        handler = self.make_handler(headers={
            'Host': '127.0.0.1:19580', 'Origin': 'http://127.0.0.1:19580'})
        self.assertTrue(handler._check_origin())

    def test_same_origin_referer_allowed(self):
        handler = self.make_handler(headers={
            'Host': '127.0.0.1:19580',
            'Referer': 'http://127.0.0.1:19580/login.html'})
        self.assertTrue(handler._check_origin())

    def test_no_origin_headers_allowed(self):
        handler = self.make_handler(headers={'Host': '127.0.0.1:19580'})
        self.assertTrue(handler._check_origin())

    def test_evil_origin_suffix_rejected(self):
        handler = self.make_handler(headers={
            'Host': '127.0.0.1:19580',
            'Origin': 'http://127.0.0.1:19580.evil.com'})
        self.assertFalse(handler._check_origin())

    def test_evil_referer_rejected(self):
        handler = self.make_handler(headers={
            'Host': '127.0.0.1:19580', 'Referer': 'http://evil.com/'})
        self.assertFalse(handler._check_origin())

    def test_post_evil_origin_403_even_with_valid_session(self):
        headers = self.auth_headers()
        headers['Host'] = '127.0.0.1:19580'
        headers['Origin'] = 'http://127.0.0.1:19580.evil.com'
        handler = self.make_handler(path='/api/color', headers=headers)
        self.post_json(handler, {'led': 'power', 'r': 1, 'g': 2, 'b': 3})
        status, body = handler._responses[-1]
        self.assertEqual(status, 403)
        self.assertEqual(body['message'], '来源校验失败')
        self.ctrl.set_color.assert_not_called()

    def test_post_same_origin_allowed(self):
        headers = self.auth_headers()
        headers['Host'] = '127.0.0.1:19580'
        headers['Origin'] = 'http://127.0.0.1:19580'
        handler = self.make_handler(path='/api/color', headers=headers)
        self.post_json(handler, {'led': 'power', 'r': 1, 'g': 2, 'b': 3})
        self.assertEqual(handler._responses[-1][0], 200)
        self.ctrl.set_color.assert_called_once_with('power', 1, 2, 3)


class TestIntValidation(_HandlerMixin, unittest.TestCase):
    """E3 regression: non-int color/brightness payloads must be 400, not crash."""

    def test_color_invalid_r_400(self):
        handler = self.make_handler()
        handler._color({'led': 'power', 'r': 'abc', 'g': 0, 'b': 0})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], 'Invalid color')
        self.ctrl.set_color.assert_not_called()

    def test_color_float_r_400(self):
        handler = self.make_handler()
        handler._color({'led': 'power', 'r': '12.5', 'g': 0, 'b': 0})
        self.assertEqual(handler._responses[-1][0], 400)

    def test_color_missing_b_400(self):
        handler = self.make_handler()
        handler._color({'led': 'power', 'r': 1, 'g': 2})
        self.assertEqual(handler._responses[-1][0], 400)

    def test_color_invalid_r_via_post_400(self):
        handler = self.make_handler(path='/api/color', headers=self.auth_headers())
        self.post_json(handler, {'led': 'power', 'r': 'xyz', 'g': 0, 'b': 0})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], 'Invalid color')
        self.ctrl.set_color.assert_not_called()

    def test_color_valid_200(self):
        handler = self.make_handler(path='/api/color', headers=self.auth_headers())
        self.post_json(handler, {'led': 'power', 'r': 10, 'g': 20, 'b': 30})
        self.assertEqual(handler._responses[-1][0], 200)
        self.ctrl.set_color.assert_called_once_with('power', 10, 20, 30)

    def test_brightness_invalid_400(self):
        handler = self.make_handler()
        handler._brightness({'brightness': 'bright'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], 'Invalid brightness')
        self.ctrl.set_global_brightness.assert_not_called()

    def test_brightness_invalid_via_post_400(self):
        handler = self.make_handler(path='/api/brightness', headers=self.auth_headers())
        self.post_json(handler, {'brightness': 'x'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], 'Invalid brightness')

    def test_brightness_global_valid_200(self):
        handler = self.make_handler(path='/api/brightness', headers=self.auth_headers())
        self.post_json(handler, {'brightness': 100})
        self.assertEqual(handler._responses[-1][0], 200)
        self.ctrl.set_global_brightness.assert_called_once_with(100)

    def test_brightness_led_valid_200(self):
        handler = self.make_handler(path='/api/brightness', headers=self.auth_headers())
        self.post_json(handler, {'led': 'power', 'brightness': 50})
        self.assertEqual(handler._responses[-1][0], 200)
        self.ctrl.set_brightness.assert_called_once_with('power', 50)

    def test_control_invalid_params_400(self):
        handler = self.make_handler(path='/api/control', headers=self.auth_headers())
        self.post_json(handler, {'led': 'bad_led', 'action': 'on'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], '无效参数')

    def test_invalid_json_400(self):
        handler = self.make_handler(path='/api/login')
        with patch.object(handler, '_body', return_value='{not json'):
            handler.do_POST()
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['message'], 'Invalid JSON')


class TestStaticServing(_HandlerMixin, unittest.TestCase):
    """Static files: path traversal rejected, real assets served."""

    def test_path_traversal_403(self):
        handler = self.make_handler(path='/static/../../etc/passwd')
        handler.do_GET()
        status, body = handler._responses[-1]
        self.assertEqual(status, 403)
        self.assertFalse(body['success'])

    def test_url_encoded_traversal_403(self):
        # unquote turns %2F into '/'; the '..' segment must still be caught
        handler = self.make_handler(path='/static/..%2F..%2Fetc/passwd')
        handler.do_GET()
        self.assertEqual(handler._responses[-1][0], 403)

    def test_app_css_200(self):
        handler = self.make_handler(path='/static/app.css')
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(self.content_type_headers(handler), ['text/css'])
        cache = [c.args[1] for c in handler.send_header.call_args_list
                 if c.args[0] == 'Cache-Control']
        self.assertEqual(cache, ['public, max-age=3600'])
        data = handler.wfile.write.call_args[0][0]
        self.assertGreater(len(data), 0)

    def test_index_serves_login_when_unauthenticated(self):
        handler = self.make_handler(path='/')
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertIn('text/html', self.content_type_headers(handler)[0])
        html = handler.wfile.write.call_args[0][0].decode()
        self.assertIn('<html', html.lower())

    def test_missing_static_404(self):
        handler = self.make_handler(path='/static/does-not-exist.js')
        handler.do_GET()
        self.assertEqual(handler._responses[-1][0], 404)


class TestRequestBody(_HandlerMixin, unittest.TestCase):
    def test_oversized_body_raises(self):
        handler = self.make_handler(headers={
            'Content-Length': str(MAX_BODY_BYTES + 1)})
        with self.assertRaises(ValueError):
            handler._body()

    def test_body_reads_exact_length(self):
        handler = self.make_handler(headers={'Content-Length': '5'})
        handler.rfile.read.return_value = b'hello'
        self.assertEqual(handler._body(), b'hello')
        handler.rfile.read.assert_called_once_with(5)

    def test_empty_body_no_read(self):
        handler = self.make_handler(headers={})
        self.assertEqual(handler._body(), '')

    def test_post_unknown_route_404_authenticated(self):
        handler = self.make_handler(path='/api/nope', headers=self.auth_headers())
        self.post_json(handler, {})
        self.assertEqual(handler._responses[-1][0], 404)


class TestCalibrate(_HandlerMixin, unittest.TestCase):
    """F8 (Todo 20): 盘位校准 — identify blink + manual bind, persisted."""

    FAKE_DEVICES = {
        'sda': {'hctl': '0:0:0:0', 'ata': None, 'serial': ''},
        'sdb': {'hctl': '1:0:0:0', 'ata': None, 'serial': ''},
        'sdc': {'hctl': '2:0:0:0', 'ata': None, 'serial': ''},
        'sdd': {'hctl': '3:0:0:0', 'ata': None, 'serial': ''},
    }

    def make_controller(self, bay_bindings=None):
        return PilotController(
            ['power', 'netdev', 'disk1', 'disk2', 'disk3', 'disk4'],
            run=MagicMock(return_value=(True, '', '')),
            state_file=os.path.join(self.dir, 'led_state.json'),
            settings_file=os.path.join(self.dir, 'led_settings.json'),
            disk_count=4,
            bay_bindings=bay_bindings,
        )

    def test_identify_valid_led_delegates_to_ctrl(self):
        # F2: the handler must delegate to ctrl.identify() so the blink
        # runs inside the controller lock, never a raw run() call.
        self.ctrl.identify.return_value = (True, 'OK')
        handler = self.make_handler(
            path='/api/calibrate/identify', headers=self.auth_headers())
        self.post_json(handler, {'led': 'disk1'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.ctrl.identify.assert_called_once_with('disk1')
        self.ctrl.run.assert_not_called()

    def test_identify_invalid_led_400(self):
        handler = self.make_handler(
            path='/api/calibrate/identify', headers=self.auth_headers())
        self.post_json(handler, {'led': 'disk9'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(body['success'])
        self.ctrl.identify.assert_not_called()

    def test_bind_valid_hctl_200_and_persisted(self):
        cfg_path = os.path.join(self.dir, 'device_config.json')
        with patch('http_handler.CONFIG_FILE', cfg_path):
            handler = self.make_handler(
                path='/api/calibrate/bind', headers=self.auth_headers())
            handler.app.cfg = {'bay_bindings': {}}
            self.post_json(handler, {'slot': 'disk1', 'hctl': '2:0:0:0'})
            status, body = handler._responses[-1]
            self.assertEqual(status, 200)
            self.assertTrue(body['success'])
            with open(cfg_path, encoding='utf-8') as f:
                saved = json.load(f)
            self.assertEqual(saved['bay_bindings'], {'disk1': '2:0:0:0'})
            # other keys survive (additive-only)
            self.assertIn('bay_bindings', saved)
            self.ctrl.set_bay_bindings.assert_called_once_with({'disk1': '2:0:0:0'})

    def test_bind_invalid_hctl_400(self):
        handler = self.make_handler(
            path='/api/calibrate/bind', headers=self.auth_headers())
        handler.app.cfg = {'bay_bindings': {}}
        self.post_json(handler, {'slot': 'disk1', 'hctl': 'not-a-hctl'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(body['success'])

    def test_bind_invalid_slot_400(self):
        handler = self.make_handler(
            path='/api/calibrate/bind', headers=self.auth_headers())
        handler.app.cfg = {'bay_bindings': {}}
        self.post_json(handler, {'slot': 'disk9', 'hctl': '0:0:0:0'})
        self.assertEqual(handler._responses[-1][0], 400)

    def test_remap_uses_bay_bindings(self):
        with patch('pilot_core.collect_block_devices',
                   return_value=dict(self.FAKE_DEVICES)):
            ctrl = self.make_controller(bay_bindings={'disk1': '3:0:0:0'})
            ctrl.restore_state(apply_hardware=False)
        # bound slot 1 -> 3:0:0:0 (sdd); unbound slots keep default map
        self.assertEqual(ctrl._disk_map, {1: 'sdd', 2: 'sdb', 3: 'sdc', 4: 'sdd'})

    def test_force_remap_applies_runtime_bindings(self):
        # bindings saved after boot (via API) must apply on force_remap
        with patch('pilot_core.collect_block_devices',
                   return_value=dict(self.FAKE_DEVICES)):
            ctrl = self.make_controller()
            ctrl.set_bay_bindings({'disk2': '0:0:0:0'})
            ok, _ = ctrl.force_remap()
        self.assertTrue(ok)
        self.assertEqual(ctrl._disk_map[1], 'sda')
        self.assertEqual(ctrl._disk_map[2], 'sda')
        self.assertEqual(ctrl._disk_map[3], 'sdc')

    def test_reset_removes_bay_bindings(self):
        cfg_path = os.path.join(self.dir, 'device_config.json')
        with patch('http_handler.CONFIG_FILE', cfg_path), \
                patch('app_context.STATE_FILE',
                      os.path.join(self.dir, 'led_state.json')), \
                patch('app_context.SETTINGS_FILE',
                      os.path.join(self.dir, 'led_settings.json')):
            # a prior bind persisted bay_bindings
            handler = self.make_handler(
                path='/api/calibrate/bind', headers=self.auth_headers())
            handler.app.cfg = {'bay_bindings': {}}
            self.post_json(handler, {'slot': 'disk1', 'hctl': '2:0:0:0'})
            with open(cfg_path, encoding='utf-8') as f:
                self.assertIn('bay_bindings', json.load(f))

            # reset rebuilds the default config without bay_bindings
            handler = self.make_handler(
                path='/api/reset', headers=self.auth_headers())

            def fake_reset_controller():
                save_json(cfg_path, {
                    'disk_count': 4, 'model': 'dxp4800plus',
                    'model_name': 'DXP4800 Plus', 'product_name': '',
                    'auto_detected': True,
                    'ata_map': ['ata1', 'ata2', 'ata3', 'ata4'],
                    'hctl_map': ['0:0:0:0', '1:0:0:0', '2:0:0:0', '3:0:0:0'],
                    'activity_blink': True,
                })
            handler.app.reset_controller = fake_reset_controller
            self.post_json(handler, {})
            with open(cfg_path, encoding='utf-8') as f:
                saved = json.load(f)
            self.assertNotIn('bay_bindings', saved)


class TestEffectRoutes(_HandlerMixin, unittest.TestCase):
    """F1/F2/F4 (Todo 16/17/18): effect / chase / speed-blink HTTP wiring.

    The backend set_effect / set_chase / set_speed_blink methods already
    exist; these tests lock the HTTP layer entry points that were missing
    (rejected with 404), the all_off() chase stop, and ctrl.identify().
    """

    def make_controller(self):
        return self._track(PilotController(
            ['power', 'netdev', 'disk1', 'disk2'],
            run=MagicMock(return_value=(True, '', '')),
            state_file=os.path.join(self.dir, 'led_state.json'),
            settings_file=os.path.join(self.dir, 'led_settings.json'),
            disk_count=2,
        ))

    def tearDown(self):
        # cancel pending 0.8s settings timers + stop uevent watchers so no
        # controller threads outlive the test (same hygiene as _PilotTestCase)
        if getattr(self, '_ctrls', None):
            for ctrl in self._ctrls:
                if ctrl._settings_timer:
                    ctrl._settings_timer.cancel()
                ctrl._uevent.stop()
        super().tearDown()

    def _track(self, ctrl):
        if not getattr(self, '_ctrls', None):
            self._ctrls = []
        self._ctrls.append(ctrl)
        return ctrl

    def post_effect(self, ctrl, payload):
        handler = self.make_handler(path='/api/effect', headers=self.auth_headers())
        handler.app.ctrl = ctrl
        self.post_json(handler, payload)
        return handler

    def test_effect_breath_200_runs_cli_breath(self):
        ctrl = self.make_controller()
        ctrl.set_mode('power', 'on')
        ctrl.run.reset_mock()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'breath'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        args = ctrl.run.call_args[0]
        self.assertEqual(args[0], 'power')
        self.assertIn('-breath', args)

    def test_effect_blink_maps_to_manual_blink(self):
        # UI option 'blink' -> backend 'manual-blink' (-blink 400 400)
        ctrl = self.make_controller()
        ctrl.set_mode('power', 'on')
        ctrl.run.reset_mock()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'blink'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        args = ctrl.run.call_args[0]
        self.assertIn('-blink', args)
        self.assertIn('400', args)

    def test_effect_manual_blink_accepted_directly(self):
        ctrl = self.make_controller()
        ctrl.set_mode('power', 'on')
        ctrl.run.reset_mock()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'manual-blink'})
        self.assertEqual(handler._responses[-1][0], 200)
        self.assertIn('-blink', ctrl.run.call_args[0])

    def test_effect_off_clears_effect(self):
        ctrl = self.make_controller()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        ctrl.run.reset_mock()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'off'})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertNotIn('power', ctrl.effects)
        args = ctrl.run.call_args[0]
        self.assertNotIn('-breath', args)
        self.assertNotIn('-blink', args)
        self.assertIn('-on', args)

    def test_effect_null_clears_effect(self):
        ctrl = self.make_controller()
        ctrl.set_mode('power', 'on')
        ctrl.set_effect('power', 'breath')
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': None})
        self.assertEqual(handler._responses[-1][0], 200)
        self.assertNotIn('power', ctrl.effects)

    def test_effect_invalid_led_400(self):
        ctrl = self.make_controller()
        handler = self.post_effect(ctrl, {'led': 'disk9', 'effect': 'breath'})
        self.assertEqual(handler._responses[-1][0], 400)

    def test_effect_invalid_effect_400(self):
        ctrl = self.make_controller()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'strobe'})
        self.assertEqual(handler._responses[-1][0], 400)
        self.assertNotIn('power', ctrl.effects)

    def test_effect_invalid_timing_400(self):
        ctrl = self.make_controller()
        handler = self.post_effect(ctrl, {'led': 'power', 'effect': 'breath', 't_on': 'x'})
        self.assertEqual(handler._responses[-1][0], 400)

    def test_chase_enable_200(self):
        ctrl = self.make_controller()
        handler = self.make_handler(path='/api/chase', headers=self.auth_headers())
        handler.app.ctrl = ctrl
        self.post_json(handler, {'enabled': True})
        status, body = handler._responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertTrue(ctrl.chase_enabled)

    def test_chase_disable_200(self):
        ctrl = self.make_controller()
        ctrl.set_chase(True)
        handler = self.make_handler(path='/api/chase', headers=self.auth_headers())
        handler.app.ctrl = ctrl
        self.post_json(handler, {'enabled': False})
        self.assertEqual(handler._responses[-1][0], 200)
        self.assertFalse(ctrl.chase_enabled)

    def test_speed_blink_200_and_persisted(self):
        cfg_path = os.path.join(self.dir, 'device_config.json')
        with patch('http_handler.CONFIG_FILE', cfg_path):
            ctrl = self.make_controller()
            handler = self.make_handler(path='/api/speed-blink', headers=self.auth_headers())
            handler.app.ctrl = ctrl
            handler.app.cfg = {'activity_blink': True}
            self.post_json(handler, {'enabled': True})
            status, body = handler._responses[-1]
            self.assertEqual(status, 200)
            self.assertTrue(body['success'])
            self.assertTrue(ctrl.speed_blink_enabled)
            with open(cfg_path, encoding='utf-8') as f:
                saved = json.load(f)
            # additive: the new key lands next to pre-existing ones
            self.assertTrue(saved['speed_blink'])
            self.assertTrue(saved['activity_blink'])

    def test_all_off_stops_chase(self):
        # F2: "全部关闭" must stop the chase or the demo would re-light LEDs
        ctrl = self.make_controller()
        ctrl.set_chase(True)
        self.assertTrue(ctrl.chase_enabled)
        ok, msg = ctrl.all_off()
        self.assertTrue(ok)
        self.assertEqual(msg, 'OK')
        self.assertFalse(ctrl.chase_enabled)
        self.assertEqual(ctrl._chase_leds, [])
        ctrl.run.assert_any_call('all', '-off')

    def test_identify_blinks_led_with_current_color(self):
        ctrl = self.make_controller()
        ctrl.set_color('disk1', 10, 20, 30)
        ctrl.run.reset_mock()
        ok, err = ctrl.identify('disk1')
        self.assertTrue(ok)
        self.assertEqual(err, 'OK')
        args = ctrl.run.call_args[0]
        self.assertEqual(args[0], 'disk1')
        self.assertIn('-blink', args)
        self.assertIn('400', args)
        self.assertIn('10', args)
        self.assertIn('20', args)
        self.assertIn('30', args)

    def test_identify_records_apply_key(self):
        ctrl = self.make_controller()
        ok, _ = ctrl.identify('disk1')
        self.assertTrue(ok)
        self.assertEqual(
            ctrl._last_applied['disk1'],
            ctrl._apply_key('disk1', 'on', False, effect='manual-blink'))

    def test_identify_invalid_led_fails(self):
        ctrl = self.make_controller()
        ok, err = ctrl.identify('disk9')
        self.assertFalse(ok)
        self.assertIn('disk9', err)


if __name__ == '__main__':
    unittest.main()
