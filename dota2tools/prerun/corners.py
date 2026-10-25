"""Pre-run script: the black fields beyond the map's corners, given ground.

Beyond the bottom-left and top-right corners the map has one model
(maps/<map>/worldnodes/..._agg_merge_black_0, on materials/dev/black.vmat)
that draws them black. A render addon can't replace a map's own model (the
map's copy wins), and one material couldn't give the two sides different
ground anyway. So each side's part of that mesh is written as a model of the
render addon -- a DMX of its triangles, a little above the black mesh --
on ground of the map's own, one texture baked over it the way the map's own
edge looks: every level of its ground on the side's floor layer, the steps
between levels and the very high ground in another (bake). Which
layers: from the world's vertex paint along the map's edge next to the
corner (mapinfo.edge_layers: what covers most of its flat ground and of its
slopes). Dire's floor is that flat layer (on dota the path blend's cracked
sand, on dota_ti10 its fractured stone, on dota_reef its reef ground);
Radiant's the main layer of its terrain material (dota's grass, a variant
map's counterpart); the rises take the edge's slope layer. Each layer is
tiled as the terrain tiles it (128 units times the layer's scale, its
rotation), triplanar so it follows the relief, and coloured as the terrain
shader colours it (tinted). The models go into the SFM session; lit by the
sun like everything else, they keep the mesh's own relief.

The material is global_lit_simple, not a copy of the terrain's multiblend:
on a model (not the map's own world geometry) multiblend draws black.

Options (settings.json, render.corners): "lift" (units above the black
mesh, default 2), "layer" / "layer_radiant" / "layer_dire" ("edge", the
default: the bake above; a number: that layer of the side's main ground
alone, tinted by "tint_<side>"), "floor_<side>" / "rock_<side>" (the
bake's layers instead, "material.vmat:layer"), "step_from" / "step_to"
(how far the ground within "step_window" units (72) spans where a step
between levels starts and is all rock: 80, 128 -- mounds under trees span
less), "slopes_<side>" (0..1: how much the slopes take the rock
besides the steps; Dire's 1, Radiant's 0), "slope_from" / "slope_to"
(rise over run: 0.4, 1.0), "slope_width" (units their rock reaches out,
24),
"high_from" / "high_to" (units above the floor where the ground starts and
is all rock: 512, 768), "rock_soften" (units, 24), "texture_res" (units per texel, 2), "uv_units"
(world units per texture repeat at a layer scale of 1, 128), "tint_radiant" / "tint_dire" ("layer", the
default: the terrain layer's own tint; empty: none -- Dota's ground textures
are near white, the terrain colours them; "#rrggbb", "r g b" or one grey
value), "shade_radiant" / "shade_dire" (r g b multipliers of that tint;
defaults in SHADE), "source_radiant" / "source_dire" (another blend
material's layer as that side's ground, "materials/blends/x.vmat:2"),
"unlit" (drawn flat, unlit).
"""
import math

from dota2tools import mapinfo

FOLDER = "models/dota2tools"
# The tint each side's layer tint is multiplied by: Radiant's grass on a lit
# model came out too saturated (lighter and greyer here); Dire's cliff as the
# map has it.
SHADE = {"radiant": [1.33, 1.10, 1.62], "dire": [0.85, 0.85, 0.85]}
# How much of a side's slopes take the rock besides its steps (0..1).
SLOPES = {"radiant": 0.0, "dire": 1.0}
# Another blend's layer for a side ("materials/blends/x.vmat:2"); none.
SOURCE = {}
# The layer of each side's main ground the corner takes (else 1, its main
# one); "edge": the layer the map's own edge next to the corner is painted
# with (Dire's: the cliff layer tried before matched none of the maps' edges).
LAYER = {"radiant": "edge", "dire": "edge"}
# The shades when a source_<side> material is missing.
OWN_SHADE = SHADE


