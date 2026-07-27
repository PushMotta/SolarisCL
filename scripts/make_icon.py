#!/usr/bin/env python3
"""Draw the hsl application icon and write ``hsl/assets/hsl.ico``.

Pure standard library -- no Pillow, in keeping with the project's
no-dependencies rule. Re-run it after editing any constant below::

    python scripts/make_icon.py

The mark is a solar eclipse: a lit crescent with a corona, on a dark violet
app tile. Every size is drawn from vector maths rather than downscaled from one
bitmap, so the 16 px taskbar version stays crisp instead of turning to mush.

Shapes were chosen for what survives at 16 px, which is where the taskbar
actually draws this. Thin rings, rays and anything with two competing details
turn to porridge at that size; a single bold silhouette does not.
"""

from __future__ import annotations

import math
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "hsl", "assets", "hsl.ico")

# Windows picks the nearest size per surface: 16 in the taskbar and title bar,
# 20/24/40 at 125-250% display scaling, 32 in alt-tab, 48 in Explorer, 256 for
# the large tile. Leaving one out makes the shell downscale a bigger bitmap
# itself, which is exactly the muddy result we are avoiding.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# Vista and later accept PNG-compressed entries, and for the big sizes it saves
# most of the file. The small ones stay uncompressed BMP, which every shell
# surface has always understood.
PNG_FROM = 128

# --- palette ---------------------------------------------------------------
TILE_DARK = (0x17, 0x0B, 0x2E)    # near-black violet, top-left of the tile
TILE_LIT = (0x43, 0x1A, 0x8A)     # deep violet, bottom-right
CORONA_HOT = (0xF7, 0xF5, 0xFF)   # near-white, the bright edge of the crescent
CORONA_COOL = (0xC4, 0xB5, 0xFD)  # lavender, its cooler side
UMBRA = (0x10, 0x07, 0x22)        # the eclipse shadow itself

# --- geometry, all in units of the icon's width so it scales exactly --------
TILE_HALF = 0.47      # half-width of the rounded square
TILE_CORNER = 0.14    # its corner radius
DISC_R = 0.315        # the sun
SHADOW_X = 0.605      # the occluding body, offset up and to the right
SHADOW_Y = 0.425
SHADOW_R = 0.255
GLOW = 0.075          # corona falloff outside the disc
GLOW_STRENGTH = 0.40
SUPERSAMPLE = 4       # samples per pixel per axis -- 16 per pixel, i.e. the AA


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def _in_tile(x: float, y: float) -> bool:
    """Rounded square, the shape Windows 11 app icons tend to use."""
    hx, hy = abs(x - 0.5), abs(y - 0.5)
    flat = TILE_HALF - TILE_CORNER
    if hx <= flat and hy <= flat:
        return True
    return math.hypot(max(hx - flat, 0.0), max(hy - flat, 0.0)) <= TILE_CORNER


def _shade(x: float, y: float):
    """Colour at a point, or None outside the tile."""
    if not _in_tile(x, y):
        return None

    tile = _lerp(TILE_DARK, TILE_LIT,
                 min(1.0, max(0.0, ((x - 0.03) + (y - 0.03)) / 1.88)))

    d = math.hypot(x - 0.5, y - 0.5)
    shadow = math.hypot(x - SHADOW_X, y - SHADOW_Y)

    if d <= DISC_R:
        if shadow > SHADOW_R:
            # The lit crescent: hottest toward the bottom-left, where the
            # occluding body has moved furthest away.
            h = min(1.0, max(0.0, ((x - 0.5) + (y - 0.5)) / (2 * DISC_R) + 0.5))
            return _lerp(CORONA_HOT, CORONA_COOL, h)
        return _lerp(tile, UMBRA, 0.55)

    glow = math.exp(-(((d - DISC_R) / GLOW) ** 2))
    return _lerp(tile, CORONA_COOL, GLOW_STRENGTH * glow)


def _render(size: int) -> bytearray:
    """Rasterise the mark at ``size`` into top-down RGBA bytes."""
    ss = SUPERSAMPLE
    per_pixel = ss * ss
    step = 1.0 / (size * ss)
    half_step = step / 2.0

    out = bytearray(size * size * 4)
    for py in range(size):
        for px in range(size):
            red = green = blue = 0.0
            covered = 0
            for sy in range(ss):
                y = (py * ss + sy) * step + half_step
                for sx in range(ss):
                    sample = _shade((px * ss + sx) * step + half_step, y)
                    if sample is None:
                        continue        # outside the tile -- stays transparent
                    red += sample[0]
                    green += sample[1]
                    blue += sample[2]
                    covered += 1

            if covered:
                i = (py * size + px) * 4
                out[i] = int(red / covered)
                out[i + 1] = int(green / covered)
                out[i + 2] = int(blue / covered)
                out[i + 3] = (covered * 255) // per_pixel
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(size: int, rgba: bytearray) -> bytes:
    """Encode RGBA as a PNG (colour type 6, no filtering)."""
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)                       # filter type 0 for this scanline
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def _dib(size: int, rgba: bytearray) -> bytes:
    """A 32-bit bottom-up DIB plus the 1-bit AND mask .ico still expects.

    The alpha channel is what actually gets composited on anything since XP,
    but the mask has to be there and be right, or old shell surfaces draw the
    icon as a black square.
    """
    # biHeight is doubled: the format counts the colour rows and mask rows.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)

    xor = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for i in range(0, len(row), 4):
            xor += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))   # BGRA

    row_bytes = ((size + 31) // 32) * 4     # 1 bit per pixel, rows 4-byte aligned
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = bytearray(row_bytes)
        for x in range(size):
            if rgba[(y * size + x) * 4 + 3] == 0:
                bits[x >> 3] |= 0x80 >> (x & 7)      # 1 = transparent
        mask += bits

    return header + bytes(xor) + bytes(mask)


def _ico(images) -> bytes:
    """Pack ``[(size, payload), ...]`` into an ICO container."""
    entries = bytearray()
    body = bytearray()
    offset = 6 + 16 * len(images)
    for size, payload in images:
        dim = 0 if size >= 256 else size      # 256 is encoded as 0 in one byte
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(payload), offset)
        body += payload
        offset += len(payload)
    return struct.pack("<HHH", 0, 1, len(images)) + bytes(entries) + bytes(body)


def main() -> int:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    images = []
    for size in SIZES:
        rgba = _render(size)
        payload = _png(size, rgba) if size >= PNG_FROM else _dib(size, rgba)
        kind = "PNG" if size >= PNG_FROM else "BMP"
        images.append((size, payload))
        print(f"  {size:>3}x{size:<3}  {kind}  {len(payload):>7,} bytes")

    blob = _ico(images)
    with open(OUT_PATH, "wb") as fh:
        fh.write(blob)
    print(f"wrote {OUT_PATH}  ({len(blob):,} bytes, {len(images)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
