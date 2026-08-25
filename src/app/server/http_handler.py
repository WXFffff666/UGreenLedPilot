"""HTTP request handler with static files, REST API, and SSE."""

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse

from app_context import APP_VERSION, CONFIG_FILE, WWW_DIR
from pilot_core import VALID_MODES, VALID_EFFECTS
from utils import remove_file, save_json

MAX_BODY_BYTES = 65536
LED_NAME_RE = re.compile(r'^(power|netdev|disk[1-4])$')
HCTL_RE = re.compile(r'^\d+:\d+:\d+:\d+$')


def validate_led(led):
    return bool(led and LED_NAME_RE.match(led))


def validate_hctl(hctl):
    return bool(hctl and HCTL_RE.match(hctl))


def validate_mode(mode):
    return mode in VALID_MODES


def _origin_identity(url):
    """Extract (scheme, hostname, port) from an Origin/Referer/Host URL.

    Returns None when the host is missing or the port segment is not
    numeric, so crafted values like ``http://127.0.0.1:19580.evil.com``
    (whose parsed hostname would be ``127.0.0.1``) are rejected.

    Default ports are normalized so that ``http://host/`` equals
    ``host:80``: port 80 is dropped for http (and for scheme-less Host
    headers), port 443 for https.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return None
        port = parsed.port  # raises ValueError when port segment is non-numeric
        scheme = parsed.scheme.lower()
        if port is not None:
            if scheme == 'http' and port == 80:
                port = None
            elif scheme == 'https' and port == 443:
                port = None
            elif scheme == '' and port in (80, 443):
                port = None
        return (scheme, hostname.lower(), port)
    except (ValueError, TypeError, AttributeError):
        return None


def _header_host(url):
    """Extract lowercase hostname from an Origin/Referer URL, or None if invalid.

    Returns None when the host is missing or the port segment is not
    numeric, so that crafted values like ``http://127.0.0.1:19580.evil.com``
    (whose parsed hostname would be ``127.0.0.1``) are rejected.
    """
    identity = _origin_identity(url)
    return identity[1] if identity else None


class PilotHandler(BaseHTTPRequestHandler):
    server_version = f'UGreenLedPilot/{APP_VERSION}'
    app = None

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ''

    def _send_security_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        self.send_header('Cache-Control', 'no-store')
        # fnOS desktop embeds the app in a cross-origin iframe (desktop on
        # port 5666/5667 vs this app's service port). X-Frame-Options:
        # SAMEORIGIN would block that framing, so it is intentionally NOT
        # set; CSP frame-ancestors is its modern successor and allows the
        # desktop origin (scheme-sources, since the desktop port is non-default).
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'self' http: https:",
        )

    def _check_origin(self):
        origin = self.headers.get('Origin', '')
        referer = self.headers.get('Referer', '')
        host = self.headers.get('Host', '')
        if not origin and not referer:
            return True
        host_ident = _origin_identity('//' + host)
        if host_ident is None:
            return False
        host_scheme, host_name, host_port = host_ident
        for candidate in (origin, referer):
            if not candidate:
                continue
            cand = _origin_identity(candidate)
            if cand is None:
                return False
            cand_scheme, cand_name, cand_port = cand
            if (host_name, host_port) != (cand_name, cand_port):
                return False
            if host_scheme and cand_scheme and host_scheme != cand_scheme:
                return False
        return True

    def _require_auth(self):
        if not self.app.auth.is_authenticated(self.headers):
            self._json(401, {'success': False, 'message': '请先登录'})
            return False
        return True

    def _require_csrf(self):
        token = self.headers.get('X-CSRF-Token', '')
        if not self.app.auth.validate_csrf(token):
            self._json(403, {'success': False, 'message': 'CSRF 校验失败，请刷新页面'})
            return False
        return True

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/login', '/login.html'):
            return self._serve_static('login.html')
        if path in ('/', '/index.html'):
            if not self.app.auth.is_authenticated(self.headers):
                return self._serve_static('login.html')
            return self._serve_static('index.html')
        if path.startswith('/static/'):
            return self._serve_static(path[len('/static/'):])
        if path == '/api/events':
            return self._sse_stream()
        if not self._require_auth():
            return
        if path == '/api/status':
            return self._api_status()
        if path == '/api/config':
            return self._json(200, {
                'success': True,
                'config': self.app.cfg,
                'detected': self.app.detected,
                'probe_error': self.app.probe_error,
                'probe_pending': getattr(self.app, 'probe_pending', False),
            })
        return self._json(404, {'success': False})

    def do_POST(self):
        if not self._check_origin() and self.path not in ('/api/login',):
            return self._json(403, {'success': False, 'message': '来源校验失败'})

        body = self._body()
        if body is None:
            return
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json(400, {'success': False, 'message': 'Invalid JSON'})

        if self.path == '/api/login':
            return self._login(data)
        if not self._require_auth():
            return
        if not self._require_csrf():
            return

        routes = {
            '/api/logout': self._logout,
            '/api/control': lambda: self._control(data),
            '/api/color': lambda: self._color(data),
            '/api/preset': lambda: self._preset(data),
            '/api/brightness': lambda: self._brightness(data),
            '/api/save': self._save_settings,
            '/api/change-password': lambda: self._change_password(data),
            '/api/remap': self._remap,
            '/api/activity-blink': lambda: self._activity_blink(data),
            '/api/effect': lambda: self._effect(data),
            '/api/chase': lambda: self._chase(data),
            '/api/speed-blink': lambda: self._speed_blink(data),
            '/api/night-schedule': lambda: self._night_schedule(data),
            '/api/alerts': lambda: self._alerts(data),
            '/api/calibrate/identify': lambda: self._calibrate_identify(data),
            '/api/calibrate/bind': lambda: self._calibrate_bind(data),
            '/api/all/off': lambda: self._all('off'),
            '/api/all/on': lambda: self._all('on'),
            '/api/all/auto': lambda: self._all('auto'),
            '/api/reset': self._reset_config,
        }
        if self.path in routes:
            return routes[self.path]()
        return self._json(404, {'success': False})

    def _api_status(self):
        status = self.app.ctrl.get_status()
        status['csrf_token'] = self.app.auth.get_csrf_token()
        status['must_change_password'] = self.app.auth.must_change_password()
        status['version'] = APP_VERSION
        status['model_warning'] = self.app.model_info.get('warning', '')
        return self._json(200, {
            'success': True,
            'initialized': self.app.initialized,
            'model': self.app.model_info,
            'leds': self.app.led_names,
            **status,
        })

    def _sse_stream(self):
        if not self._require_auth():
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self._send_security_headers()
        self.end_headers()

        queue, ev = self.app.broadcaster.subscribe()
        try:
            payload = json.dumps(self.app.ctrl.get_live_status(), ensure_ascii=False)
            self.wfile.write(f'data: {payload}\n\n'.encode())
            self.wfile.flush()

            while not self.app.ctrl._stop.is_set():
                if queue:
                    payload = json.dumps(queue.pop(0), ensure_ascii=False)
                    self.wfile.write(f'data: {payload}\n\n'.encode())
                    self.wfile.flush()
                    continue
                ev.clear()
                if not ev.wait(timeout=30.0):
                    try:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.broadcaster.unsubscribe((queue, ev))

    def _login(self, data):
        ip = self._client_ip()
        if self.app.auth.is_locked_out(ip):
            remaining = self.app.auth.lockout_remaining(ip)
            return self._json(429, {
                'success': False,
                'message': f'登录尝试过多，请 {remaining // 60 + 1} 分钟后再试',
            })
        username = data.get('username', '')
        if not self.app.auth.verify_credentials(
                username, data.get('password', '')):
            self.app.auth.record_failed_login(ip)
            print(f'Login failed: user={username!r} ip={ip}')
            return self._json(401, {'success': False, 'message': '用户名或密码错误'})
        self.app.auth.record_successful_login(ip)
        print(f'Login ok: {username} from {ip}')
        session, csrf = self.app.auth.create_session()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.send_header(
            'Set-Cookie',
            f'pilot_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={86400 * 7}',
        )
        self.end_headers()
        self.wfile.write(json.dumps({
            'success': True,
            'csrf_token': csrf,
            'must_change_password': self.app.auth.must_change_password(),
        }).encode())

    def _logout(self):
        self.app.auth.logout()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.send_header('Set-Cookie', 'pilot_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict')
        self.end_headers()
        self.wfile.write(json.dumps({'success': True}).encode())

    def _control(self, data):
        led, mode = data.get('led', ''), data.get('action', '')
        if not validate_led(led) or not validate_mode(mode):
            return self._json(400, {'success': False, 'message': '无效参数'})
        ok, msg = self.app.ctrl.set_mode(led, mode)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _color(self, data):
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        try:
            r, g, b = int(data['r']), int(data['g']), int(data['b'])
        except (TypeError, ValueError, KeyError):
            return self._json(400, {'success': False, 'message': 'Invalid color'})
        ok, msg = self.app.ctrl.set_color(led, r, g, b)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _preset(self, data):
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        ok, msg = self.app.ctrl.apply_preset(led, data.get('preset', ''))
        color = self.app.ctrl.get_settings(led).get('color', [255, 255, 255])
        return self._json(200 if ok else 400, {'success': ok, 'message': msg, 'color': color})

    def _brightness(self, data):
        try:
            brightness = int(data.get('brightness', 255))
        except (TypeError, ValueError):
            return self._json(400, {'success': False, 'message': 'Invalid brightness'})
        if 'led' in data:
            if not validate_led(data['led']):
                return self._json(400, {'success': False, 'message': '无效 LED'})
            ok, msg = self.app.ctrl.set_brightness(data['led'], brightness)
        else:
            ok, msg = self.app.ctrl.set_global_brightness(brightness)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _all(self, mode):
        if not validate_mode(mode):
            return self._json(400, {'success': False, 'message': '无效模式'})
        if mode == 'off':
            ok, msg = self.app.ctrl.all_off()
            return self._json(200 if ok else 500, {'success': ok, 'message': msg})
        errors = []
        for led in self.app.led_names:
            ok, msg = self.app.ctrl.set_mode(led, mode)
            if not ok:
                errors.append(f'{led}: {msg}')
        return self._json(500 if errors else 200, {'success': not errors, 'message': '; '.join(errors)})

    def _activity_blink(self, data):
        enabled = bool(data.get('enabled', True))
        ok, msg = self.app.ctrl.set_activity_blink(enabled)
        self.app.cfg['activity_blink'] = enabled
        from utils import save_json
        from app_context import CONFIG_FILE
        save_json(CONFIG_FILE, self.app.cfg)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _effect(self, data):
        """Set/clear a per-LED effect (breath / manual-blink).

        effect = 'off' / null / missing clears the effect; 'blink' is the
        UI name mapped to backend 'manual-blink' (both are accepted).
        """
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        effect = data.get('effect')
        if effect in (None, '', 'off'):
            effect = None
        elif effect == 'blink':
            effect = 'manual-blink'
        if effect is not None and effect not in VALID_EFFECTS:
            return self._json(400, {'success': False, 'message': '无效效果'})
        t_on = data.get('t_on')
        t_off = data.get('t_off')
        try:
            t_on = int(t_on) if t_on is not None else None
            t_off = int(t_off) if t_off is not None else None
        except (TypeError, ValueError):
            return self._json(400, {'success': False, 'message': 'Invalid timing'})
        ok, msg = self.app.ctrl.set_effect(led, effect, t_on, t_off)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _chase(self, data):
        """Toggle the chase demo (running light)."""
        enabled = bool(data.get('enabled', True))
        ok, msg = self.app.ctrl.set_chase(enabled)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _speed_blink(self, data):
        """Toggle rate-aware blink, persisted to device_config."""
        enabled = bool(data.get('enabled', True))
        ok, msg = self.app.ctrl.set_speed_blink(enabled)
        self.app.cfg['speed_blink'] = enabled
        save_json(CONFIG_FILE, self.app.cfg)
        return self._json(200 if ok else 500, {'success': ok, 'message': msg})

    def _night_schedule(self, data):
        """Set the night auto-dim schedule, persisted to device_config."""
        ok, msg = self.app.ctrl.set_night_schedule(data)
        if not ok:
            return self._json(400, {'success': False, 'message': msg})
        self.app.cfg['night_schedule'] = dict(self.app.ctrl.night_schedule)
        save_json(CONFIG_FILE, self.app.cfg)
        return self._json(200, {
            'success': True, 'message': msg,
            'schedule': dict(self.app.ctrl.night_schedule),
            'night_active': self.app.ctrl.night_active,
        })

    def _alerts(self, data):
        """Configure failure alerts, persisted to device_config."""
        ok, msg = self.app.ctrl.set_alert_config(data)
        if not ok:
            return self._json(400, {'success': False, 'message': msg})
        self.app.cfg['alerts'] = dict(self.app.ctrl.alert_cfg)
        save_json(CONFIG_FILE, self.app.cfg)
        return self._json(200, {
            'success': True, 'message': msg,
            'alerts': self.app.ctrl.alerts,
            'alert_cfg': dict(self.app.ctrl.alert_cfg),
        })

    def _remap(self):
        ok, msg = self.app.ctrl.force_remap()
        return self._json(200 if ok else 500, {
            'success': ok, 'message': msg,
            'disk_map': self.app.ctrl.get_status().get('disk_map', {}),
        })

    def _calibrate_identify(self, data):
        """Blink the given LED so the user can locate the bay.

        Delegates to ctrl.identify() — the blink runs inside the controller
        lock (F2 finding 4), never a raw run() call from the handler.
        """
        led = data.get('led', '')
        if not validate_led(led):
            return self._json(400, {'success': False, 'message': '无效 LED'})
        ok, err = self.app.ctrl.identify(led)
        if not ok:
            return self._json(500, {'success': False, 'message': err or '识别失败'})
        return self._json(200, {'success': True, 'message': '识别中'})

    def _calibrate_bind(self, data):
        """Persist a manual slot -> hctl bay binding."""
        slot, hctl = data.get('slot', ''), data.get('hctl', '')
        if isinstance(slot, str) and slot.startswith('disk'):
            try:
                slot_num = int(slot[len('disk'):])
            except ValueError:
                return self._json(400, {'success': False, 'message': '无效盘位'})
        else:
            try:
                slot_num = int(slot)
            except (TypeError, ValueError):
                return self._json(400, {'success': False, 'message': '无效盘位'})
        if not (1 <= slot_num <= self.app.ctrl.disk_count):
            return self._json(400, {'success': False, 'message': '无效盘位'})
        if not validate_hctl(hctl):
            return self._json(400, {'success': False, 'message': '无效 HCTL'})
        key = f'disk{slot_num}'
        cfg = self.app.cfg
        cfg.setdefault('bay_bindings', {})
        cfg['bay_bindings'][key] = hctl
        save_json(CONFIG_FILE, cfg)
        self.app.ctrl.set_bay_bindings(cfg['bay_bindings'])
        return self._json(200, {'success': True, 'message': f'盘位 {slot_num} 已绑定 {hctl}'})

    def _reset_config(self):
        from app_context import SETTINGS_FILE, STATE_FILE
        for p in (STATE_FILE, SETTINGS_FILE):
            remove_file(p)
        self.app.reset_controller()
        return self._json(200, {'success': True})

    def _save_settings(self):
        self.app.ctrl._flush_settings()
        self.app.ctrl._persist()
        return self._json(200, {'success': True})

    def _change_password(self, data):
        ok, msg = self.app.auth.change_password(data.get('old_password', ''), data.get('new_password', ''))
        if ok:
            self.app.auth.logout()
        return self._json(200 if ok else 400, {'success': ok, 'message': msg})

    def _serve_static(self, rel_path):
        rel_path = unquote(rel_path).lstrip('/')
        if '..' in rel_path.split('/'):
            return self._json(403, {'success': False})
        full = os.path.join(WWW_DIR, rel_path)
        if not os.path.isfile(full):
            return self._json(404, {'success': False, 'message': 'Not found'})
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self._send_security_headers()
        if rel_path.endswith('.html'):
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        else:
            self.send_header('Content-Type', ctype)
            if rel_path.endswith(('.css', '.js')):
                self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _body(self):
        """Read and validate the request body.

        Returns the body ('' when absent), or None after sending a 400/413
        error response for a malformed or oversized Content-Length.
        """
        raw = self.headers.get('Content-Length', 0)
        try:
            n = int(raw)
        except (TypeError, ValueError):
            self._json(400, {'success': False, 'message': 'Invalid Content-Length'})
            return None
        if n < 0:
            self._json(400, {'success': False, 'message': 'Invalid Content-Length'})
            return None
        if n > MAX_BODY_BYTES:
            self._json(413, {'success': False, 'message': 'Request body too large'})
            return None
        return self.rfile.read(n) if n else ''

    def _json(self, status, data):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def log_message(self, *args):
        pass
