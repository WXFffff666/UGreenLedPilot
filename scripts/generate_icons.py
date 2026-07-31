#!/usr/bin/env python3
"""Generate placeholder icons for fnpack build."""

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'src'


def png_chunk(tag, data):
    crc = zlib.crc32(tag + data) & 0xffffffff
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)


def write_png(path, size, rgb=(0, 229, 160)):
    r, g, b = rgb
    width = height = size
    raw = b''.join(
        b'\x00' + bytes([r, g, b]) * width
        for _ in range(height)
    )
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    data = b'\x89PNG\r\n\x1a\n'
    data += png_chunk(b'IHDR', ihdr)
    data += png_chunk(b'IDAT', zlib.compress(raw, 9))
    data += png_chunk(b'IEND', b'')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f'Wrote {path}')


def main():
    write_png(ROOT / 'ICON.PNG', 64)
    write_png(ROOT / 'ICON_256.PNG', 256)
    write_png(ROOT / 'app' / 'ui' / 'images' / 'icon_64.png', 64)
    write_png(ROOT / 'app' / 'ui' / 'images' / 'icon_256.png', 256)


if __name__ == '__main__':
    main()
