"""AA-954 (CRM-24b): rasterise the CRM web's own mark (web/app/icon.svg: ink rounded square, gold peak) to the
installable traveller page's PNG icons, with no image library. Run from the repo root:

    python3 tools/pwa/pwa_icons.py web/public/c/icons
"""

import struct
import sys
import zlib
from pathlib import Path

INK = (0x1F, 0x29, 0x33)
GOLD = (0xDB, 0x96, 0x28)
PEAK = [(9, 23), (16, 8), (23, 23), (19.6, 23), (16, 15), (12.4, 23)]
SS = 4


def inside(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def in_rrect(x, y, r):
    # 32x32 square with corner radius r (in 32-unit space)
    cx = min(max(x, r), 32 - r)
    cy = min(max(y, r), 32 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size, rounded, mark_scale):
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) * 32 / size
                    y = (py + (sy + 0.5) / SS) * 32 / size
                    if rounded and not in_rrect(x, y, 7):
                        continue
                    # the mark scaled about the centre
                    mx = 16 + (x - 16) / mark_scale
                    my = 16 + (y - 16) / mark_scale
                    col = GOLD if inside(PEAK, mx, my) else INK
                    acc[0] += col[0]
                    acc[1] += col[1]
                    acc[2] += col[2]
                    acc[3] += 255
            n = SS * SS
            a = acc[3] // n
            if acc[3]:
                k = acc[3] // 255
                row += bytes([acc[0] // k, acc[1] // k, acc[2] // k, a])
            else:
                row += bytes([0, 0, 0, 0])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
(out / "trip-192.png").write_bytes(render(192, True, 1.0))
(out / "trip-512.png").write_bytes(render(512, True, 1.0))
(out / "trip-maskable-512.png").write_bytes(render(512, False, 0.75))
(out / "trip-apple-180.png").write_bytes(render(180, False, 0.85))
