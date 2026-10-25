"""PNG written on every core: Pillow's encoder is one thread, and a 20480 px
picture took 14 minutes to save.

Rows get PNG's Up filter (numpy), then blocks of rows are deflated in
parallel as raw deflate streams ended with a sync flush -- byte-aligned, so
they concatenate into one stream (as pigz does) -- and wrapped in one zlib
header and Adler-32 for the IDAT chunks.
"""
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def save(img, path, level=6, rows_per_block=256):
    """PIL RGB image `img` as a PNG at `path`."""
    a = np.asarray(img.convert("RGB"), np.uint8)
    h, w = a.shape[:2]
    ranges = [(r, min(h, r + rows_per_block)) for r in range(0, h, rows_per_block)]

    def block(rng):
        r0, r1 = rng
        cur = a[r0:r1].reshape(r1 - r0, w * 3)
        prev = np.concatenate([a[r0 - 1].reshape(1, w * 3) if r0 else np.zeros((1, w * 3), np.uint8), cur[:-1]])
        rows = np.empty((r1 - r0, w * 3 + 1), np.uint8)
        rows[:, 0] = 2                                  # filter: Up
        rows[:, 1:] = cur - prev                        # uint8 wraps, as PNG wants
        raw = rows.tobytes()
        c = zlib.compressobj(level, zlib.DEFLATED, -15)
        last = r1 == h
        return raw, c.compress(raw) + c.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        parts = list(pool.map(block, ranges))
    adler = 1
    for raw, _ in parts:
        adler = zlib.adler32(raw, adler)
    stream = b"\x78\x01" + b"".join(d for _, d in parts) + struct.pack(">I", adler & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        step = 1 << 30
        for i in range(0, len(stream), step):
            f.write(_chunk(b"IDAT", stream[i:i + step]))
        f.write(_chunk(b"IEND", b""))
