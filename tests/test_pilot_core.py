#!/usr/bin/env python3
"""Unit tests for UGreenLedPilot disk mapping."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'app' / 'server'))

from pilot_core import (
    map_disks_by_profile, disk_present, quick_hardware_signature,
    DEFAULT_SETTINGS, NETDEV_ORANGE, WHITE,
)


class TestDiskMapping(unittest.TestCase):
    def test_dxp6800_ata_mapping(self):
        devices = {
            'sda': {'ata': 'ata1', 'hctl': '0:0:0:0'},
            'sdb': {'ata': 'ata2', 'hctl': '1:0:0:0'},
            'sdc': {'ata': 'ata3', 'hctl': '2:0:0:0'},
            'sdd': {'ata': 'ata4', 'hctl': '3:0:0:0'},
            'sde': {'ata': 'ata5', 'hctl': '4:0:0:0'},
            'sdf': {'ata': 'ata6', 'hctl': '5:0:0:0'},
        }
        ata_map = ['ata3', 'ata4', 'ata5', 'ata6', 'ata1', 'ata2']
        result = map_disks_by_profile(devices, ata_map=ata_map, disk_count=6)
        self.assertEqual(result[1], 'sdc')
        self.assertEqual(result[4], 'sda')

    def test_default_hctl(self):
        devices = {'sda': {'hctl': '0:0:0:0'}, 'sdb': {'hctl': '1:0:0:0'}}
        result = map_disks_by_profile(
            devices, hctl_map=['0:0:0:0', '1:0:0:0'], disk_count=2, mapping_method='hctl',
        )
        self.assertEqual(result, {1: 'sda', 2: 'sdb'})


class TestDefaults(unittest.TestCase):
    def test_netdev_orange_power_white(self):
        self.assertEqual(DEFAULT_SETTINGS['power']['color'], WHITE)
        self.assertEqual(DEFAULT_SETTINGS['netdev']['color'], NETDEV_ORANGE)
        self.assertEqual(DEFAULT_SETTINGS['disk1']['color'], WHITE)


class TestHotplugSignature(unittest.TestCase):
    def test_signature_is_hashable(self):
        sig = quick_hardware_signature()
        self.assertIsInstance(sig, tuple)


if __name__ == '__main__':
    unittest.main()
