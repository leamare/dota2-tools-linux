"""Pre-run script: the lava, glowing.

The map's lava (on dota: the channels along the Dire base's border) is
terrain on a blend material whose lava layers glow through a self-illum
mask. The map's world meshes carry an emissive factor of 0 (in the map
itself, which a render addon can't change), so in the tools the lava draws
as dark, unlit rock. Here the lava is copied into render-addon models on an
unlit material of the lava layer's own texture, and put into the SFM session
just above the map's. Roshan's pit and the small pools keep their original
meshes and get a plain light bake, without growth or grass removal. On dota,
builtin:dire_lava_corner (with this script in the pre-run list too) joins the
Dire base's three connected channels round its corner. Large joined channels
are tiled, but every tile samples one light field and one coarse coverage
field for the whole spatial group, avoiding brightness and false-border
seams. Tile meshes clip to non-overlapping ownership rectangles inside their
gutters.

In the joined corner, lava grown up to stones and raised ground stays at the
lava's own height and reaches a little under them, so its edge is hidden
beneath them. Grass and identified tree-leaf cards rooted in the final lava
coverage (not on a bank above it) are removed; all survivors keep their
original UVs and cast no sun shadows.

Options (settings.json, render.lava): "lift" (units above the map and corner
ground, default 4), "brightness" (default 1.5), "repeat" (world units per lava
texture repeat, default 384), "texture_res" (world units per texel, default
0.75; lower = sharper, sampled bicubically), "max_side" (bake atlas cap, default
4096; 8192 is the tools' PNG loader limit), "tile_overlap" (world units
neighbouring atlas tiles share, default 224, at least twice feather plus 32),
"feather" (alpha fade at the north/north-east outer
edge, default 16), "bridge" (morphological close radius to fill gaps between
nearby lava, default 512), "extend" (grow past the closed silhouette toward
stones, default 128), "max_rise" (ground this far above the nearest lava still
accepts growth; taller stones stop it, default 128), "paint_min" (source blend
weight counted as lava, default 0.1), "under_props" (how far grown lava reaches
under stones and raised ground, default 48), "prop_rise" (how much higher than
the lava counts as raised, default 24), "edge_soften" (the outline's smoothing,
default 18), "edge_soft_width" (its fade, default 14), "bank" (units
the north/north-east outer edge darkens over, default 32), "bank_floor" (darkest
bank multiplier, default 0.35), "bank_texture" (blend highground rock into outer
banks, default true), "mesh_res" (overlay grid step, default 6), "edge_feather"
(local silhouette antialiasing, default 6), "shared_atlas"
(default false), "grass_noshadow" (default true), "roshan_brightness"
(Roshan-only plain-bake gain, default 1.8), "roshan_edge" (Roshan's
right/bottom fade to black, default 96), "corner_min_y" and
"corner_south_fade" (the joined overlay's crossfade into the plain southern
channel, 4096 and 512). Growth happens at mesh_res and cannot jump across stone
banks. The manual join clears the terrain/corner base but stays below stone
props. Huge channels are split into balanced overlapping tiles so each atlas
stays within max_side at texture_res.
"""
import math
import re
from pathlib import Path

from dota2tools import mapinfo
from dota2tools.prerun import corners
from dota2tools.prerun import dire_lava_corner as dlc

FOLDER = "models/dota2tools"
GRASS = (
    "materials/models/props_nature/grass_clump_00a_brown.vmat",
    "materials/models/props_nature/grass_clump_tintable_02.vmat",
)
DIRE_LEAVES = "materials/models/props_tree/tree_oak_leaves_08.vmat"
# A material that draws nothing: its colour cleared to transparent and alpha
# tested away (SFM draws a translucent one at opacity 0 as opaque black).
HIDDEN = {"F_ALPHA_TEST": 1, "F_TRANSLUCENT": 0, "F_ADDITIVE_BLEND": 0,
          "F_DO_NOT_CAST_SHADOWS": 1, "g_flAlphaTestReference": 0.5}


def lava_layer(m):
    """The layer of multiblend material m that glows (a self-illum map in its
    specular slot), or None."""
    if not m or not m["shader"].startswith("multiblend"):
        return None
    for g, vtex in sorted(m["textures"].items()):
        mt = re.match(r"g_tSpecular(\d)$", g)
        if mt and "selfillum" in vtex and f"g_tColor{mt.group(1)}" in m["textures"]:
            return int(mt.group(1))
    return None


def layer_paint(streams, layer):
    """The blend weight of a layer at each face corner. Newer terrain meshes
    keep VertexPaintBlendParams in texcoord$2; older ones in texcoord$1.
    None means an entirely unpainted model: keep all of it."""
    import numpy as np
    w = streams.get("texcoord$2")
    if w is None or not np.any(w[:, :3]):
        w = streams.get("texcoord$1")
    if w is None or w.shape[1] < 3 or not 0 <= layer <= 3 or not np.any(w[:, :3]):
        return None
    w = np.clip(w[:, :3], 0, 1)
    l1 = w[:, 0]
    l2 = np.minimum(w[:, 1], 1 - l1)
    l3 = np.minimum(w[:, 2], 1 - l1 - l2)
    return (1 - l1 - l2 - l3, l1, l2, l3)[layer]


def light_direction(ctx):
    """The map's sunlight: (pitch, yaw) in degrees."""
    from dota2tools import maprender
    sun = maprender.sun_override(ctx.tools)
    if sun and maprender.map_source(ctx.tools, ctx.addon, ctx.map):
        return sun[:2]
    for e in mapinfo.entities(ctx.tools, ctx.addon, ctx.map):
        if e.get("classname") == "env_global_light" and "angles" in e:
            a = mapinfo._vec(e["angles"])
            return a[0], a[1]
    return 66.0, 0.0


def solid_meshes(ctx):
    """World geometry that blocks light (no alpha-tested / translucent /
    tools materials)."""
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    see_through = {}

    def clear(rel):
        if rel not in see_through:
            names = set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", models.get(rel, b"")))
            mats = [mapinfo.material(ctx.tools, ctx.addon, ctx.map, n.decode()) for n in names]
            see_through[rel] = any(n.startswith(b"materials/tools/") or
                                   (n.startswith(b"materials/dev/") and n != b"materials/dev/black.vmat")
                                   for n in names) or any(
                mt and (mt["ints"].get("F_ALPHA_TEST") or mt["ints"].get("F_TRANSLUCENT")) for mt in mats)
        return see_through[rel]
    return [w for w in mapinfo.world_meshes(ctx.tools, ctx.addon, ctx.map) if not clear(w[0])]


