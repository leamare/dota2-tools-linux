"""Tiles for devilesk's dota-interactive-map, and pictures saved for the web.

The interactive map shows a square picture through zoom levels of 256 px
tiles: level N-1 is the picture itself (padded to a multiple of the tile),
each level below half the one above, down to 1280 px (five levels for a
20480 px picture): <folder>/<level>/tile_<x>_<y>.<ext>. Its own tiles are
JPEG at quality 25 (docs/05-mapimage.md); its tile URL ends in .jpg, so
WebP and PNG tiles need that changed in the map's code.
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FORMATS = ("jpg", "webp", "png")
# jpg: the live map's. webp 30: smaller than JPEG 25 and sharper (measured on the map:
# 230 KB, 30.1 dB against 257 KB, 29.4 dB).
DEFAULT_QUALITY = {"jpg": 25, "webp": 30, "png": 0}


def save_web(img, path, fmt, quality=None):
    """`img` as a web picture: JPEG progressive and optimised (4:2:0), WebP
    at the slowest, smallest setting, PNG optimised. `path` without its
    extension; returns the path written."""
    fmt = fmt.lower().replace("jpeg", "jpg")
    q = DEFAULT_QUALITY[fmt] if quality in (None, "", 0) and fmt != "png" else quality
    path = Path(str(path) + "." + fmt)
    img = img.convert("RGB")
    if fmt == "jpg":
        img.save(path, "JPEG", quality=int(q), optimize=True, progressive=True, subsampling=2)
    elif fmt == "webp":
        img.save(path, "WEBP", quality=int(q), method=6)
    elif fmt == "png":
        img.save(path, "PNG", optimize=True)
    else:
        raise SystemExit(f"unknown format {fmt}: {', '.join(FORMATS)}")
    return path


def levels_for(size, tile=256, smallest=1280):
    """Zoom levels for a picture of `size` px: halving down to `smallest`."""
    return max(1, int(math.floor(math.log2(max(size, smallest) / smallest))) + 1)


def make(picture, folder, fmt="jpg", quality=None, tile=256, levels=0, log=print, progress=None):
    """Picture file `picture` cut into the interactive map's tiles in `folder`."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    img = Image.open(picture).convert("RGB")
    size = max(img.size)
    levels = levels or levels_for(size, tile)
    folder = Path(folder)
    total = sum((math.ceil(size / 2 ** (levels - 1 - lv) / tile)) ** 2 for lv in range(levels))
    done = [0]
    log(f"tiles: {Path(picture).name}, {img.size[0]} x {img.size[1]} px, {levels} levels, {total} {fmt} tiles")
    cur = img
    for lv in range(levels - 1, -1, -1):
        side = math.ceil(max(cur.size) / tile) * tile
        if cur.size != (side, side):
            pad = Image.new("RGB", (side, side))
            pad.paste(cur, (0, 0))
            cur = pad
        d = folder / str(lv)
        d.mkdir(parents=True, exist_ok=True)
        n = side // tile

        def one(xy, cur=cur, d=d):
            x, y = xy
            save_web(cur.crop((x * tile, y * tile, (x + 1) * tile, (y + 1) * tile)), d / f"tile_{x}_{y}", fmt, quality)

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for _ in pool.map(one, [(x, y) for y in range(n) for x in range(n)]):
                done[0] += 1
                if progress and done[0] % 64 == 0:
                    progress(f"level {lv}: {n} x {n}", done[0] / total)
        log(f"tiles level {lv}: {n} x {n}")
        if lv:
            cur = cur.resize((cur.size[0] // 2, cur.size[1] // 2), Image.LANCZOS)
    if progress:
        progress("done", 1.0)
    return folder
