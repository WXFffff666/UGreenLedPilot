"""Administrator authentication for UGreenLedPilot."""

import hashlib
import json
import os
import secrets
import time

SESSION_MAX_AGE = 86400 * 7  # 7 days


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
    """Single-admin password gate. All control APIs require valid session."""

    def __init__(self, auth_file):
        self.auth_file = auth_file
        self.cfg = load_json(auth_file, {})
        if 'password_hash' not in self.cfg and 'password' not in self.cfg:
            self.set_password('admin123')
        elif 'password' in self.cfg and 'password_hash' not in self.cfg:
            self.set_password(self.cfg['password'])
            del self.cfg['password']

    def _hash(self, password, salt=None):
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 120000)
        return salt, digest.hex()

    def set_password(self, password):
        salt, pw_hash = self._hash(password)
        self.cfg['password_hash'] = pw_hash
        self.cfg['password_salt'] = salt
        self.cfg['session'] = ''
        self.cfg['session_created'] = 0
        self.save()

    def verify_password(self, password):
        salt = self.cfg.get('password_salt', '')
        expected = self.cfg.get('password_hash', '')
        if not salt or not expected:
            return password == self.cfg.get('password', 'admin123')
        _, digest = self._hash(password, salt)
        return digest == expected

    def create_session(self):
        session = secrets.token_hex(32)
        self.cfg['session'] = session
        self.cfg['session_created'] = time.time()
        self.save()
        return session

    def validate_session(self, session):
        if not session or session != self.cfg.get('session', ''):
            return False
        created = self.cfg.get('session_created', 0)
        if created and time.time() - created > SESSION_MAX_AGE:
            self.logout()
            return False
        return True

    def logout(self):
        self.cfg['session'] = ''
        self.cfg['session_created'] = 0
        self.save()

    def change_password(self, old_password, new_password):
        if not self.verify_password(old_password):
            return False, '旧密码错误'
        if not new_password or len(new_password) < 6:
            return False, '新密码至少6位'
        self.set_password(new_password)
        return True, '密码已修改'

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