def _disk(radius_px):
    import cv2
    r = max(1, int(round(radius_px)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def grow_coverage(cov, cell, bridge, extend, allowed=None):
    """Close gaps between lava arms, then grow past the silhouette.

    With `allowed`, growth stays on those cells and on components connected
    to the original lava: a close/dilate cannot jump across a stone bank.
    """
    import cv2
    import numpy as np
    out = cov.astype("uint8")
    seeds = out > 0

    def reachable(a):
        a = (a > 0) & (allowed > 0)
        _n, labels = cv2.connectedComponents(a.astype("uint8"))
        ids = np.unique(labels[seeds])
        return np.isin(labels, ids[ids > 0]).astype("uint8")

    if allowed is not None:
        out = reachable(out)
    if bridge > 0:
        out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, _disk(bridge / cell / 2.0))
        if allowed is not None:
            out = reachable(out)
    if extend > 0:
        out = cv2.dilate(out, _disk(extend / cell))
        if allowed is not None:
            out = reachable(out)
    return out


def ground_height(ctx, extent, shape):
    """Terrain and black-corner height, excluding stone props and plants.
    New lava clears this base but can remain below the props that occlude it."""
    import re
    from dota2tools import lightbake
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    ground = []
    for rel, P, tris in mapinfo.world_meshes(ctx.tools, ctx.addon, ctx.map):
        names = set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", models.get(rel, b"")))
        if any(n == b"materials/dev/black.vmat" or b"/blends/" in n for n in names):
            ground.append((rel, P, tris))
    if not ground:
        return None
    Z, _ = lightbake.height_field(ground, extent, (extent[1] - extent[0]) / shape[1])
    return Z


def mesh_from_coverage(cov, Z, x0, x1, y0, y1):
    """A grid mesh over cells where cov is set. Z is the height field (filled).
    Returns (positions, normals, triangles)."""
    import numpy as np
    H, W = cov.shape
    dx, dy = (x1 - x0) / W, (y1 - y0) / H
    # Corner heights: mean of the up-to-4 cells that touch the corner.
    Zh = np.where(np.isfinite(Z), Z, 0.0).astype(np.float32)
    weigh = (cov > 0).astype(np.float32)
    # pad so corners are (H+1, W+1)
    acc = np.zeros((H + 1, W + 1), np.float32)
    wt = np.zeros((H + 1, W + 1), np.float32)
    for di, dj in ((0, 0), (0, 1), (1, 0), (1, 1)):
        acc[di:di + H, dj:dj + W] += Zh * weigh
        wt[di:di + H, dj:dj + W] += weigh
    Zc = np.divide(acc, np.maximum(wt, 1e-6))

    # Only emit verts used by covered cells.
    used = np.zeros((H + 1, W + 1), bool)
    ys, xs = np.where(cov > 0)
    for i, j in zip(ys, xs):
        used[i:i + 2, j:j + 2] = True
    index = -np.ones((H + 1, W + 1), np.int64)
    P, N = [], []
    for i, j in zip(*np.where(used)):
        index[i, j] = len(P)
        # cov row 0 is y=y1 (top of atlas); row H-1 is y=y0.
        x = x0 + j * dx
        y = y1 - i * dy
        P.append((float(x), float(y), float(Zc[i, j])))
        N.append((0.0, 0.0, 1.0))
    tris = []
    for i, j in zip(ys, xs):
        a, b = int(index[i, j]), int(index[i, j + 1])
        c, d = int(index[i + 1, j]), int(index[i + 1, j + 1])
        if min(a, b, c, d) < 0:
            continue
        tris.append((a, d, b))
        tris.append((a, c, d))
    return P, N, tris


def atlas_tiles(x0, x1, y0, y1, tex_res, max_side, overlap=128.0):
    """Split a world extent into balanced overlapping tiles that each fit in
    a max_side atlas at tex_res. Neighbouring tiles overlap by `overlap` units
    so bridge/feather meet cleanly."""
    max_span = max_side * tex_res

    def axis(a, b):
        if b - a <= max_span + 1e-6:
            return [(a, b)]
        step = max_span - overlap
        n = max(2, math.ceil((b - a - overlap) / step))
        span = (b - a + (n - 1) * overlap) / n
        return [(a + i * (span - overlap),
                 a + i * (span - overlap) + span) for i in range(n - 1)] + \
               [(a + (n - 1) * (span - overlap), b)]

    return [(xa, xb, ya, yb) for xa, xb in axis(x0, x1) for ya, yb in axis(y0, y1)]


def tile_ownership(extent, tiles):
    """The non-overlapping part of an atlas tile: halfway through each gutter.
    The gutter is wider than the alpha feather, so adjoining edges are opaque."""
    x0, x1, y0, y1 = extent
    out = [x0, x1, y0, y1]
    for other in tiles:
        if other == extent:
            continue
        ox0, ox1, oy0, oy1 = other
        if oy1 > y0 and oy0 < y1:
            if ox0 < x0:
                out[0] = max(out[0], (x0 + ox1) / 2)
            if ox1 > x1:
                out[1] = min(out[1], (ox0 + x1) / 2)
        if ox1 > x0 and ox0 < x1:
            if oy0 < y0:
                out[2] = max(out[2], (y0 + oy1) / 2)
            if oy1 > y1:
                out[3] = min(out[3], (oy0 + y1) / 2)
    return tuple(out)


def clip_mesh(P, N, tris, extent):
    """Clip triangles to an XY rectangle, interpolating positions and normals.
    Unlike dropping whole grid cells, this gives neighbouring tiles identical
    boundary vertices: no overlap, z-fighting, or uncovered line."""
    import numpy as np
    P = np.asarray(P, np.float32)
    N = np.asarray(N, np.float32)
    x0, x1, y0, y1 = extent
    planes = ((0, 1.0, -x0), (0, -1.0, x1), (1, 1.0, -y0), (1, -1.0, y1))
    out_p, out_n, out_t = [], [], []
    for tri in tris:
        poly = [(P[i], N[i]) for i in tri]
        for axis, sign, bound in planes:
            clipped = []
            for a, b in zip(poly, poly[1:] + poly[:1]):
                da = sign * a[0][axis] + bound
                db = sign * b[0][axis] + bound
                if da >= -1e-5:
                    clipped.append(a)
                if (da >= -1e-5) != (db >= -1e-5):
                    t = float(np.clip(da / (da - db), 0, 1))
                    clipped.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            poly = clipped
            if len(poly) < 3:
                break
        if len(poly) < 3:
            continue
        base = len(out_p)
        out_p.extend(p[0] for p in poly)
        out_n.extend(p[1] for p in poly)
        out_t.extend((base, base + i + 1, base + i + 2) for i in range(len(poly) - 2))
    return out_p, out_n, out_t


def clip_tris(P, N, tris, x0, x1, y0, y1, pad=0.0):
    """Triangles whose centroid lies in the padded tile rectangle, still
    indexed into the full P/N arrays."""
    keep = []
    for t in tris:
        cx = (P[t[0]][0] + P[t[1]][0] + P[t[2]][0]) / 3
        cy = (P[t[0]][1] + P[t[1]][1] + P[t[2]][1]) / 3
        if x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad:
            keep.append(t)
    return keep


def light_field(ctx, stem, P, tris, extent):
    """One continuous baked-light field over a whole spatial lava group.
    Atlas tiles sample this field; none renormalizes ambient light locally."""
    import cv2
    import numpy as np
    from dota2tools import lightbake
    o = ctx.options
    res = float(o.get("bake_res", 8.0))
    margin = 600.0
    x0, x1, y0, y1 = extent
    region = (x0 - margin, x1 + margin, y0 - margin, y1 + margin)
    ctx.log(f"lava: reading the world's geometry around {stem}")
    world = solid_meshes(ctx)
    Hgt, _ = lightbake.height_field(world, region, res)
    Hgt = lightbake._fill(Hgt)
    k = np.ones((9, 9), np.uint8)
    Hgt = cv2.dilate(cv2.erode(Hgt, k), k)
    Z, _ = lightbake.height_field([(stem, np.asarray(P, np.float32), np.asarray(tris, np.int64))], region, res)
    Z0 = Z
    Z = np.where(Z < -1e8, Hgt, Z)
    pitch, yaw = light_direction(ctx)
    vis, ao = lightbake.shading(Hgt, Z, res, pitch, yaw, ao_radius=float(o.get("ao_radius", 900.0)))
    sun = float(o.get("sun", 0.5))
    lava_cells = Z0 > -1e8
    top = np.percentile(ao[lava_cells], 90) if lava_cells.any() else 1.0
    ao = np.clip(ao / max(top, 1e-3), 0, 1)
    S = ao ** float(o.get("occlusion", 2.5)) * ((1 - sun) + sun * vis)
    edge_units = float(o.get("edge", 160.0))
    if edge_units > 0:
        above = ((Hgt - Z) > 24.0).astype(np.uint8)
        dist = cv2.distanceTransform(1 - above, cv2.DIST_L2, 5) * res
        e = np.clip(dist / edge_units, 0, 1)
        e = e * e * (3 - 2 * e)
        lo = float(o.get("edge_floor", 0.08))
        S = S * (lo + (1 - lo) * e)
    soft = float(o.get("soften", 40.0)) / res
    if soft > 0:
        S = cv2.GaussianBlur(S.astype(np.float32), (0, 0), soft)
    m0, m1 = int(margin / res), int(margin / res)
    S = S[m1:S.shape[0] - m1, m0:S.shape[1] - m0]
    return S, world, pitch, yaw


def coverage_field(ctx, stem, layer, P, tris, streams, ptris, world, extent):
    """Coarse lava coverage and height over a whole spatial group.

    Atlas tiles sample this field. Their crop edges therefore stay interior
    lava, rather than turning into false outer banks and alpha seams.
    """
    import cv2
    import numpy as np
    from dota2tools import lightbake
    o = ctx.options
    x0, x1, y0, y1 = extent
    mesh_res = float(o.get("mesh_res", 12.0))
    mw = max(2, int(math.ceil((x1 - x0) / mesh_res)))
    mh = max(2, int(math.ceil((y1 - y0) / mesh_res)))
    Z_l, _ = lightbake.height_field([(stem, np.asarray(P, np.float32), np.asarray(tris, np.int64))],
                                    extent, mesh_res)
    Z_w, _ = lightbake.height_field(world, extent, mesh_res)
    Z_w = lightbake._fill(Z_w)
    if Z_l.shape != (mh, mw):
        has = (Z_l > -1e8).astype(np.uint8)
        Z_fill = np.where(Z_l > -1e8, Z_l, 0).astype(np.float32)
        has = cv2.resize(has, (mw, mh), interpolation=cv2.INTER_NEAREST)
        Z_fill = cv2.resize(Z_fill, (mw, mh), interpolation=cv2.INTER_LINEAR)
        Z_l = np.where(has > 0, Z_fill, -1e9)
    has = Z_l > -1e8
    weights = layer_paint(streams, layer)
    if weights is not None:
        painted, pcov = lightbake.rasterize(streams["position$0"], ptris, weights[:, None], extent, mesh_res)
        if painted.shape[:2] != (mh, mw):
            painted = cv2.resize(painted, (mw, mh), interpolation=cv2.INTER_LINEAR)[..., None]
            pcov = cv2.resize(pcov.astype("uint8"), (mw, mh), interpolation=cv2.INTER_NEAREST) > 0
        has &= pcov & (painted[..., 0] >= float(o.get("paint_min", 0.1)))
    if has.any():
        _dist, labels = cv2.distanceTransformWithLabels(
            (~has).astype("uint8"), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        heights = np.zeros(int(labels.max()) + 1, np.float32)
        heights[1:] = Z_l[has]
        nearest = heights[labels]
        allowed = ((Z_w - nearest) <= float(o.get("max_rise", 128.0))).astype("uint8")
        cov_m = grow_coverage((has & (allowed > 0)).astype("uint8"), mesh_res,
                              float(o.get("bridge", 512.0)), float(o.get("extend", 128.0)), allowed=allowed)
        cov_m |= (has & (allowed == 0)).astype("uint8")
    else:
        nearest = np.full((mh, mw), 256.0, np.float32)
        allowed = np.zeros((mh, mw), np.uint8)
        cov_m = np.zeros((mh, mw), np.uint8)
    join = dlc.dire_connection(ctx, extent, (mh, mw))
    if join is not None:
        cov_m |= join
        xx = x0 + (np.arange(mw) + 0.5) * (x1 - x0) / mw
        yy = y1 - (np.arange(mh) + 0.5) * (y1 - y0) / mh
        grass = (xx >= 4900) & (xx <= 7700)
        elbow = (xx > 7700) & (xx <= 7820)
        cov_m[(yy > 7800)[:, None] & grass[None, :]] = 0
        cov_m[(yy > 7900)[:, None] & elbow[None, :]] = 0
        cov_m[(yy > 7900)[:, None] & (xx < 4900)[None, :]] = 0
        cov_m[(yy > 6800)[:, None] & (xx > 8680)[None, :]] = 0
    rock_m = dlc.dire_connection(ctx, extent, (mh, mw), rock=True)
    ground_m = dlc.dire_connection(ctx, extent, (mh, mw), ground=True)
    Z_m = np.where(has, np.maximum(Z_l, Z_w), Z_w)
    Z_m[has & (allowed == 0)] = Z_l[has & (allowed == 0)]
    if join is not None:
        new_join = (join > 0) & ~has
        low = np.minimum(Z_w, nearest)
        Z_g = ground_height(ctx, extent, (mh, mw))
        if Z_g is None:
            Z_g = np.full(Z_m.shape, -1e9, np.float32)
        elif Z_g.shape != (mh, mw):
            Z_g = cv2.resize(Z_g, (mw, mh), interpolation=cv2.INTER_NEAREST)
        low = np.maximum(low, np.where(Z_g > -1e8, Z_g, -1e9))
        Z_m[new_join] = low[new_join]
        # Low black-corner ground must not hide the new outer arc. High inner
        # terrain still caps growth below its cliffs and keeps the source bed.
        high = Z_g > dlc.DIRE_GROUND_Z + 64
        Z_m[new_join & high] = np.minimum(Z_m[new_join & high], dlc.DIRE_GROUND_Z)
        original = (join > 0) & (Z_l > -1e8)
        low_ground = original & ~high & (Z_g > -1e8)
        Z_m[low_ground] = np.maximum(Z_l[low_ground], Z_g[low_ground])
        Z_m[original & high] = Z_l[original & high]
    # Where the lava meets what stands over it (stones, ledges, raised
    # grass): grown lava stays at the lava's own height and reaches a little
    # under them, so its edge is hidden beneath them instead of wrapping
    # round them at their height.
    under = float(o.get("under_props", 48.0))
    if under > 0 and cov_m.any():
        high = (Z_w - nearest) > float(o.get("prop_rise", 24.0))
        band = (cv2.dilate(cov_m, _disk(under / mesh_res)) > 0) & high & (cov_m == 0)
        zone_m = dlc.dire_rim(ctx, extent, (mh, mw))
        if zone_m is not None:
            band &= ~zone_m
        keep = band | ((cov_m > 0) & ~has & high)
        if join is not None:
            keep &= join == 0
        Z_m[keep] = nearest[keep]
        cov_m = (cov_m | band).astype(np.uint8)
    cut = dlc.dire_cut(ctx, extent, (mh, mw))
    if cut is not None:
        cov_m[cut] = 0
    combined = cov_m.copy()
    if rock_m is not None:
        banks = (rock_m | ground_m) & (Z_w <= 512)
        combined |= banks
        Z_m[(rock_m | ground_m) > 0] = dlc.DIRE_GROUND_Z
    zone = dlc.dire_rim(ctx, extent, (mh, mw))
    if zone is not None and cov_m.any():
        # The rim's geometry: on the ground (terrain and corner ground, not
        # the stones, which stay over it).
        rim_w = float(o.get("rim", 160.0))
        ring = (cv2.dilate(cov_m, _disk(rim_w / mesh_res + 2)) > 0) & zone & (combined == 0)
        Z_g = ground_height(ctx, extent, (mh, mw))
        if Z_g is not None and Z_g.shape != (mh, mw):
            Z_g = cv2.resize(Z_g, (mw, mh), interpolation=cv2.INTER_NEAREST)
        base = Z_w if Z_g is None else np.where(Z_g > -1e8, Z_g, Z_w)
        Z_m[ring] = base[ring]
        combined[ring] = 1
    return cov_m, Z_m, combined


def bake(ctx, stem, m, layer, tint, P, tris, repeat, data=None, extent=None,
         light_cache=None, light_extent=None):
    """Bake lava into an atlas over `extent` (or the mesh AABB grown by
    extend). Coverage is morphologically closed and dilated so gaps between
    arms fill; a mesh_res grid over that coverage is the overlay geometry.
    Returns (extent, png path, overlay_P, overlay_N, overlay_tris)."""
    import cv2
    import numpy as np
    from PIL import Image
    from dota2tools import lightbake
    o = ctx.options
    res = float(o.get("bake_res", 8.0))
    tex_res = float(o.get("texture_res", 0.75))
    bridge = float(o.get("bridge", 512.0))
    extend = float(o.get("extend", 128.0))
    # resourcecompiler's LoadFromPNG fails above 8192 on a side.
    max_side = int(o.get("max_side", 4096))
    V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
    grow = extend
    if extent is not None:
        x0, x1, y0, y1 = extent
    else:
        x0 = float(V[..., 0].min()) - grow
        x1 = float(V[..., 0].max()) + grow
        y0 = float(V[..., 1].min()) - grow
        y1 = float(V[..., 1].max()) + grow
    if light_cache is not None:
        if not light_cache:
            S, world, pitch, yaw = light_field(ctx, stem, P, tris, light_extent)
            light_cache.update(light=S, world=world, pitch=pitch, yaw=yaw)
        world, pitch, yaw = light_cache["world"], light_cache["pitch"], light_cache["yaw"]
        if tuple(extent) == tuple(light_extent):
            S = light_cache["light"]
        else:
            fx0, _fx1, _fy0, fy1 = light_extent
            xs = x0 + (np.arange(int(math.ceil((x1 - x0) / res))) + 0.5) * res
            ys = y1 - (np.arange(int(math.ceil((y1 - y0) / res))) + 0.5) * res
            uu, vv = np.meshgrid((xs - fx0) / res - 0.5, (fy1 - ys) / res - 0.5)
            S = cv2.remap(light_cache["light"], uu.astype(np.float32), vv.astype(np.float32),
                          cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    else:
        S, world, pitch, yaw = light_field(ctx, stem, P, tris, (x0, x1, y0, y1))

    def side(span):
        return min(max_side, 2 ** max(4, math.ceil(math.log2(max(span / tex_res, 1)))))

    W, Hh = side(x1 - x0), side(y1 - y0)
    S = cv2.resize(S, (W, Hh), interpolation=cv2.INTER_LINEAR)
    cell = max((x1 - x0) / W, (y1 - y0) / Hh)
    join_light = dlc.dire_connection(ctx, (x0, x1, y0, y1), (Hh, W))
    if join_light is not None:
        soft = np.clip(cv2.distanceTransform(join_light, cv2.DIST_L2, 5) * cell
                       / float(o.get("join_soft", 256.0)), 0, 1)
        soft = soft * soft * (3 - 2 * soft)
        # The join's own light: the baked field (which darkens toward the
        # stones and under their shadow) lifted to join_light where the
        # field has nothing to say (no source lava), never flattened to it.
        lift = np.maximum(S, float(o.get("join_light", 0.35)) * np.clip(S / 0.35, 0.3, 1.0))
        S = S + (lift - S) * soft
        S = cv2.GaussianBlur(S, (0, 0), 64.0 / cell) * soft + S * (1 - soft)

    texa = np.asarray(mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tColor{layer}"])
                      .convert("RGBA"), np.float32)
    tex, height = texa[..., :3], texa[..., 3] / 255
    spec = mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tSpecular{layer}"])
    mask = np.asarray(spec.convert("RGBA").split()[1].resize((tex.shape[1], tex.shape[0])), np.float32) / 255
    th, tw = tex.shape[:2]
    xs = x0 + (np.arange(W) + 0.5) * (x1 - x0) / W
    ys = y1 - (np.arange(Hh) + 0.5) * (y1 - y0) / Hh
    ui = (np.mod(xs / repeat, 1.0) * tw).astype(np.float32)
    vi = ((1 - np.mod(ys / repeat, 1.0)) * th).astype(np.float32)
    uu, vv = np.meshgrid(ui, vi)

    def sample(a):
        return cv2.remap(a, uu, vv, cv2.INTER_CUBIC, borderMode=cv2.BORDER_WRAP)

    col = sample(tex)
    glow = sample(mask)
    g = float(o.get("glow", 0.6))
    lava = col * np.asarray(tint, np.float32) * (1 + g * glow[:, :, None])
    streams, ptris = mapinfo.decompile_streams(data) if data is not None else ({}, [])
    paint = None
    if o.get("paint", True) and "texcoord$3" in streams and "position$0" in streams:
        grid, covered = lightbake.rasterize(streams["position$0"], ptris, streams["texcoord$3"][:, :3],
                                            (x0, x1, y0, y1), 6.0)
        kpaint = grid.mean(2)
        if covered.any():
            _dist, nearest = cv2.distanceTransformWithLabels(
                (~covered).astype("uint8"), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
            values = np.zeros((int(nearest.max()) + 1, 3), np.float32)
            values[1:] = grid[covered]
            kpaint = np.where(covered, kpaint, values[nearest].mean(2))
        paint = cv2.resize(kpaint, (W, Hh), interpolation=cv2.INTER_LINEAR)[:, :, None]
        source_cov = cv2.resize(covered.astype(np.uint8), (W, Hh), interpolation=cv2.INTER_NEAREST) > 0
    else:
        source_cov = None
    if paint is not None:
        a = sample(height)
        thr = float(o.get("crust_base", 0.3)) + float(o.get("crust_span", 0.6)) * paint[:, :, 0]
        c = np.clip((thr - a) / float(o.get("crust_soft", 0.18)) + 0.5, 0, 1)
        c = (c * c * (3 - 2 * c))[:, :, None]
        dim = float(o.get("paint_dim", 0.65))
        out = (lava * c * ((1 - dim) + dim * paint) + lava * float(o.get("crust", 0.18)) * (1 - c)) * S[:, :, None]
    else:
        out = lava * S[:, :, None]

    # One coarse field for the entire channel, shared by all its atlas tiles.
    # The separate lava mask excludes forced ground from outer-bank detection.
    mesh_res = float(o.get("mesh_res", 12.0))
    mw = max(2, int(math.ceil((x1 - x0) / mesh_res)))
    mh = max(2, int(math.ceil((y1 - y0) / mesh_res)))
    field_extent = light_extent or (x0, x1, y0, y1)
    if light_cache is not None:
        if "coverage" not in light_cache:
            light_cache["coverage"] = coverage_field(
                ctx, stem, layer, P, tris, streams, ptris, world, field_extent)
        lava_full, Z_full, combined_full = light_cache["coverage"]
    else:
        lava_full, Z_full, combined_full = coverage_field(
            ctx, stem, layer, P, tris, streams, ptris, world, field_extent)
    fx0, fx1, fy0, fy1 = field_extent
    xx = x0 + (np.arange(mw) + 0.5) * (x1 - x0) / mw
    yy = y1 - (np.arange(mh) + 0.5) * (y1 - y0) / mh
    uu, vv = np.meshgrid((xx - fx0) / (fx1 - fx0) * lava_full.shape[1] - 0.5,
                         (fy1 - yy) / (fy1 - fy0) * lava_full.shape[0] - 0.5)
    uu, vv = uu.astype(np.float32), vv.astype(np.float32)
    cov_m = cv2.remap(lava_full, uu, vv, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    combined_m = cv2.remap(combined_full, uu, vv, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    Z_m = cv2.remap(Z_full, uu, vv, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    rock_m = dlc.dire_connection(ctx, (x0, x1, y0, y1), (mh, mw), rock=True)
    ground_m = dlc.dire_connection(ctx, (x0, x1, y0, y1), (mh, mw), ground=True)
    if rock_m is not None:
        rock_only = cv2.resize(rock_m, (W, Hh), interpolation=cv2.INTER_NEAREST)
        ground_only = cv2.resize(ground_m, (W, Hh), interpolation=cv2.INTER_NEAREST)
    else:
        rock_only = np.zeros((Hh, W), np.uint8)
        ground_only = np.zeros((Hh, W), np.uint8)
    cov = cv2.resize(combined_m, (W, Hh), interpolation=cv2.INTER_NEAREST)
    lava_cov = cv2.resize(cov_m, (W, Hh), interpolation=cv2.INTER_NEAREST)
    cut_hi = dlc.dire_cut(ctx, (x0, x1, y0, y1), (Hh, W))
    if cut_hi is not None:
        # The coarse field's cut is stepped; at the atlas's resolution it
        # follows the bank's line.
        lava_cov = np.where(cut_hi, 0, lava_cov).astype(np.uint8)

    # The join's new lava has no vertex paint of its own, and the map's lava
    # meets its outer edge dark: where it was made, and along the outer
    # edge, it darkens from its middle to its outline as the map's own
    # channels do, smoothly into the paint-lit lava around it.
    if dlc.enabled(ctx) and lava_cov.any():
        fall = float(o.get("fall", 160.0))
        lo = float(o.get("fall_floor", 0.12))

        def ramp(dist, width):
            v = np.clip(dist / width, 0, 1)
            return lo + (1 - lo) * v * v * (3 - 2 * v)
        # Toward the outer edge (the arc's cut and the northern shelf): all
        # lava there, the map's own included, darkens to its outline.
        outer_edge = (rock_only > 0) | (ground_only > 0)
        cut_a = dlc.dire_cut(ctx, (x0, x1, y0, y1), (Hh, W))
        if cut_a is not None:
            outer_edge |= cut_a
        if outer_edge.any():
            d = cv2.distanceTransform((~outer_edge).astype(np.uint8), cv2.DIST_L2, 5) * cell
            out = out * ramp(d, fall)[:, :, None]
        # Toward whatever stands over it (stones, the base's tile steps) the
        # lava darkens as the map's does under its banks: the lava the join
        # made has no vertex paint for it, and grown lava reaches under
        # those steps' square edges, which then cut it bright.
        # The lava the join made has no vertex paint: toward the stones in
        # it, it darkens as the map's lava does under its banks.
        if source_cov is not None:
            made = (~source_cov) & (lava_cov > 0)
            if made.any():
                Zw = cv2.resize(Z_m, (W, Hh), interpolation=cv2.INTER_LINEAR)
                lava_z = np.where(made, Zw, np.nan)
                base = np.nanmedian(lava_z)
                stone = dlc._stones(ctx, (x0, x1, y0, y1), (Hh, W), base + float(o.get("stone_rise", 48.0)))
                if stone is not None and stone.any():
                    ds = cv2.distanceTransform((~stone).astype(np.uint8), cv2.DIST_L2, 5) * cell
                    w = cv2.GaussianBlur(made.astype(np.float32), (0, 0), 64.0 / cell)
                    g = ramp(ds, float(o.get("stone_fall", 120.0)))
                    out = out * (1 - w * (1 - g))[:, :, None]

    # External silhouettes darken into crust, not grey rock. Internal holes
    # keep their baked colour. Rock is reserved for the northern grass strip.
    bank_data = dlc.corner_bank(ctx, (x0, x1, y0, y1), (W, Hh)) \
        if cov.any() and (o.get("bank_texture", False) or rock_only.any() or ground_only.any()) else None
    feather = float(o.get("feather", 32.0))
    bank = float(o.get("bank", 64.0))
    # Only the north and north-east outer banks fade broadly. The eastern
    # channel and internal edges get only local antialiasing below.
    soft = np.clip((ys - 6800.0) / 400.0, 0, 1)
    soft = (soft * soft * (3 - 2 * soft))[:, None]
    feather_field = feather * soft
    if bank > 0 and lava_cov.any():
        outer = np.zeros_like(lava_cov)
        contours, _ = cv2.findContours(lava_cov, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(outer, contours, -1, 1, cv2.FILLED)
        dist_in = cv2.distanceTransform(outer, cv2.DIST_L2, 5) * cell
        # Keep full darkening through the alpha feather; transition to lava
        # only after the bank is fully visible, not while still translucent.
        bank_t = np.clip((dist_in - feather_field) / bank, 0, 1)
        bank_t = bank_t * bank_t * (3 - 2 * bank_t)
        weight = (1 - bank_t) * soft
        zone_b = dlc.dire_rim(ctx, (x0, x1, y0, y1), (Hh, W))
        if zone_b is not None:
            # The rim replaces the bank on the lava's own edge; the northern
            # grass's forced rock and ground keep it.
            weight = np.where(zone_b & (rock_only == 0) & (ground_only == 0), 0.0, weight)
        lo = float(o.get("bank_floor", 0.35))
        out = out * (1 - (1 - lo) * weight)[:, :, None]
        if bank_data is not None and o.get("bank_texture", False):
            rock, inside = bank_data
            mix = weight[:, :, None] * inside[:, :, None]
            out = out * (1 - mix) + rock * mix
    if rock_only.any() or ground_only.any():
        rock_data = bank_data or dlc.corner_bank(ctx, (x0, x1, y0, y1), (W, Hh))
        ground_data = dlc.corner_bank(ctx, (x0, x1, y0, y1), (W, Hh), ground=True)
        if rock_data is not None and ground_data is not None:
            rock, rock_inside = rock_data
            ground, ground_inside = ground_data
            south = float(ys[(rock_only > 0).any(axis=1)].min()) if rock_only.any() else 7580.0
            boundary = south + 60.0
            t = np.clip((ys - boundary + 32.0) / 64.0, 0, 1)
            t = t * t * (3 - 2 * t)
            strip = rock * (1 - t[:, None, None]) + ground * t[:, None, None]
            forced = np.clip(rock_only + ground_only, 0, 1).astype(np.float32)
            mix = cv2.GaussianBlur(forced, (0, 0), 32.0 / cell) * rock_inside * ground_inside
            # The shelf's lava side: black crust into its rock, as the rim.
            d_l = cv2.distanceTransform((lava_cov == 0).astype(np.uint8), cv2.DIST_L2, 5) * cell
            # Then its rock at the rim's shade, not brighter than the ground
            # it meets: black, dark rock, ground, one ramp as at the rim.
            crust_w = float(o.get("rim_crust", 36.0))
            k = np.clip(d_l / (crust_w * 2.5), 0, 1)
            k = k * k * (3 - 2 * k)
            shade = 0.06 + (float(o.get("rim_shade", 0.8)) - 0.06) * k
            # Into the ground over the shelf's own height (the rock band is
            # narrower than the rim).
            to_ground = np.clip((d_l - crust_w * 2.5) / float(o.get("shelf_fade", 72.0)), 0, 1)
            to_ground = to_ground * to_ground * (3 - 2 * to_ground)
            shade = shade + (1 - shade) * to_ground
            dark_rock = rock * (1 - to_ground[:, :, None]) + ground * to_ground[:, :, None]
            strip = dark_rock * shade[:, :, None]
            out = out * (1 - mix[:, :, None]) + strip * mix[:, :, None]
            # The seam where the lava meets the shelf: the shelf's crust starts
            # as dark as the lava ends (no step between the two).
            seam_w = float(o.get("shelf_seam", 24.0))
            if seam_w > 0:
                shelf_m = (rock_only > 0) | (ground_only > 0)
                inside = cv2.distanceTransform(shelf_m.astype(np.uint8), cv2.DIST_L2, 5) * cell
                near = np.clip(1 - inside / seam_w, 0, 1) * shelf_m
                near = (near * near * (3 - 2 * near))[:, :, None]
                out = out * (1 - near * 0.85)

    # The rim: past the lava's visible outer edge, black crust, then the
    # corner's dark rock, darker still, fading into the ground.
    zone = dlc.dire_rim(ctx, (x0, x1, y0, y1), (Hh, W))
    rim_alpha = None
    shelf_blend = None
    if zone is not None and lava_cov.any():
        rim_w = float(o.get("rim", 160.0))
        crust_w = float(o.get("rim_crust", 36.0))
        # The rim from the lava's own edge, at the atlas's resolution: the
        # mesh's 6-unit cells only bound it.
        ring = zone & (lava_cov == 0) & (rock_only == 0) & (ground_only == 0)
        if ring.any():
            d = cv2.distanceTransform((lava_cov == 0).astype(np.uint8), cv2.DIST_L2, 5) * cell
            ring &= d < rim_w
            rim_rock = bank_data or dlc.corner_bank(ctx, (x0, x1, y0, y1), (W, Hh))
            rock = rim_rock[0] if rim_rock is not None else np.full((Hh, W, 3), 60.0, np.float32)
            k = np.clip(d / (crust_w * 2.5), 0, 1)
            k = k * k * (3 - 2 * k)
            shade = 0.06 + (float(o.get("rim_shade", 0.8)) - 0.06) * k
            colour = rock * shade[:, :, None]
            out = np.where(ring[:, :, None], colour, out)
            a = np.clip((rim_w - d) / (rim_w * 0.55), 0, 1)
            rim_alpha = np.where(ring, a * a * (3 - 2 * a), -1.0)
            # Past the rim (mesh cells the lava's own edge doesn't reach):
            # nothing drawn.
            beyond = zone & (lava_cov == 0) & (rock_only == 0) & (ground_only == 0) & ~ring
            rim_alpha = np.where(beyond, 0.0, rim_alpha)
            # The shelf's east end turns into the rim over its last units:
            # colour and alpha blended by x, so the two meet without a step.
            shelf = zone & (lava_cov == 0) & ((rock_only > 0) | (ground_only > 0))
            if shelf.any():
                sx = np.clip((xs - 7300.0) / 400.0, 0, 1)
                sx = (sx * sx * (3 - 2 * sx))[None, :] * shelf
                rim_a = np.where(d < rim_w, a * a * (3 - 2 * a), 0.0)
                out = out * (1 - sx[:, :, None]) + colour * sx[:, :, None]
                shelf_blend = (sx, rim_a)
            else:
                shelf_blend = None

    edge_feather = float(o.get("edge_feather", 6.0))
    if max(feather, edge_feather) > 0 and cov.any():
        # The coverage is a 6-unit grid: its outline steps. Smoothed first
        # (a blur, thresholded at half) it follows a curve, then feathered.
        soft_edge = float(o.get("edge_soften", 18.0))
        cov_s = cov
        if soft_edge > 0:
            cov_s = (cv2.GaussianBlur(cov.astype(np.float32), (0, 0), soft_edge / cell) > 0.5).astype(np.uint8) | \
                ((rock_only > 0) | (ground_only > 0)).astype(np.uint8)
        dist_in = cv2.distanceTransform(cov_s, cv2.DIST_L2, 5) * cell
        width = np.maximum(feather_field, max(edge_feather, float(o.get("edge_soft_width", 14.0))))
        alpha = np.clip(dist_in / np.maximum(width, 1e-6), 0, 1)
        alpha = alpha * alpha * (3 - 2 * alpha)
        if ground_only.any():
            # Its east end fades out over the arc's start, where the rim
            # takes over (no step where the two meet).
            edge = np.clip((xs - 4900.0) / 128.0, 0, 1)
            edge = edge * edge * (3 - 2 * edge)
            alpha = np.where((ground_only > 0) | (rock_only > 0), np.minimum(alpha, edge[None, :]), alpha)
            # And out at its top, into the ground it stands for, over
            # shelf_top units (no line where the overlay ends).
            top = float(ys[(ground_only > 0).any(axis=1)].max()) if ground_only.any() else 8120.0
            fade_top = np.clip((top - ys) / float(o.get("shelf_top", 120.0)), 0, 1)
            fade_top = fade_top * fade_top * (3 - 2 * fade_top)
            alpha = np.where(ground_only > 0, alpha * fade_top[:, None], alpha)
    else:
        alpha = cov.astype(np.float32)
    if rim_alpha is not None:
        alpha = np.where(rim_alpha >= 0, rim_alpha, alpha)
        if shelf_blend is not None:
            sx, rim_a = shelf_blend
            alpha = alpha * (1 - sx) + rim_a * sx
        # The lava under the rim's edge: no feather of its own.
        near = zone & (lava_cov > 0)
        alpha = np.where(near, np.maximum(alpha, (cov > 0).astype(np.float32)), alpha)
    south = south_blend(ctx)
    if south is not None:
        a = np.clip((ys - south[0]) / (south[1] - south[0]), 0, 1)
        alpha *= (a * a * (3 - 2 * a))[:, None]
    rgba = np.concatenate([np.clip(out + 0.5, 0, 255), (alpha * 255)[:, :, None]], axis=2)
    rel = f"materials/dota2tools/textures/{stem}_baked.png"
    p = ctx.content / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba.astype(np.uint8), "RGBA").save(p)
    alpha_rel = f"materials/dota2tools/textures/{stem}_translucency.png"
    Image.fromarray(rgba[:, :, 3].astype(np.uint8), "L").save(ctx.content / alpha_rel)
    ctx.log(f"lava: {stem}'s light baked, {W} x {Hh} px ({cell:.2f} u/px), "
            f"bridge {bridge:.0f} extend {extend:.0f}, light at {pitch:.0f}/{yaw:.0f}")
    oP, oN, oTris = mesh_from_coverage(combined_m, Z_m, x0, x1, y0, y1)
    ctx.log(f"lava: {stem} overlay mesh {len(oTris)} tris at {mesh_res:g} u "
            f"({mw}x{mh} cells, was {len(tris)} map tris)")
    return (float(x0), float(x1), float(y0), float(y1)), rel, oP, oN, oTris


def coverage_query(overlays, inset=-24.0, res=6.0, rise=24.0):
    """Point-in-lava query for final, clipped overlay meshes. Positive inset
    ignores the transparent feather; negative inset also catches grass roots
    just outside it, whose cards would otherwise protrude into the lava."""
    import cv2
    import numpy as np
    from dota2tools import lightbake
    grids = []
    for extent, P, tris in overlays:
        P = np.asarray(P, np.float32)
        if not len(P) or not len(tris):
            continue
        T = np.asarray(tris, np.int64)
        heights, covered = lightbake.rasterize(P, T, P[:, 2:3], extent, res)
        heights = heights[..., 0]
        if inset > 0:
            covered = cv2.erode(covered.astype(np.uint8), _disk(inset / res)) > 0
        elif inset < 0:
            covered = cv2.dilate(covered.astype(np.uint8), _disk(-inset / res)) > 0
        grids.append((extent, covered, heights))

    def contains(x, y, z=None):
        """In the lava at (x, y); with z (a root's height), only if that
        root is not well above the lava there (on a bank or a stone the
        lava reaches under)."""
        for (x0, x1, y0, y1), covered, heights in grids:
            col, row = int((x - x0) / res), int((y1 - y) / res)
            if 0 <= row < covered.shape[0] and 0 <= col < covered.shape[1] and covered[row, col]:
                if z is not None and z > heights[row, col] + rise:
                    continue
                return True
        return False

    return contains


def final_grass_query(ctx, overlays):
    """Final lava coverage, except the northern grass's replacement ground."""
    contains = coverage_query(overlays)
    if not dlc.enabled(ctx):
        return contains

    def query(x, y, z=None):
        if 4900 <= x <= 7700 and 7800 <= y <= 8120:
            return False
        return contains(x, y, z)

    return query


def grass_noshadow(ctx, in_lava=None):
    """No grass-card shadows. On Dota's rebuilt lava also remove grass and
    confirmed oak-leaf cards over its final coverage, preserving original UVs."""
    import numpy as np
    in_lava = in_lava or dlc.in_dire_lava
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    used = mapinfo.model_materials(models)
    selective = dlc.enabled(ctx)
    kinds = GRASS + ((DIRE_LEAVES,) if selective else ())
    mats = [mat for mat in kinds if mat in used and mapinfo.material(ctx.tools, ctx.addon, ctx.map, mat)]
    if not selective:
        for mat in mats:
            ctx.override_material(mat, {"F_DO_NOT_CAST_SHADOWS": 1},
                                  "dota2-tools map renderer: grass cards, no sun shadows")
        if mats:
            ctx.log("lava: " + ", ".join(m.split("/")[-1] for m in mats) + " do not cast shadows")
        return

    placements = {rel: placed for rel, placed in mapinfo._aggregates(ctx.tools, ctx.addon, ctx.map)
                  if rel in models and any(mat.encode() in models[rel] for mat in mats)}
    groups = {mat: [[], [], [], []] for mat in mats}
    removed = kept = raised = leaves_removed = 0
    for rel, placed in placements.items():
        decoded = mapinfo.decompile_textured(models[rel])
        if not decoded:
            continue
        P, N, UV, sets = decoded[0]
        done = set()
        for dc, tf in placed:
            if dc >= len(sets) or (tf is None and dc in done):
                continue
            done.add(dc)
            mat, tris = sets[dc]
            if mat not in groups or not tris:
                continue
            T = np.asarray(tris, np.int64)
            parent = np.arange(len(P), dtype=np.int64)

            def root(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for tri in T:
                a, b = root(int(tri[0])), root(int(tri[1]))
                if a != b:
                    parent[b] = a
                a, b = root(int(tri[0])), root(int(tri[2]))
                if a != b:
                    parent[b] = a
            cards = {}
            for tri in T:
                cards.setdefault(root(int(tri[0])), []).append(tri)
            for card in cards.values():
                card = np.asarray(card, np.int64)
                idx = np.unique(card)
                V = P[idx] if tf is None else P[idx] @ tf[:, :3].T + tf[:, 3]
                base = V[V[:, 2] <= V[:, 2].min() + 2.0]
                centre = base[:, :2].mean(axis=0)
                northern = mat in GRASS and 4900 <= centre[0] <= 7700 and 7800 <= centre[1] <= 8120

                root_z = float(V[:, 2].min())

                def edge_crosses(a, b):
                    distance = float(np.linalg.norm(b[:2] - a[:2]))
                    steps = max(1, int(np.ceil(distance / 12.0)))
                    return any(in_lava(float(a[0] + (b[0] - a[0]) * k / steps),
                                       float(a[1] + (b[1] - a[1]) * k / steps), root_z)
                               for k in range(1, steps))

                overhang = not northern and (
                    any(in_lava(float(x), float(y), root_z) for x, y in V[:, :2])
                    or any(edge_crosses(a, b) for a, b in zip(V, np.roll(V, -1, axis=0))))
                if in_lava(float(centre[0]), float(centre[1]), root_z) or overhang:
                    removed += 1
                    leaves_removed += mat == DIRE_LEAVES
                    continue
                if northern:
                    V = V.copy()
                    V[:, 2] += dlc.DIRE_GROUND_Z + float(ctx.options.get("lift", 4.0)) - V[:, 2].min()
                    raised += 1
                normals = N[idx] if tf is None else N[idx] @ tf[:, :3]
                normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-8)
                remap = np.full(len(P), -1, np.int64)
                remap[idx] = np.arange(len(idx))
                out = groups[mat]
                out[0].append(V)
                out[1].append(normals)
                out[2].append(UV[idx])
                out[3].append(remap[card])
                kept += 1

    clones = {}
    tints = mapinfo.aggregate_tints(ctx.tools, ctx.addon, ctx.map)
    for mat in mats:
        clone = f"materials/dota2tools/{Path(mat).stem}_keep.vmat"
        tint = next((tints[rel[:-2]] for rel in placements if mat.encode() in models[rel]
                     and tints.get(rel[:-2]) is not None), (255, 255, 255))
        clones[mat] = ctx.override_material(
            mat, {"F_DO_NOT_CAST_SHADOWS": 1,
                  "g_vColorTint": [c / 255 for c in tint] + [0.0]},
            "dota2-tools map renderer: grass outside lava, its aggregate tint, no sun shadows", out=clone)
    for mat, (positions, normals, uvs, triangles) in groups.items():
        if not triangles:
            continue
        P, N, UV = np.concatenate(positions), np.concatenate(normals), np.concatenate(uvs)
        tris, offset = [], 0
        for T in triangles:
            tris.append(T + offset)
            offset += int(T.max()) + 1
        T = np.concatenate(tris)
        stem = f"lava_grass_{Path(mat).stem}"
        ctx.write(f"{FOLDER}/{stem}.dmx", corners.dmx_text(T, P, N, 0, uv_values=UV))
        vmdl = ctx.write(f"{FOLDER}/{stem}.vmdl", corners.VMDL.format(
            material=clones[mat], name=stem, mesh=f"{FOLDER}/{stem}.dmx"))
        ctx.compile([vmdl])
        ctx.add_model(vmdl, stem, (0, 0, 0))
    def clear(im):
        from PIL import Image
        return Image.new("RGBA", im.size, (0, 0, 0, 0))

    for mat in mats:
        alias = f"materials/dota2tools/{Path(mat).stem}_hidden.vmat"
        while len(alias) < len(mat):
            alias = alias[:-5] + "_0.vmat"
        assert len(alias) == len(mat)
        hidden = ctx.override_material(
            mat, HIDDEN,
            "dota2-tools map renderer: original foliage replaced by filtered cards",
            edits={"TextureColor": clear}, out=alias)
        for rel in placements:
            if mat.encode() in models[rel]:
                replacement = hidden.encode()
                assert len(replacement) == len(mat.encode())
                ctx.rename_in_map(rel, mat.encode(), replacement)
    if mats:
        ctx.log(f"lava: foliage filtered, {removed} cards in lava removed ({leaves_removed} oak-leaf), "
                f"{kept} kept without shadows; {raised} northern grass cards lifted with their ground")


def spatial_groups(P, sets, gap):
    """Face sets whose bounds meet after `gap` units of growth, transitively.

    One world model can hold the whole base channel and isolated lava patches
    thousands of units away. Each group gets its own atlas, so a distant patch
    cannot force the base channel through an atlas-tile boundary.
    """
    import numpy as np
    boxes = {}
    for i, tris in enumerate(sets):
        if not tris:
            continue
        V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
        boxes[i] = (float(V[..., 0].min()) - gap, float(V[..., 0].max()) + gap,
                    float(V[..., 1].min()) - gap, float(V[..., 1].max()) + gap)

    def meet(a, b):
        return not (a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2])

    groups = []
    for i, box in boxes.items():
        touching = [g for g in groups if any(meet(box, boxes[j]) for j in g)]
        merged = {i}.union(*touching) if touching else {i}
        groups = [g for g in groups if all(g is not h for h in touching)]
        groups.append(merged)
    return [[t for i in sorted(g) for t in sets[i]] for g in groups]


def bake_plain(ctx, stem, m, layer, tint, P, tris, repeat, data=None, roshan=False):
    """The lava over the mesh's extent: the lava layer's texture where the
    mesh's vertex paint (its VertexPaintTintColor: bright along the flow,
    dark at the banks and the rocks -- the map's own shading, which the
    current shader leaves unused) is bright, the blend's crust where it is
    dark, glowing where the self-illum mask says, shaded by the world around
    (lightbake). Returns (extent, the texture's path). The bake every lava
    patch but the Dire corner's gets: over its own mesh, nothing grown."""
    import cv2
    import numpy as np
    from PIL import Image
    from dota2tools import lightbake
    o = ctx.options
    res, tex_res, margin = float(o.get("bake_res", 8.0)), float(o.get("plain_texture_res", 3.0)), 600.0
    V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
    x0, y0 = V[..., 0].min(), V[..., 1].min()
    x1, y1 = V[..., 0].max(), V[..., 1].max()
    region = (x0 - margin, x1 + margin, y0 - margin, y1 + margin)
    ctx.log(f"lava: reading the world's geometry around {stem}")
    world = solid_meshes(ctx)
    H, _ = lightbake.height_field(world, region, res)
    H = lightbake._fill(H)
    # Thin things (chains hung over the lava, poles) shade almost nothing: an
    # opening takes away what is narrower than ~9 cells.
    k = np.ones((9, 9), np.uint8)
    H = cv2.dilate(cv2.erode(H, k), k)
    Z, _ = lightbake.height_field([(stem, np.asarray(P, np.float32), np.asarray(tris, np.int64))], region, res)
    Z0 = Z
    Z = np.where(Z < -1e8, H, Z)
    pitch, yaw = light_direction(ctx)
    vis, ao = lightbake.shading(H, Z, res, pitch, yaw, ao_radius=float(o.get("ao_radius", 900.0)))
    # Lava lit itself: mostly how open each point is (the rocks and banks
    # around shut its light in), the sun's shadows only a little.
    sun = float(o.get("sun", 0.5))
    # The lava's openest stays at full brightness: occlusion relative to it
    # (the whole channel lies in a trench).
    lava_cells = Z0 > -1e8
    top = np.percentile(ao[lava_cells], 90) if lava_cells.any() else 1.0
    ao = np.clip(ao / max(top, 1e-3), 0, 1)
    S = ao ** float(o.get("occlusion", 2.5)) * ((1 - sun) + sun * vis)
    # Where the lava meets rock (a bank, a boulder standing in it) it goes
    # dark over the last few hundred units: the distance to anything standing
    # above it.
    edge_units = float(o.get("edge", 160.0))
    if edge_units > 0:
        above = ((H - Z) > 24.0).astype(np.uint8)
        dist = cv2.distanceTransform(1 - above, cv2.DIST_L2, 5) * res
        e = np.clip(dist / edge_units, 0, 1)
        e = e * e * (3 - 2 * e)
        lo = float(o.get("edge_floor", 0.08))
        S = S * (lo + (1 - lo) * e)
    # Light bounced and glowing around: the shade spreads softly.
    soft = float(o.get("soften", 40.0)) / res
    if soft > 0:
        S = cv2.GaussianBlur(S.astype(np.float32), (0, 0), soft)
    # The mesh's extent, at the texture's resolution.
    m0, m1 = int(margin / res), int(margin / res)
    S = S[m1:S.shape[0] - m1, m0:S.shape[1] - m0]
    # Power-of-two sides (the tools want them), at least tex_res's detail.
    def side(span):
        return min(8192, 2 ** max(4, math.ceil(math.log2(max(span / tex_res, 1)))))
    W, Hh = side(x1 - x0), side(y1 - y0)
    S = cv2.resize(S, (W, Hh), interpolation=cv2.INTER_LINEAR)
    texa = np.asarray(mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tColor{layer}"])
                      .convert("RGBA"), np.float32)
    tex, height = texa[..., :3], texa[..., 3] / 255
    spec = mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tSpecular{layer}"])
    mask = np.asarray(spec.convert("RGBA").split()[1].resize((tex.shape[1], tex.shape[0])), np.float32) / 255
    th, tw = tex.shape[:2]
    xs = x0 + (np.arange(W) + 0.5) * (x1 - x0) / W
    ys = y1 - (np.arange(Hh) + 0.5) * (y1 - y0) / Hh
    ui = (np.mod(xs / repeat, 1.0) * tw).astype(np.int64) % tw
    vi = ((1 - np.mod(ys / repeat, 1.0)) * th).astype(np.int64) % th
    col = tex[vi][:, ui]
    glow = mask[vi][:, ui]
    g = float(o.get("glow", 0.6))
    lava = col * np.asarray(tint, np.float32) * (1 + g * glow[:, :, None])
    paint = None
    if data is not None and o.get("paint", True):
        streams, ptris = mapinfo.decompile_streams(data)
        if "texcoord$3" in streams and "position$0" in streams:
            grid, covered = lightbake.rasterize(streams["position$0"], ptris, streams["texcoord$3"][:, :3],
                                                (x0, x1, y0, y1), 6.0)
            k = grid.mean(2)
            # Uncovered cells (gaps): their neighbours'.
            k = np.where(covered, k, cv2.dilate(k, np.ones((5, 5), np.uint8)))
            paint = cv2.resize(k, (W, Hh), interpolation=cv2.INTER_LINEAR)[:, :, None]
    if paint is not None:
        # The blend's own way: the crust comes up through the lava where the
        # lava texture's height (its alpha: a network of cracks) stands above
        # a threshold the paint sets -- thin veins where the paint is full,
        # wide crust where it fades. The crust: the lava, darkened.
        a = height[vi][:, ui]
        thr = float(o.get("crust_base", 0.3)) + float(o.get("crust_span", 0.6)) * paint[:, :, 0]
        c = np.clip((thr - a) / float(o.get("crust_soft", 0.18)) + 0.5, 0, 1)
        c = (c * c * (3 - 2 * c))[:, :, None]
        dim = float(o.get("paint_dim", 0.65))
        out = (lava * c * ((1 - dim) + dim * paint) + lava * float(o.get("crust", 0.18)) * (1 - c)) * S[:, :, None]
    else:
        out = lava * S[:, :, None]
    if roshan:
        # Level ground does not trigger the occlusion edge above. Follow the
        # actual mesh's right and bottom borders into black instead.
        Pa = np.asarray(P, np.float32)
        Ta = np.asarray(tris, np.int64)
        cell = max((x1 - x0) / W, (y1 - y0) / Hh)
        _values, edge_cover = lightbake.rasterize(
            Pa, Ta, np.ones((len(Pa), 1), np.float32), (x0, x1, y0, y1), cell)
        if edge_cover.shape != (Hh, W):
            edge_cover = cv2.resize(edge_cover.astype(np.uint8), (W, Hh),
                                    interpolation=cv2.INTER_NEAREST) > 0
        rows, cols = np.arange(Hh)[:, None], np.arange(W)[None, :]
        right = np.where(edge_cover, cols, -1).max(axis=1)
        bottom = np.where(edge_cover, rows, -1).max(axis=0)
        distance = np.minimum(np.maximum(right[:, None] - cols, 0),
                              np.maximum(bottom[None, :] - rows, 0))
        width = float(o.get("roshan_edge", 96.0)) / cell
        fade = np.clip(distance / max(width, 1.0), 0, 1)
        fade = fade * fade * (3 - 2 * fade)
        out *= fade[:, :, None]
        ctx.log(f"lava: Roshan right/bottom edge fade {width * cell:g} units")
    rel = f"materials/dota2tools/textures/{stem}_baked.png"
    p = ctx.content / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).save(p)
    ctx.log(f"lava: {stem}'s light baked, {W} x {Hh} px, light at {pitch:.0f} deg down, yaw {yaw:.0f}")
    return (float(x0), float(x1), float(y0), float(y1)), rel


def corner_sets(ctx, P, sets):
    """Indices of the face sets the Dire corner's bake takes: those reaching
    into its area and every set touching them (the channels it joins), on
    dota with builtin:dire_lava_corner on; none otherwise."""
    import numpy as np
    if not dlc.enabled(ctx):
        return set()
    boxes = {}
    for i, tris in enumerate(sets):
        if tris:
            V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
            boxes[i] = (float(V[..., 0].min()), float(V[..., 0].max()),
                        float(V[..., 1].min()), float(V[..., 1].max()))

    def meet(a, b):
        return not (a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2])

    picked = {i for i, b in boxes.items() if meet(b, dlc.CORNER_BOX)}
    grew = True
    while grew:
        grew = False
        for i, b in boxes.items():
            if i not in picked and any(meet(b, boxes[j]) for j in picked):
                picked.add(i)
                grew = True
    return picked


def south_blend(ctx):
    """The joined overlay's fade into the original-mesh southern channel."""
    if not dlc.enabled(ctx):
        return None
    low = float(ctx.options.get("corner_min_y", 4096.0))
    width = max(1.0, float(ctx.options.get("corner_south_fade", 512.0)))
    return low, low + width


def roshan_set(ctx, P, tris):
    """The base Dota map's Roshan-pit face set, by its world bounds."""
    if ctx.map != "dota" or ctx.addon or not tris:
        return False
    import numpy as np
    V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
    x = (float(V[..., 0].min()) + float(V[..., 0].max())) / 2
    y = (float(V[..., 1].min()) + float(V[..., 1].max())) / 2
    return -3900 <= x <= -2700 and 2100 <= y <= 3300


def plain_piece(ctx, stem, mat, m, layer, tint, P, N, tris, data, repeat, roshan=False):
    """A lava patch as the map has it: its own mesh, its light baked, drawn
    unlit just above the map's."""
    box, png = bake_plain(ctx, stem, m, layer, tint, P, tris, repeat, data, roshan=roshan)
    bx0, bx1, by0, by1 = box
    vmat = f"materials/dota2tools/{stem}.vmat"
    ctx.write(f"{FOLDER}/{stem}.dmx", corners.dmx_text(
        tris, P, N, 0.0, uv=lambda x, y: ((x - bx0) / (bx1 - bx0), (by1 - y) / (by1 - by0))))
    ctx.write(vmat, f"// dota2-tools map renderer: {mat}'s lava layer {layer}, its light baked\n\n"
              "Layer0\n{\n\tshader \"global_lit_simple.vfx\"\n\tF_FULLBRIGHT 1\n"
              f"\tTextureColor \"{png}\"\n}}\n")
    ctx.compile([vmat])
    vmdl = ctx.write(f"{FOLDER}/{stem}.vmdl", corners.VMDL.format(material=vmat, name=stem,
                                                                  mesh=f"{FOLDER}/{stem}.dmx"))
    ctx.compile([vmdl])
    ctx.add_model(vmdl, stem, (0, 0, float(ctx.options.get("plain_lift", 1.0))))
    ctx.log(f"lava: {stem} on {mat} layer {layer}")


def hidden_source(ctx, mat):
    """A same-length transparent material alias for an original lava mesh."""
    from PIL import Image
    alias = str(Path(mat).with_name("nil_" + Path(mat).name[4:]))
    assert len(alias) == len(mat) and alias != mat

    def clear(im):
        return Image.new("RGBA", im.size, (0, 0, 0, 0))

    ctx.override_material(
        mat, HIDDEN,
        "dota2-tools map renderer: original lava replaced by its baked overlay",
        edits={"TextureColor": clear}, out=alias)
    return alias


def run(ctx):
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    lift = float(ctx.options.get("lift", 4.0))
    bright = float(ctx.options.get("brightness", 1.5))
    repeat = float(ctx.options.get("repeat", 384.0))
    tints = mapinfo.aggregate_tints(ctx.tools, ctx.addon, ctx.map)
    bake_enabled = bool(ctx.options.get("bake", True))
    group_gap = float(ctx.options.get("bridge", 512.0)) + float(ctx.options.get("extend", 128.0))
    pieces, overlays, hidden = [], [], {}
    for rel, data in models.items():
        mats = set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", data))
        if len(mats) != 1:
            continue
        mat = mats.pop().decode()
        m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, mat)
        layer = lava_layer(m)
        if layer is None:
            continue
        meshes = mapinfo.decompile_model(data)
        if not meshes:
            ctx.log(f"lava: could not decompile {rel}")
            continue
        P, N, sets = meshes[0]
        tint = [bright * c / 255 for c in tints.get(rel[:-2], (255, 255, 255))] \
            if m["ints"].get("F_TINT_MASK") else [bright] * 3
        tint = [t_ * g for t_, g in zip(tint, ctx.options.get("colour_gain", [1.15, 1.6, 2.0]))]
        base = Path(rel).name.replace(".vmdl_c", "")
        # Only the Dire corner's channels get the grown, joined bake; every
        # other patch (Roshan's pit, the base's small pools) stays as the map
        # draws it, its light baked over its own mesh.
        picked = corner_sets(ctx, P, sets) if bake_enabled else set()
        rest, roshan = [], []
        for i, fs in enumerate(sets):
            if i in picked:
                continue
            target = roshan if roshan_set(ctx, P, fs) else rest
            target.extend(fs)
        if rest:
            plain_piece(ctx, base if not picked else f"{base}_plain", mat, m, layer, tint, P, N, rest,
                        data, float(ctx.options.get("plain_repeat", 1200.0)))
        if roshan:
            gain = float(ctx.options.get("roshan_brightness", 1.8))
            plain_piece(ctx, f"{base}_roshan", mat, m, layer, [v * gain for v in tint], P, N, roshan,
                        data, float(ctx.options.get("plain_repeat", 1200.0)), roshan=True)
            ctx.log(f"lava: Roshan brightness {gain:g}")
        if not picked:
            continue
        sets = [fs for i, fs in enumerate(sets) if i in picked]
        south = south_blend(ctx)
        if south is not None:
            import numpy as np
            selected = [t for fs in sets for t in fs]
            V = np.asarray(P, np.float32)[np.asarray(selected, np.int64)]
            extent = (float(V[..., 0].min()) - 1, float(V[..., 0].max()) + 1,
                      float(V[..., 1].min()) - 1, south[1])
            uP, uN, uT = clip_mesh(P, N, selected, extent)
            if uT:
                plain_piece(ctx, f"{base}_south", mat, m, layer, tint, uP, uN, uT,
                            data, float(ctx.options.get("plain_repeat", 1200.0)))
        if mat not in hidden:
            hidden[mat] = hidden_source(ctx, mat)
        ctx.rename_in_map(rel, mat.encode(), hidden[mat].encode())
        groups = spatial_groups(P, sets, group_gap)
        if len(groups) > 1:
            ctx.log(f"lava: {base} split into {len(groups)} spatial groups")
        for gi, tris in enumerate(groups):
            stem = base if len(groups) == 1 else f"{base}_g{gi}"
            pieces.append((stem, mat, m, layer, tint, P, N, tris, data))
    if not pieces:
        return
    # Optional shared atlas (usually off: the Dire channel is huge, one atlas
    # hits max_side and looks pixelated). World-space sampling already aligns
    # neighbouring blocks' colour.
    shared = None
    if ctx.options.get("shared_atlas", False) and len(pieces) > 1:
        import numpy as np
        grow = float(ctx.options.get("extend", 128.0))
        xs, ys = [], []
        for _, _, _, _, _, P, _, tris, _ in pieces:
            V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
            xs += [float(V[..., 0].min()) - grow, float(V[..., 0].max()) + grow]
            ys += [float(V[..., 1].min()) - grow, float(V[..., 1].max()) + grow]
        shared = (min(xs), max(xs), min(ys), max(ys))
        ctx.log(f"lava: shared atlas x {shared[0]:.0f}..{shared[1]:.0f}, "
                f"y {shared[2]:.0f}..{shared[3]:.0f} ({len(pieces)} meshes)")
    import numpy as np
    tex_res = float(ctx.options.get("texture_res", 0.75))
    max_side = int(ctx.options.get("max_side", 4096))
    overlap = max(float(ctx.options.get("tile_overlap", 224.0)),
                  2 * float(ctx.options.get("feather", 32.0)) + 32.0)
    for stem, mat, m, layer, tint, P, N, tris, data in pieces:
        if not ctx.options.get("bake", True):
            vmat = f"materials/dota2tools/{stem}.vmat"
            ctx.write(f"{FOLDER}/{stem}.dmx", corners.dmx_text(tris, P, N, lift, repeat))
            unlit = bool(ctx.options.get("unlit", False))
            textures = {"g_tColor": m["textures"][f"g_tColor{layer}"]}
            edits = {}
            if not unlit and ctx.options.get("glow", True):
                textures["g_tSelfIllumMask"] = m["textures"][f"g_tSpecular{layer}"]
                edits["TextureSelfIllumMask"] = lambda im: im.convert("RGBA").split()[1]
            source = {"shader": "global_lit_simple.vfx", "ints": {"F_FULLBRIGHT": 1} if unlit else {},
                      "floats": {}, "vectors": {"g_vColorTint": tint + [0.0],
                                                "g_vTexCoordScale": [1.0, 1.0, 0.0, 0.0],
                                                "g_vTexCoordOffset": [0.0, 0.0, 0.0, 0.0]},
                      "textures": textures}
            ctx.override_material(mat, {}, f"dota2-tools map renderer: {mat}'s lava layer {layer}",
                                  edits=edits, out=vmat, source=source)
            vmdl = ctx.write(f"{FOLDER}/{stem}.vmdl", corners.VMDL.format(material=vmat, name=stem,
                                                                          mesh=f"{FOLDER}/{stem}.dmx"))
            ctx.compile([vmdl])
            ctx.add_model(vmdl, stem, (0, 0, 0))
            ctx.log(f"lava: {stem} on {mat} layer {layer}")
            V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
            box = (float(V[..., 0].min()), float(V[..., 0].max()),
                   float(V[..., 1].min()), float(V[..., 1].max()))
            overlays.append((box, P, tris))
            continue
        grow = float(ctx.options.get("extend", 128.0))
        if dlc.enabled(ctx):
            # Room for the rim past the lava's outer edge.
            grow += float(ctx.options.get("rim", 160.0))
        if shared is not None:
            full = shared
        else:
            V = np.asarray(P, np.float32)[np.asarray(tris, np.int64)]
            full = (float(V[..., 0].min()) - grow, float(V[..., 0].max()) + grow,
                    float(V[..., 1].min()) - grow, float(V[..., 1].max()) + grow)
        fit = max((full[1] - full[0]) / max_side, (full[3] - full[2]) / max_side)
        # A few percent over the requested resolution is better than cutting
        # a channel at an atlas-tile boundary. Genuinely huge channels tile.
        tile_res = fit if tex_res < fit <= tex_res * 1.25 else tex_res
        tiles = atlas_tiles(*full, tile_res, max_side, overlap)
        if len(tiles) > 1:
            ctx.log(f"lava: {stem} split into {len(tiles)} atlas tiles "
                    f"(max {max_side} px at {tile_res:g} u/px)")
        light_cache = {}
        for ti, extent in enumerate(tiles):
            tstem = stem if len(tiles) == 1 else f"{stem}_t{ti}"
            tx0, tx1, ty0, ty1 = extent
            # A forced join can be the tile's only lava. Do not skip it merely
            # because no original triangle reaches its source clip rectangle.
            has_source = bool(clip_tris(P, N, tris, tx0, tx1, ty0, ty1, pad=overlap))
            if not has_source and dlc.enabled(ctx):
                mesh_res = float(ctx.options.get("mesh_res", 12.0))
                shape = (max(2, int(math.ceil((ty1 - ty0) / mesh_res))),
                         max(2, int(math.ceil((tx1 - tx0) / mesh_res))))
                has_source = any(
                    forced is not None and forced.any()
                    for forced in (dlc.dire_connection(ctx, extent, shape),
                                   dlc.dire_connection(ctx, extent, shape, rock=True),
                                   dlc.dire_connection(ctx, extent, shape, ground=True)))
            if not has_source:
                ctx.log(f"lava: {tstem} empty, skipping")
                continue
            box, png, oP, oN, oT = bake(ctx, tstem, m, layer, tint, P, tris, repeat, data,
                                        extent=extent, light_cache=light_cache, light_extent=full)
            ownership = list(tile_ownership(extent, tiles))
            south = south_blend(ctx)
            if south is not None:
                ownership[2] = max(ownership[2], south[0])
            oP, oN, oT = clip_mesh(oP, oN, oT, ownership)
            if not oT:
                ctx.log(f"lava: {tstem} overlay empty, skipping")
                continue
            overlays.append((box, oP, oT))
            bx0, bx1, by0, by1 = box
            vmat = f"materials/dota2tools/{tstem}.vmat"
            ctx.write(f"{FOLDER}/{tstem}.dmx", corners.dmx_text(
                oT, oP, oN, lift,
                uv=lambda x, y, bx0=bx0, bx1=bx1, by0=by0, by1=by1:
                    ((x - bx0) / (bx1 - bx0), (by1 - y) / (by1 - by0))))
            ctx.write(vmat,
                      f"// dota2-tools map renderer: {mat}'s lava layer {layer}, its light baked\n\n"
                      "Layer0\n{\n\tshader \"global_lit_simple.vfx\"\n\tF_FULLBRIGHT 1\n"
                      "\tF_TRANSLUCENT 1\n"
                      f"\tTextureColor \"{png}\"\n"
                      f"\tTextureTranslucency \"{Path(png).with_name(f'{tstem}_translucency.png')}\"\n}}\n")
            ctx.compile([vmat])
            vmdl = ctx.write(f"{FOLDER}/{tstem}.vmdl",
                             corners.VMDL.format(material=vmat, name=tstem,
                                                 mesh=f"{FOLDER}/{tstem}.dmx"))
            ctx.compile([vmdl])
            ctx.add_model(vmdl, tstem, (0, 0, 0))
            ctx.log(f"lava: {tstem} on {mat} layer {layer} ({len(oT)} tris)")
    if ctx.options.get("grass_noshadow", True):
        grass_noshadow(ctx, final_grass_query(ctx, overlays))
