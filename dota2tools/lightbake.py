"""Light baked into a texture, from the world's own geometry.

For surfaces the map no longer lights the way it used to (the lava: its
light came from its own glow, baked into the map's lightmaps by an older
compile): a height field of everything around, read from the map's
world-node models, then per point of the surface its view of the sun (a soft
shadow, marched through the height field toward the map's light direction)
and of the sky (a horizon ambient occlusion over eight directions).
"""
import math

import numpy as np


def height_field(meshes, region, res, rng_seed=0):
    """The highest geometry over the region (x0, x1, y0, y1), res units per
    cell: (H, (x0, y1, res)) with H[row, col], rows from the top (y1). Each
    triangle sampled about twice per cell it covers."""
    x0, x1, y0, y1 = region
    nx, ny = int(math.ceil((x1 - x0) / res)), int(math.ceil((y1 - y0) / res))
    H = np.full((ny, nx), -1e9, np.float32)
    rng = np.random.default_rng(rng_seed)
    for _rel, P, T in meshes:
        V = P[T]                                   # (m, 3, 3)
        lo, hi = V.min(1), V.max(1)
        keep = (hi[:, 0] >= x0) & (lo[:, 0] <= x1) & (hi[:, 1] >= y0) & (lo[:, 1] <= y1)
        V = V[keep]
        if not len(V):
            continue
        e1, e2 = V[:, 1] - V[:, 0], V[:, 2] - V[:, 0]
        area = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
        n = np.clip(np.ceil(2 * area / (res * res)), 1, 4000).astype(np.int64) + 2
        idx = np.repeat(np.arange(len(V)), n)
        r1, r2 = rng.random(len(idx), np.float32), rng.random(len(idx), np.float32)
        s = np.sqrt(r1)
        a, b = 1 - s, s * (1 - r2)
        pts = V[idx, 0] * a[:, None] + V[idx, 1] * b[:, None] + V[idx, 2] * (1 - a - b)[:, None]
        cx = ((pts[:, 0] - x0) / res).astype(np.int64)
        cy = ((y1 - pts[:, 1]) / res).astype(np.int64)
        ok = (cx >= 0) & (cx < nx) & (cy >= 0) & (cy < ny)
        np.maximum.at(H, (cy[ok], cx[ok]), pts[ok, 2])
    # Random samples leave the odd cell of a steep or thin surface empty: the
    # highest of each 3 x 3.
    import cv2
    H = cv2.dilate(H, np.ones((3, 3), np.uint8))
    return H, (x0, y1, res)


def _fill(H):
    """Cells nothing covered take their neighbours' height."""
    import cv2
    empty = H < -1e8
    if not empty.any():
        return H
    H = H.copy()
    for _ in range(64):
        if not empty.any():
            break
        grown = cv2.dilate(np.where(empty, -1e9, H).astype(np.float32), np.ones((3, 3), np.uint8))
        fill = empty & (grown > -1e8)
        H[fill] = grown[fill]
        empty &= ~fill
    H[empty] = 0
    return H


def _shift(A, dx, dy):
    """A moved by (dx, dy) cells (bilinear; edges repeated): out[r, c] =
    A[r + dy, c + dx]."""
    import cv2
    M = np.float32([[1, 0, -dx], [0, 1, -dy]])
    return cv2.warpAffine(A, M, (A.shape[1], A.shape[0]), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def shading(H, Z, res, pitch, yaw, reach=1600.0, ao_radius=360.0, softness=6.0, bias=24.0):
    """Per cell of a surface at heights Z under the height field H (same
    grid): (sun visibility 0..1, ambient occlusion 0..1). pitch, yaw: the
    map's light direction (degrees: how steeply and which way its light
    travels). Only what stands `bias` units above the surface occludes it:
    ground level with it (a bank the surface runs under) doesn't."""
    el, az = math.radians(pitch), math.radians(yaw)
    # The light's angles point where its light goes (shadows in the render
    # fall that way); toward the sun is the other way. In cells: +x is +col,
    # +y is -row.
    sx, sy = -math.cos(az), math.sin(az)
    rise = math.tan(el)
    vis = np.ones_like(Z)
    t = res
    while t <= reach:
        h = _shift(H, sx * t / res, sy * t / res)
        occl = (h - (Z + t * rise + bias)) / t
        vis = np.minimum(vis, np.clip(1.0 - softness * occl, 0, 1))
        t += max(res, t * 0.06)
    ao = np.zeros_like(Z)
    dirs = 8
    for k in range(dirs):
        a = 2 * math.pi * k / dirs
        dx, dy = math.cos(a), -math.sin(a)
        slope = np.zeros_like(Z)
        t = res
        while t <= ao_radius:
            h = _shift(H, dx * t / res, dy * t / res)
            slope = np.maximum(slope, (h - Z - bias) / t)
            t += max(res, t * 0.15)
        ao += 1.0 - np.sin(np.arctan(slope))
    return vis, ao / dirs


def rasterize(P, tris, vals, region, res):
    """Per-corner values `vals` (n, k) of triangles over world positions P
    (n, 3), interpolated over the grid of region (x0, x1, y0, y1) at res
    units per cell, seen from above: (grid (rows, cols, k), covered)."""
    x0, x1, y0, y1 = region
    W, H = int(math.ceil((x1 - x0) / res)), int(math.ceil((y1 - y0) / res))
    out = np.zeros((H, W, vals.shape[1]), np.float32)
    top = np.full((H, W), -1e9, np.float32)
    for tri in tris:
        p = P[tri]
        px, py = (p[:, 0] - x0) / res, (y1 - p[:, 1]) / res
        c0, c1 = max(0, int(px.min())), min(W - 1, int(math.ceil(px.max())))
        r0, r1 = max(0, int(py.min())), min(H - 1, int(math.ceil(py.max())))
        if c1 < c0 or r1 < r0:
            continue
        d = (py[1] - py[2]) * (px[0] - px[2]) + (px[2] - px[1]) * (py[0] - py[2])
        if abs(d) < 1e-9:
            continue
        gx, gy = np.meshgrid(np.arange(c0, c1 + 1) + 0.5, np.arange(r0, r1 + 1) + 0.5)
        l1 = ((py[1] - py[2]) * (gx - px[2]) + (px[2] - px[1]) * (gy - py[2])) / d
        l2 = ((py[2] - py[0]) * (gx - px[2]) + (px[0] - px[2]) * (gy - py[2])) / d
        l3 = 1 - l1 - l2
        inside = (l1 >= -1e-3) & (l2 >= -1e-3) & (l3 >= -1e-3)
        z = l1 * p[0, 2] + l2 * p[1, 2] + l3 * p[2, 2]
        sel = inside & (z > top[r0:r1 + 1, c0:c1 + 1])
        if not sel.any():
            continue
        v = vals[tri]
        val = l1[..., None] * v[0] + l2[..., None] * v[1] + l3[..., None] * v[2]
        out[r0:r1 + 1, c0:c1 + 1][sel] = val[sel]
        top[r0:r1 + 1, c0:c1 + 1][sel] = z[sel]
    return out, top > -1e8
