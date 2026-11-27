"""Pre-run script: the Dire base's lava corner on dota.

Dota 7.42 left the Dire base's lava as two channels that stop short of
each other behind the fountain, cut off square by the map's black corner.
This joins them: one lava sheet from the northern channel round the corner
to the eastern one, whose outer edge (an arc south of the north-east stone,
the gaps between the eastern bank's stones) darkens to black crust, then
the corner's dark rock, then fades into the ground. The northern grass
gets a dark-rock shelf of its own, lifted with its replacement ground; the
lava the join makes darkens toward the stones in it as the map's does.

The work is done by builtin:lava, which bakes the lava: this script holds
the corner's shapes and turns them on. With it in the pre-run list (dota
only), builtin:lava joins the corner; without it, the lava is the map's.
Its options live with the lava's (settings.json, render.lava): "rim" (the
rim's width, default 160), "rim_crust" (the black crust's, 36), "rim_shade"
(the rock's brightness, 0.8), "shelf_fade" (72), "shelf_top" (120),
"shelf_seam" (24), "fall" and "fall_floor" (the lava's darkening to its
outer edge, 160 and 0.12), "stone_rise", "stone_fall" (its darkening
toward stones, 48 and 120), "join_light" (0.35), "join_soft" (256).
"""
import math

from dota2tools import mapinfo
from dota2tools.prerun import corners

SCRIPT = "builtin:dire_lava_corner"


def enabled(ctx):
    """On for dota with this script in the render's pre-run list."""
    if ctx.map != "dota":
        return False
    r = ctx.tools.settings["render"]
    return r.get("prerun_enabled", True) and SCRIPT in r.get("prerun", [])


# The corner the join is made in (x0, x1, y0, y1): builtin:lava gives its
# grown, joined bake to the lava face sets reaching into it and the ones
# they touch (the base's three channels), and to nothing else.
CORNER_BOX = (7300.0, 8800.0, 6600.0, 8200.0)

DIRE_JOIN = (
    ((7625, 7660), (7700, 7900), (7980, 7900), (8230, 7700), (8470, 7480),
     (8660, 7200), (8660, 6920), (7900, 6840), (7800, 7000), (7800, 7250),
     (7600, 7450), (7350, 7425)),
    ((5900, 7260), (7900, 7260), (7900, 6990), (5900, 6990)),
    ((4900, 7800), (7700, 7800), (7700, 7480), (4900, 7480)),
    ((7875, 7200), (7975, 7200), (7975, 7075), (7875, 7075)),
    ((8350, 7600), (8680, 7600), (8680, 6800), (8350, 6800)),
    # The corner under the arc (DIRE_EDGE cuts it to the curve).
    ((7600, 7830), (8700, 7830), (8700, 7200), (7600, 7200)),
)
# Northern grass roots occupy y=7660..7843. Its southernmost cards yield to
# the lava channel; textured dark rock supports the surviving band to its north.
# The surrounding corner ground is at z=256.
DIRE_ROCK = (((4900, 7920), (7700, 7920), (7700, 7800), (4900, 7800)),)
DIRE_GROUND = (((4900, 8120), (7700, 8120), (7700, 7920), (4900, 7920)),)
DIRE_GROUND_Z = 256.0
# Fullbright multipliers. Banks stay darker than the adjacent lit highground.
DIRE_BANK_TINT = (1.30, 1.20, 1.65)
DIRE_GROUND_TINT = (1.24, 1.22, 1.66)


# The north-east edge of the joined lava: the bank's line from the northern
# strip down to the eastern channel's rocks (x, y). Lava north of it is cut.
def _bezier(p0, p1, p2, p3, n=48):
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append(tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d
                         for a, b, c, d in zip(p0, p1, p2, p3)))
    return tuple(out)


# One arc from the northern strip round the corner to the eastern channel's
# rocks, inside the bank's line (x, y); flat before it.
DIRE_EDGE = ((7300, 7812),) + _bezier((7560, 7812), (7980, 7812), (8200, 7640), (8300, 7470)) \
    + _bezier((8300, 7470), (8380, 7330), (8560, 7300), (8700, 7290))[1:]
# Gaps between the eastern bank's stones (x where the lava stops, y range).
DIRE_EAST_GAPS = ((8760, 5560, 5980), (8790, 6500, 6850))
# Where the lava's outer edge shows (not under the stones): a rim of black
# crust and dark rock fades into the ground there.
DIRE_RIM = (((4900, 8200), (8760, 8200), (8760, 7250), (4900, 7250)),
            ((8700, 6100), (9100, 6100), (9100, 5500), (8700, 5500)),
            ((8700, 6900), (9100, 6900), (9100, 6450), (8700, 6450)))


def _grid(extent, shape):
    import numpy as np
    x0, x1, y0, y1 = extent
    h, w = shape
    return (x0 + (np.arange(w) + 0.5) * (x1 - x0) / w,
            y1 - (np.arange(h) + 0.5) * (y1 - y0) / h)


def dire_cut(ctx, extent, shape):
    """Cells north of DIRE_EDGE (no lava there), or None when off."""
    if not enabled(ctx):
        return None
    import numpy as np
    xx, yy = _grid(extent, shape)
    ex, ey = np.asarray(DIRE_EDGE, np.float64).T
    line = np.interp(xx, ex, ey)
    inside = (xx >= ex[0]) & (xx <= ex[-1])
    cut = (yy[:, None] > line[None, :]) & inside[None, :]
    # The eastern channel's outer bank between its stones: the lava stops
    # short of the atlas's edge, so its rim has room to fade into the ground.
    for (gx, gy0, gy1) in DIRE_EAST_GAPS:
        cut |= (xx[None, :] > gx) & (yy[:, None] >= gy0) & (yy[:, None] <= gy1)
    return cut


