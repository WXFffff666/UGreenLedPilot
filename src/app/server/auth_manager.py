"""Administrator authentication for UGreenLedPilot."""

import hashlib
import json
import os
import secrets
import time

SESSION_MAX_AGE = 86400 * 7  # 7 days
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 900  # 15 minutes
PBKDF2_ITERATIONS = 200000


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


class AuthManager:
    """Single-admin password gate with rate limiting and CSRF tokens."""

    def __init__(self, auth_file):
        self.auth_file = auth_file
        self.cfg = load_json(auth_file, {})
        self._login_attempts = {}
        if 'password_hash' not in self.cfg and 'password' not in self.cfg:
            self.set_password('admin123')
            self.cfg['must_change_password'] = True
            self.save()
        elif 'password' in self.cfg and 'password_hash' not in self.cfg:
            self.set_password(self.cfg['password'])
            del self.cfg['password']

    def _hash(self, password, salt=None):
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            'sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS,
        )
        return salt, digest.hex()

    def set_password(self, password):
        salt, pw_hash = self._hash(password)
        self.cfg['password_hash'] = pw_hash
        self.cfg['password_salt'] = salt
        self.cfg['session'] = ''
        self.cfg['csrf_token'] = ''
        self.cfg['session_created'] = 0
        self.save()

    def verify_password(self, password):
        salt = self.cfg.get('password_salt', '')
        expected = self.cfg.get('password_hash', '')
        if not salt or not expected:
            fallback = self.cfg.get('password', 'admin123')
            return secrets.compare_digest(password, fallback)
        _, digest = self._hash(password, salt)
        return secrets.compare_digest(digest, expected)

    def _client_key(self, ip):
        return ip or 'unknown'

    def is_locked_out(self, ip):
        key = self._client_key(ip)
        entry = self._login_attempts.get(key)
        if not entry:
            return False
        _, lock_until = entry
        if lock_until and time.time() < lock_until:
            return True
        if lock_until and time.time() >= lock_until:
            self._login_attempts.pop(key, None)
        return False

    def lockout_remaining(self, ip):
        key = self._client_key(ip)
        entry = self._login_attempts.get(key)
        if not entry:
            return 0
        _, lock_until = entry
        if lock_until and time.time() < lock_until:
            return int(lock_until - time.time())
        return 0

    def record_failed_login(self, ip):
        key = self._client_key(ip)
        count, lock_until = self._login_attempts.get(key, (0, 0))
        count += 1
        if count >= MAX_LOGIN_ATTEMPTS:
            lock_until = time.time() + LOCKOUT_SECONDS
            count = 0
        self._login_attempts[key] = (count, lock_until)
        print(f'Login failed from {ip} (attempt {count}/{MAX_LOGIN_ATTEMPTS})')

    def record_successful_login(self, ip):
        self._login_attempts.pop(self._client_key(ip), None)

    def create_session(self):
        session = secrets.token_hex(32)
        csrf = secrets.token_hex(24)
        self.cfg['session'] = session
        self.cfg['csrf_token'] = csrf
        self.cfg['session_created'] = time.time()
        self.save()
        return session, csrf

    def validate_session(self, session):
        if not session:
            return False
        stored = self.cfg.get('session', '')
        if not stored or not secrets.compare_digest(session, stored):
            return False
        created = self.cfg.get('session_created', 0)
        if created and time.time() - created > SESSION_MAX_AGE:
            self.logout()
            return False
        return True

    def validate_csrf(self, token):
        stored = self.cfg.get('csrf_token', '')
        if not stored or not token:
            return False
        return secrets.compare_digest(token, stored)

    def get_csrf_token(self):
        return self.cfg.get('csrf_token', '')

    def logout(self):
        self.cfg['session'] = ''
        self.cfg['csrf_token'] = ''
        self.cfg['session_created'] = 0
        self.save()

    def change_password(self, old_password, new_password):
        if not self.verify_password(old_password):
            return False, '旧密码错误'
        if not new_password or len(new_password) < 8:
            return False, '新密码至少8位'
        if new_password == 'admin123':
            return False, '请勿使用默认密码'
        self.set_password(new_password)
        self.cfg['must_change_password'] = False
        self.save()
        return True, '密码已修改，请重新登录'

    def must_change_password(self):
        return bool(self.cfg.get('must_change_password', False))

    def parse_cookie(self, cookie_header):
        cookies = {}
        for item in cookie_header.split(';'):
            if '=' in item:
                k, v = item.strip().split('=', 1)
                cookies[k] = v
        return cookies.get('pilot_session', '')

    def is_authenticated(self, headers):
        return self.validate_session(self.parse_cookie(headers.get('Cookie', '')))

    def save(self):
        save_json(self.auth_file, self.cfg)
