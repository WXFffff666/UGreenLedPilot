#!/usr/bin/env python3
"""Packaging-structure tests for fnOS store acceptance.

These tests pin the store-critical invariants of the fnOS package under src/:
- manifest must use `platform` (not the deprecated `arch`) and declare os_min_version
- privilege must be valid JSON with a lowercase username/groupname
- resource must be valid JSON
- wizard files must exist and be valid JSON arrays
- required icons and the bundled CLI binary must be present

The fnOS app store rejects packages ("应用包格式不符合系统版本要求", code 10111)
when these invariants are violated, so they are guarded in CI.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / 'src'


def read_manifest() -> str:
    return (SRC / 'manifest').read_text(encoding='utf-8')


def manifest_field(name: str) -> str | None:
    for line in read_manifest().splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        key, _, value = line.partition('=')
        if key.strip() == name:
            return value.strip()
    return None


class TestManifest(unittest.TestCase):
    def test_no_deprecated_arch_field(self):
        # The `arch` FIELD (arch = ...) is deprecated; substrings inside
        # changelog/desc prose are fine. Match a field-definition line only.
        for line in read_manifest().splitlines():
            line = line.strip()
            if not line or '=' not in line:
                continue
            key = line.split('=', 1)[0].strip()
            self.assertNotEqual(key, 'arch',
                                'manifest must not use deprecated `arch` field (fnOS 1.x)')

    def test_platform_is_x86(self):
        self.assertEqual(manifest_field('platform'), 'x86',
                         'platform=x86 required for x86_64 native binary')

    def test_os_min_version_declared(self):
        self.assertIsNotNone(manifest_field('os_min_version'),
                             'os_min_version must be declared')

    def test_required_fields_present(self):
        for field in ('appname', 'version', 'display_name', 'desc', 'source'):
            self.assertIsNotNone(manifest_field(field),
                                 f'missing required manifest field: {field}')

    def test_version_semver(self):
        self.assertRegex(manifest_field('version'), r'^\d+\.\d+\.\d+$',
                         'version must be X.Y.Z semver')

    def test_service_port_present(self):
        self.assertIsNotNone(manifest_field('service_port'),
                             'service_port must be declared')


class TestPrivilege(unittest.TestCase):
    def test_privilege_valid_json(self):
        data = json.loads((SRC / 'config' / 'privilege').read_text(encoding='utf-8'))
        self.assertIn('defaults', data)
        self.assertIn('username', data)
        self.assertIn('groupname', data)

    def test_username_lowercase(self):
        data = json.loads((SRC / 'config' / 'privilege').read_text(encoding='utf-8'))
        self.assertTrue(re.fullmatch(r'[a-z][a-z0-9_]{2,31}', data['username']),
                        f'username must be lowercase a-z/0-9/_ start-with-letter, got {data["username"]}')

    def test_groupname_lowercase(self):
        data = json.loads((SRC / 'config' / 'privilege').read_text(encoding='utf-8'))
        self.assertTrue(re.fullmatch(r'[a-z][a-z0-9_]{2,31}', data['groupname']),
                        f'groupname must be lowercase a-z/0-9/_ start-with-letter, got {data["groupname"]}')


class TestResource(unittest.TestCase):
    def test_resource_valid_json(self):
        json.loads((SRC / 'config' / 'resource').read_text(encoding='utf-8'))


class TestWizard(unittest.TestCase):
    def test_wizard_dir_exists(self):
        # fnpack 1.2.3 requires the wizard/ directory to exist. Individual
        # wizard files are optional and MUST NOT be present unless they are
        # valid fnOS wizard-step JSON — an empty/0-byte or malformed wizard
        # file triggers "应用包格式不符合系统版本要求" (code 10111) on fnOS 1.x.
        self.assertTrue((SRC / 'wizard').is_dir(),
                        'wizard/ directory must exist')

    def test_no_empty_wizard_files(self):
        # Any present wizard file must be non-empty valid JSON. An empty or
        # 0-byte wizard file is rejected by the fnOS app store.
        wizard_dir = SRC / 'wizard'
        for path in wizard_dir.iterdir():
            if path.is_file():
                content = path.read_text(encoding='utf-8').strip()
                self.assertTrue(content, f'wizard file {path.name} must not be empty')
                self.assertIsInstance(json.loads(content), list,
                                      f'wizard/{path.name} must be a JSON array')


class TestIconsAndBinary(unittest.TestCase):
    def test_icons_present(self):
        self.assertTrue((SRC / 'ICON.PNG').exists())
        self.assertTrue((SRC / 'ICON_256.PNG').exists())

    def test_cli_binary_present(self):
        cli = SRC / 'app' / 'server' / 'ugreen_leds_cli'
        self.assertTrue(cli.exists(), 'bundled ugreen_leds_cli binary must be present')
        self.assertGreater(cli.stat().st_size, 1_000_000,
                           'ugreen_leds_cli should be >1MB (real ELF binary)')


if __name__ == '__main__':
    unittest.main()