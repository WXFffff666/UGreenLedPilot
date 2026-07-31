"""Shared utilities for UGreenLedPilot."""

import json
import os


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f'Load failed: {path} — {e}')
    return default


def save_json(path, data):
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f'Save FAILED: {path} — {e}')


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except IOError as e:
        return str(e)
    return None