def tint_of(spec, m, layer):
    """A side's tint option: "" (the texture as it is), "layer" (the terrain
    layer's own tint), "#rrggbb", "r g b" (0..1) or one number (grey)."""
    spec = str(spec).strip()
    if not spec:
        return [1.0, 1.0, 1.0]
    if spec == "layer":
        return list(m["vectors"].get(f"g_vColorTint{layer}", [1, 1, 1, 0]))[:3]
    if spec.startswith("#"):
        return [int(spec[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    v = [float(x) for x in spec.split()]
    return (v * 3)[:3] if len(v) == 1 else v[:3]


def simple_material(m, layer, tint, unlit=False):
    """A global_lit_simple material dict with multiblend m's layer's colour
    texture, tinted (UVs come from the mesh: world position over the layer's
    repeat)."""
    ints = {"F_FULLBRIGHT": 1} if unlit else {}
    return {"shader": "global_lit_simple.vfx", "ints": ints, "floats": {},
            "vectors": {"g_vColorTint": list(tint) + [0.0], "g_vTexCoordScale": [1.0, 1.0, 0.0, 0.0],
                        "g_vTexCoordOffset": [0.0, 0.0, 0.0, 0.0]},
            "textures": {"g_tColor": m["textures"][f"g_tColor{layer}"]}}


def tinted(ctx, m, layer):
    """An edit of layer `layer`'s colour texture: coloured as multiblend does
    it. Each layer has two tints, g_vColorTint<layer> and ...TintB<layer>
    (alpha 0: unset); with both set the tint mask's channel <layer> mixes
    them per texel (1: the first -- dota_ti10's teal glow in its cracks),
    else the one set applies everywhere."""
    import numpy as np
    from PIL import Image

    def tint(name):
        v = m["vectors"].get(name, [1, 1, 1, 0])
        return np.array(v[:3], np.float32) if v[3] > 0 else None
    a, b = tint(f"g_vColorTint{layer}"), tint(f"g_vColorTintB{layer}")
    mask = None
    if a is not None and b is not None and m["ints"].get("F_TINT_MASK") and m["textures"].get("g_tTintMasks"):
        mask = mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"]["g_tTintMasks"]).convert("RGBA")
    one = a if a is not None else b if b is not None else np.ones(3, np.float32)

    def edit(im):
        rgba = im.convert("RGBA")
        c = np.asarray(rgba, np.float32) / 255
        if mask is not None:
            k = np.asarray(mask.resize(rgba.size, Image.BILINEAR), np.float32)[:, :, layer:layer + 1] / 255
            c[:, :, :3] *= b + (a - b) * k
        else:
            c[:, :, :3] *= one
        return Image.fromarray((np.clip(c, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
    return edit


def bake(ctx, side, tris, P, flat, steep):
    """One texture over the corner's extent: the `flat` layer ((blend
    material, layer)) on its floor, blending into the `steep` one where the
    mesh rises above it or slopes steeply, as the map's own edge goes from
    its ground into rock. From the corner mesh's own heights; each layer
    tiled at its repeat and rotation and coloured as the terrain shader
    colours it (tinted). Returns (the texture's content path, its extent)."""
    import cv2
    import numpy as np
    from PIL import Image
    from dota2tools import lightbake
    o = ctx.options
    P = np.asarray(P, np.float32)
    T = np.asarray(tris, np.int64)
    V = P[T]
    x0, y0, x1, y1 = float(V[..., 0].min()), float(V[..., 1].min()), float(V[..., 0].max()), float(V[..., 1].max())
    res = float(o.get("height_res", 8.0))
    Z, _ = lightbake.height_field([(side, P, T)], (x0, x1, y0, y1), res)
    inside = Z > -1e8
    Z = lightbake._fill(Z)

    def ramp(v, lo, hi):
        v = np.clip((v - lo) / (hi - lo), 0, 1)
        return v * v * (3 - 2 * v)
    # Each level of ground keeps the floor's layer; the steps between levels
    # and the very high ground (the walls at the map's border) take the rock.
    # A step: the ground within step_window units spans more than a mound
    # does (Radiant's under its trees stay grass) -- a level is 128 high.
    floor = np.percentile(Z[inside], 10) if inside.any() else 0.0
    gy, gx = np.gradient(Z, res)
    win = np.ones((2 * max(1, round(float(o.get("step_window", 72.0)) / res / 2)) + 1,) * 2, np.uint8)
    span = cv2.dilate(Z.astype(np.float32), win) - cv2.erode(Z.astype(np.float32), win)
    step = ramp(span, float(o.get("step_from", 80.0)), float(o.get("step_to", 128.0)))
    # Dire's corner rises in shallow shapes a span leaves out: its slopes
    # too, their rock reaching out a little (Radiant's mounds would turn).
    edges = float(o.get(f"slopes_{side}", SLOPES.get(side, 0.0)))
    if edges > 0:
        sl = ramp(np.hypot(gx, gy), float(o.get("slope_from", 0.4)), float(o.get("slope_to", 1.0)))
        wide = int(round(float(o.get("slope_width", 24.0)) / res))
        if wide > 0:
            sl = cv2.dilate(sl.astype(np.float32), np.ones((2 * wide + 1,) * 2, np.uint8))
        step = np.maximum(step, edges * sl)
    r = np.maximum(step, ramp(Z - floor, float(o.get("high_from", 512.0)), float(o.get("high_to", 768.0))))
    r = cv2.GaussianBlur(r.astype(np.float32), (0, 0), max(float(o.get("rock_soften", 24.0)) / res, 0.5))
    tex_res = float(o.get("texture_res", 2.0))

    def px(span):
        # Power-of-two sides (the tools want them).
        return min(8192, 2 ** max(4, math.ceil(math.log2(max(span / tex_res, 1)))))
    W, H = px(x1 - x0), px(y1 - y0)
    r = cv2.resize(r, (W, H), interpolation=cv2.INTER_LINEAR)[:, :, None]
    X, Y = np.meshgrid(np.linspace(x0, x1, W, dtype=np.float32), np.linspace(y1, y0, H, dtype=np.float32))
    Zt = cv2.resize(Z.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR)
    # Triplanar: level ground takes the texture from above, slopes from the
    # side they face, so it follows the relief (squeezed up a rise, seen from
    # above) as the map's own surface-following UVs do.
    nx, ny = cv2.resize(-gx.astype(np.float32), (W, H)), cv2.resize(-gy.astype(np.float32), (W, H))
    tw_ = np.stack([np.abs(nx), np.abs(ny), np.ones_like(nx)], -1) ** 4
    tw_ /= tw_.sum(-1, keepdims=True)
    planes = ((X, Y), (Y, Zt), (X, Zt))

    def layer(mat, k):
        m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, mat)
        img = tinted(ctx, m, k)(mapinfo.texture_image(ctx.tools, ctx.addon, ctx.map, m["textures"][f"g_tColor{k}"]))
        a = np.ascontiguousarray(np.asarray(img.convert("RGB"), np.float32) / 255)
        # The terrain's UVs are world units / 128, divided by the layer's scale.
        rep = float(o.get("uv_units", 128.0)) * float(m["floats"].get(f"g_flTexCoordScale{k}", 1.0))
        t = math.radians(float(m["floats"].get(f"g_flTexCoordRotate{k}", 0.0)))
        th, tw = a.shape[:2]
        out = 0
        # From above by the normal's z, from the sides by its x and y.
        for (A, B), wgt in zip(planes, (tw_[..., 2], tw_[..., 0], tw_[..., 1])):
            u = (A * math.cos(t) - B * math.sin(t)) / rep
            v = -(A * math.sin(t) + B * math.cos(t)) / rep
            out = out + wgt[..., None] * cv2.remap(a, ((u % 1) * tw).astype(np.float32), ((v % 1) * th).astype(np.float32),
                                                   cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        return out
    out = layer(*flat)
    if tuple(steep) != tuple(flat):
        out += (layer(*steep) - out) * r
    rel = f"materials/dota2tools/corner_{side}_ground.png"
    p = ctx.content / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)).save(p)
    return rel, (x0, x1, y0, y1)


def dmx_text(tris, P, N, lift, uv_units=256.0, uv=None):
    """The triangles as a model DMX (KeyValues2; Source's axes, z up); UVs
    world x, y over uv_units, or uv(x, y) -> (u, v)."""
    import uuid
    used = sorted({i for t in tris for i in t})
    index = {i: k for k, i in enumerate(used)}

    def arr(kind, items):
        return f'"{kind}"\n[\n' + ",\n".join(items) + "\n]"
    pos = arr("vector3_array", [f'"{P[i][0]:.3f} {P[i][1]:.3f} {P[i][2] + lift:.3f}"' for i in used])
    nrm = arr("vector3_array", ['"{:.5f} {:.5f} {:.5f}"'.format(*N[i]) for i in used])
    uvf = uv or (lambda x, y: (x / uv_units, y / uv_units))
    uv = arr("vector2_array", ['"{:.6f} {:.6f}"'.format(*uvf(P[i][0], P[i][1])) for i in used])
    idx = arr("int_array", [f'"{k}"' for k in range(len(used))])
    faces = arr("int_array", [f'"{index[i] if i >= 0 else -1}"' for t in tris for i in (*t, -1)])
    ids = {k: uuid.uuid4() for k in ("root", "model", "mt", "dag", "dt", "mesh", "vd", "fs", "mat", "axis")}
    return f"""<!-- dmx encoding keyvalues2 1 format model 22 -->
"DmElement"
{{
"id" "elementid" "{ids['root']}"
"name" "string" "root"
"skeleton" "element" "{ids['model']}"
"model" "element" "{ids['model']}"
}}

"DmeModel"
{{
"id" "elementid" "{ids['model']}"
"name" "string" "corner"
"transform" "DmeTransform"
{{
"id" "elementid" "{ids['mt']}"
"position" "vector3" "0 0 0"
"orientation" "quaternion" "0 0 0 1"
}}
"shape" "element" ""
"visible" "bool" "1"
"children" "element_array"
[
"DmeDag"
{{
"id" "elementid" "{ids['dag']}"
"name" "string" "corner"
"transform" "DmeTransform"
{{
"id" "elementid" "{ids['dt']}"
"position" "vector3" "0 0 0"
"orientation" "quaternion" "0 0 0 1"
}}
"shape" "DmeMesh"
{{
"id" "elementid" "{ids['mesh']}"
"name" "string" "corner"
"visible" "bool" "1"
"bindState" "DmeVertexData"
{{
"id" "elementid" "{ids['vd']}"
"name" "string" "bind"
"vertexFormat" "string_array"
[
"position$0",
"normal$0",
"texcoord$0"
]
"jointCount" "int" "0"
"flipVCoordinates" "bool" "0"
"position$0" {pos}
"position$0Indices" {idx}
"normal$0" {nrm}
"normal$0Indices" {idx}
"texcoord$0" {uv}
"texcoord$0Indices" {idx}
}}
"currentState" "element" "{ids['vd']}"
"baseStates" "element_array"
[
"element" "{ids['vd']}"
]
"deltaStates" "element_array"
[
]
"faceSets" "element_array"
[
"DmeFaceSet"
{{
"id" "elementid" "{ids['fs']}"
"name" "string" "ground"
"faces" {faces}
"material" "DmeMaterial"
{{
"id" "elementid" "{ids['mat']}"
"name" "string" "ground"
"mtlName" "string" "ground"
}}
}}
]
}}
"visible" "bool" "1"
"children" "element_array"
[
]
}}
]
"jointList" "element_array"
[
]
"baseStates" "element_array"
[
]
"axisSystem" "DmeAxisSystem"
{{
"id" "elementid" "{ids['axis']}"
"name" "string" "axisSystem"
"upAxis" "int" "3"
"forwardParity" "int" "1"
"coordSys" "int" "0"
}}
}}
"""


VMDL = """<!-- kv3 encoding:text:version{{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d}} format:modeldoc31:version{{165d48dd-8adc-4f1d-abf7-4992e04ad577}} -->
{{
	rootNode =
	{{
		_class = "RootNode"
		children =
		[
			{{
				_class = "MaterialGroupList"
				children =
				[
					{{
						_class = "DefaultMaterialGroup"
						remaps =
						[
							{{
								from = "ground.vmat"
								to = "{material}"
							}},
						]
						use_global_default = true
						global_default_material = "{material}"
					}},
				]
			}},
			{{
				_class = "RenderMeshList"
				children =
				[
					{{
						_class = "RenderMeshFile"
						name = "{name}"
						filename = "{mesh}"
						import_scale = 1.0
						import_filter =
						{{
							exclude_by_default = false
							exception_list = [  ]
						}}
					}},
				]
			}},
		]
	}}
}}
"""


def run(ctx):
    found = mapinfo.black_corners(ctx.tools, ctx.addon, ctx.map)
    if not found:
        ctx.log(f"corners: {ctx.map} has no black corner mesh")
        return
    _, sides, P, N = found
    models = mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)
    layers = mapinfo.terrain_layers(ctx.tools, ctx.addon, ctx.map, models)
    lift = float(ctx.options.get("lift", 2.0))
    unlit = bool(ctx.options.get("unlit", False))
    for side, tris in sides.items():
        if not tris or side not in layers:
            continue
        blend = layers[side]["material"]
        if "lava" in blend.split("/")[-1]:
            blend = "materials/blends/mod_dire_000.vmat"
        layer = ctx.options.get(f"layer_{side}", ctx.options.get("layer", LAYER.get(side)))
        # Another blend's layer instead: "materials/blends/x.vmat:2".
        src = str(ctx.options.get(f"source_{side}", SOURCE.get(side, ""))).strip()
        shade = [float(k) for k in ctx.options.get(f"shade_{side}", SHADE.get(side, [1, 1, 1]))]
        vmat, name = f"materials/dota2tools/corner_{side}.vmat", f"corner_{side}"
        uv, repeat = None, 256.0
        if layer == "edge" and not src:
            m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, blend)
            main = (blend, 1 if "g_tColor1" in m["textures"] else 0)
            edge = mapinfo.edge_layers(ctx.tools, ctx.addon, ctx.map, side, models)

            def given(key):
                spec = str(ctx.options.get(f"{key}_{side}", "")).strip()
                if spec:
                    mat, _, k = spec.rpartition(":")
                    return mat, int(k)
            # Radiant's floor its main ground (the grass, or what a variant
            # map has instead); Dire's what its edge next to the corner has.
            flat = given("floor") or (edge.get("flat", main) if side == "dire" else main)
            steep = given("rock") or edge.get("steep", flat)
            tex, (x0, x1, y0, y1) = bake(ctx, side, tris, P, flat, steep)
            mat = {"shader": "global_lit_simple.vfx", "ints": {"F_FULLBRIGHT": 1} if unlit else {}, "floats": {},
                   "vectors": {"g_vColorTint": shade + [0.0], "g_vTexCoordScale": [1.0, 1.0, 0.0, 0.0],
                               "g_vTexCoordOffset": [0.0, 0.0, 0.0, 0.0]}}
            ctx.write(vmat, mapinfo.source_vmat(mat, {"TextureColor": tex}, comment=(
                f"dota2-tools map renderer: the {side} corner's ground, {flat[0]}'s layer {flat[1]} "
                f"into {steep[0]}'s layer {steep[1]} up the slopes")))
            ctx.compile([vmat])
            uv = lambda x, y, x0=x0, x1=x1, y0=y0, y1=y1: ((x - x0) / (x1 - x0), (y1 - y) / (y1 - y0))
            what = f"{flat[0].split('/')[-1]} layer {flat[1]}, slopes {steep[0].split('/')[-1]} layer {steep[1]}"
        else:
            if layer == "edge":
                layer = None
            m = None
            if src:
                other, _, k = src.partition(":")
                m = mapinfo.material(ctx.tools, ctx.addon, ctx.map, other)
                if m:
                    blend, layer = other, (int(k) if k else None)
                else:
                    ctx.log(f"corners: {other} is not in the map or the game; {side} on its own ground")
                    shade = [float(k) for k in ctx.options.get(f"shade_{side}", OWN_SHADE.get(side, [1, 1, 1]))]
            m = m or mapinfo.material(ctx.tools, ctx.addon, ctx.map, blend)
            if layer is None:
                layer = 1 if "g_tColor1" in m["textures"] else 0
            tint = [t * k for t, k in zip(tint_of(ctx.options.get(f"tint_{side}", "layer"), m, int(layer)), shade)]
            ctx.override_material(blend, {}, f"dota2-tools map renderer: the {side} corner's ground, "
                                  f"{blend}'s layer {layer}", out=vmat,
                                  source=simple_material(m, int(layer), tint, unlit))
            repeat = float(ctx.options.get("uv_units", 256.0)) * float(m["floats"].get(f"g_flTexCoordScale{layer}", 1.0))
            what = f"{blend.split('/')[-1]} layer {layer}"
        mesh = ctx.write(f"{FOLDER}/{name}.dmx", dmx_text(tris, P, N, lift, repeat, uv=uv))
        vmdl = ctx.write(f"{FOLDER}/{name}.vmdl", VMDL.format(material=vmat, name=name, mesh=mesh))
        ctx.compile([vmdl])
        ctx.add_model(vmdl, name)
        ctx.log(f"corners: {side} on {what}, {len(tris)} triangles")
