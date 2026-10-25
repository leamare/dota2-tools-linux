"""Pre-run script: the lava, glowing.

The map's lava (on dota: the channels along the Dire base's border) is
terrain on a blend material whose lava layers glow through a self-illum
mask. The map's world meshes carry an emissive factor of 0 (in the map
itself, which a render addon can't change), so in the tools the lava draws
as dark, unlit rock. Here each world model on such a material is copied
(its mesh decompiled by source2viewer-cli) into a model of the render addon
on an unlit material of the lava layer's own texture, and put into the SFM
session just above the map's.

Options (settings.json, render.lava): "lift" (units above the map's lava,
default 1), "brightness" (the lava texture's multiplier, default 1.5: it glows),
"repeat" (world units per repeat of the lava texture, default 768).
"""
import math
import re
from pathlib import Path

from dota2tools import mapinfo
from dota2tools.prerun import corners

FOLDER = "models/dota2tools"


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


def light_direction(ctx):
    """The map's sunlight: (pitch, yaw) in degrees, the way its global light
    points (down, and where the light travels) -- the render's own sun if
    one is set; Dota's default 66, 0."""
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
    """The world's geometry without what light passes through: alpha-tested
    or translucent materials (leaf and grass cards, solid quads as geometry,
    would cast rectangles), and the editor's dev/ and tools/ materials."""
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    see_through = {}

    def clear(rel):
        if rel not in see_through:
            names = set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", models.get(rel, b"")))
            mats = [mapinfo.material(ctx.tools, ctx.addon, ctx.map, n.decode()) for n in names]
            # And the editor's own: the black void beyond the corners, tool
            # brushes.
            see_through[rel] = any(n.startswith((b"materials/dev/", b"materials/tools/")) for n in names) or any(
                mt and (mt["ints"].get("F_ALPHA_TEST") or mt["ints"].get("F_TRANSLUCENT")) for mt in mats)
        return see_through[rel]
    return [w for w in mapinfo.world_meshes(ctx.tools, ctx.addon, ctx.map) if not clear(w[0])]


def bake(ctx, stem, m, layer, tint, P, tris, repeat, data=None):
    """The lava over the mesh's extent: the lava layer's texture where the
    mesh's vertex paint (its VertexPaintTintColor: bright along the flow,
    dark at the banks and the rocks -- the map's own shading, which the
    current shader leaves unused) is bright, the blend's crust where it is
    dark, glowing where the self-illum mask says, shaded by the world around
    (lightbake). Returns (extent, the texture's path)."""
    import cv2
    import numpy as np
    from PIL import Image
    from dota2tools import lightbake
    o = ctx.options
    res, tex_res, margin = float(o.get("bake_res", 8.0)), float(o.get("texture_res", 3.0)), 600.0
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
    rel = f"materials/dota2tools/textures/{stem}_baked.png"
    p = ctx.content / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).save(p)
    ctx.log(f"lava: {stem}'s light baked, {W} x {Hh} px, light at {pitch:.0f} deg down, yaw {yaw:.0f}")
    return (float(x0), float(x1), float(y0), float(y1)), rel


def run(ctx):
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    lift = float(ctx.options.get("lift", 1.0))
    bright = float(ctx.options.get("brightness", 1.5))
    repeat = float(ctx.options.get("repeat", 1200.0))
    tints = mapinfo.aggregate_tints(ctx.tools, ctx.addon, ctx.map)
    done = 0
    for rel, data in models.items():
        mats = set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", data))
        if len(mats) != 1:
            continue
        mat = mats.pop().decode()
        m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, mat)
        layer = lava_layer(m)
        if layer is None:
            continue
        stem = Path(rel).name.replace(".vmdl_c", "")
        meshes = mapinfo.decompile_model(data)
        if not meshes:
            ctx.log(f"lava: could not decompile {rel}")
            continue
        P, N, sets = meshes[0]
        tris = [t for fs in sets for t in fs]
        # The tint the map draws this mesh with (its material's tint mask
        # takes it: on dota, orange), and the brightness.
        tint = [bright * c / 255 for c in tints.get(rel[:-2], (255, 255, 255))] \
            if m["ints"].get("F_TINT_MASK") else [bright] * 3
        # Brought to the interactive map's picture: its lava is more orange.
        tint = [t_ * g for t_, g in zip(tint, ctx.options.get("colour_gain", [1.15, 1.6, 2.0]))]
        vmat = f"materials/dota2tools/{stem}.vmat"
        if ctx.options.get("bake", True):
            # Its light baked into one texture over the mesh (lightbake);
            # drawn unlit. UVs: the texture's place over the mesh's extent.
            box, png = bake(ctx, stem, m, layer, tint, P, tris, repeat, data)
            bx0, bx1, by0, by1 = box
            ctx.write(f"{FOLDER}/{stem}.dmx", corners.dmx_text(
                tris, P, N, 0.0, uv=lambda x, y: ((x - bx0) / (bx1 - bx0), (by1 - y) / (by1 - by0))))
            ctx.write(vmat, f"// dota2-tools map renderer: {mat}'s lava layer {layer}, its light baked\n\n"
                      "Layer0\n{\n\tshader \"global_lit_simple.vfx\"\n\tF_FULLBRIGHT 1\n"
                      f"\tTextureColor \"{png}\"\n}}\n")
            ctx.compile([vmat])
        else:
            # The mesh's own UVs are per face (made for the blend); the lava
            # texture goes on by world position instead.
            ctx.write(f"{FOLDER}/{stem}.dmx", corners.dmx_text(tris, P, N, 0.0, repeat))
            unlit = bool(ctx.options.get("unlit", False))
            textures = {"g_tColor": m["textures"][f"g_tColor{layer}"]}
            edits = {}
            if not unlit and ctx.options.get("glow", True):
                # Lit, so the rocks shade it; the glow from the layer's own
                # self-illum mask (green, in its compiled specular map).
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
        ctx.add_model(vmdl, stem, (0, 0, lift))
        ctx.log(f"lava: {stem} on {mat} layer {layer}")
        done += 1
    if not done:
        ctx.log(f"lava: {ctx.map} has none")