def _stones(ctx, extent, shape, above):
    """Cells (rows, cols) where world geometry (stones, cliffs) stands higher
    than `above`, or None."""
    import numpy as np
    import cv2
    from dota2tools import lightbake
    world = getattr(ctx, "_lava_world", None)
    if world is None:
        from dota2tools.prerun import lava
        world = ctx._lava_world = lava.solid_meshes(ctx)
    res = 8.0
    Z, _ = lightbake.height_field(world, extent, res)
    if Z is None:
        return None
    Z = cv2.resize(Z.astype(np.float32), (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    return Z > above


def dire_rim(ctx, extent, shape):
    """Cells where the lava's outer edge gets the rim, or None when off."""
    if not enabled(ctx):
        return None
    import numpy as np
    xx, yy = _grid(extent, shape)
    out = np.zeros(shape, bool)
    for poly in DIRE_RIM:
        (ax0, ay1), (ax1, _), (_, ay0), _ = poly
        out |= ((xx >= ax0) & (xx <= ax1))[None, :] & ((yy >= ay0) & (yy <= ay1))[:, None]
    return out


def in_dire_lava(x, y, z=None):
    """A grass root inside Dota's manual lava coverage or northern channel."""
    import cv2
    import numpy as np
    if 4900 <= x <= 7700 and 7000 <= y <= 7800:
        return True
    if 7700 < x <= 7820 and 7000 <= y <= 7900:
        return True
    return any(cv2.pointPolygonTest(np.asarray(poly, np.int32), (x, y), False) >= 0
               for poly in DIRE_JOIN)


def dire_connection(ctx, extent, shape, rock=False, ground=False):
    """Dota 7.42's missing lava behind the Dire fountain, on a coarse grid.
    rock/ground select the northern bank or the grass's replacement ground.
    With this script off, there is neither."""
    if not enabled(ctx):
        return None
    import cv2
    import numpy as np
    x0, x1, y0, y1 = extent
    h, w = shape
    out = np.zeros((h, w), np.uint8)
    polys = DIRE_GROUND if ground else DIRE_ROCK if rock else DIRE_JOIN
    for poly in polys:
        pts = np.array([((x - x0) * w / (x1 - x0), (y1 - y) * h / (y1 - y0)) for x, y in poly], np.int32)
        cv2.fillPoly(out, [pts], 1)
    return out


def corner_bank(ctx, extent, shape, ground=False):
    """The corner edge's rock or ground, tiled at atlas world positions.

    Source layers cover the northern strips west of the corner mesh too. No
    clamped atlas edges, stretched columns, or tile-dependent colour changes.
    """
    import cv2
    import numpy as np
    x0, x1, y0, y1 = extent
    H, W = shape
    xs = x0 + (np.arange(W) + 0.5) * (x1 - x0) / W
    ys = y1 - (np.arange(H) + 0.5) * (y1 - y0) / H
    selected = getattr(ctx, "_lava_bank_layers", None)
    if selected is None:
        if not mapinfo.black_corners(ctx.tools, ctx.addon, ctx.map):
            ctx._lava_bank_layers = ()
            return None
        models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
        layers = mapinfo.terrain_layers(ctx.tools, ctx.addon, ctx.map, models)
        if "dire" not in layers:
            return None
        blend = layers["dire"]["material"]
        if "lava" in blend.split("/")[-1]:
            blend = "materials/blends/mod_dire_000.vmat"
        m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, blend)
        main = (blend, 1 if "g_tColor1" in m["textures"] else 0)
        edge = mapinfo.edge_layers(ctx.tools, ctx.addon, ctx.map, "dire", models)
        selected = edge.get("flat", main), edge.get("steep", edge.get("flat", main))
        ctx._lava_bank_layers = selected
    if not selected:
        return None
    mat, layer = selected[0 if ground else 1]
    m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, mat)
    textures = getattr(ctx, "_lava_bank_textures", None)
    if textures is None:
        textures = ctx._lava_bank_textures = {}
    if (mat, layer) not in textures:
        im = corners.tinted(ctx, m, layer)(mapinfo.texture_image(
            ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tColor{layer}"]))
        textures[mat, layer] = np.ascontiguousarray(np.asarray(im.convert("RGB"), np.float32))
    tex = textures[mat, layer]
    repeat = 128.0 * float(m["floats"].get(f"g_flTexCoordScale{layer}", 1.0))
    angle = math.radians(float(m["floats"].get(f"g_flTexCoordRotate{layer}", 0.0)))
    X, Y = np.meshgrid(xs, ys)
    u = (X * math.cos(angle) - Y * math.sin(angle)) / repeat
    v = -(X * math.sin(angle) + Y * math.cos(angle)) / repeat
    th, tw = tex.shape[:2]
    uu, vv = (u % 1) * tw, (v % 1) * th
    colour = cv2.remap(tex, uu.astype(np.float32), vv.astype(np.float32), cv2.INTER_CUBIC,
                       borderMode=cv2.BORDER_WRAP)
    tint = DIRE_GROUND_TINT if ground else DIRE_BANK_TINT
    return colour * np.asarray(tint, np.float32), np.ones((H, W), bool)


def run(ctx):
    if not enabled(ctx):
        return
    lava = "builtin:lava" in ctx.tools.settings["render"].get("prerun", [])
    ctx.log("dire lava corner: " + ("joined by builtin:lava" if lava
                                    else "builtin:lava is not in the pre-run list: nothing to join"))
