"""Pre-run script: the trees along the map's edge, as oaks.

Beyond the playable edge the trees' leaves use a material of their own,
materials/models/props_tree/tree_oak_leaves_blank.vmat: a plain white leaf,
and the map tints those trees black (per tree, through the material's tint
mask) -- silhouettes. In the render addon that material is replaced by the
map's own oak leaves (tree_oak_leaves_00: colour, normal and specular maps,
wind) as they are, the tint mask emptied so the map's black no longer
reaches them; lit like every tree, a little darker than the map's own
(render.edge_trees.shade, default 0.7: they stand in the shade beyond the
edge, as the interactive map's picture has them). render.edge_trees.tint ("#rrggbb", default
empty) brings the leaves' average colour to that colour instead;
render.edge_trees.unlit draws them flat.
"""
from dota2tools import mapinfo

LEAVES = "materials/models/props_tree/tree_oak_leaves_blank.vmat"
OAK = "materials/models/props_tree/tree_oak_leaves_00.vmat"


def rgb(text):
    text = text.strip()
    if text.startswith("#"):
        return [int(text[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [float(v) for v in text.split()[:3]]


def run(ctx):
    tint = str(ctx.options.get("tint", "")).strip()
    if LEAVES not in mapinfo.model_materials(mapinfo.world_models(ctx.tools, ctx.addon, ctx.map)):
        ctx.log(f"edge trees: {ctx.map} has none")
        return
    c = rgb(tint) if tint else None

    def colour(im):
        """The leaves' average (where they are opaque) brought to the tint."""
        from PIL import Image, ImageStat
        im = im.convert("RGBA")
        r, g, b, a = im.split()
        mask = a.point(lambda v: 255 if v > 127 else 0)
        mean = ImageStat.Stat(im.convert("RGB"), mask).mean
        r, g, b = (ch.point(lambda v, k=k: min(255, round(v * k)))
                   for ch, k in zip((r, g, b), (t * 255 / max(m, 1) for t, m in zip(c, mean))))
        return Image.merge("RGBA", (r, g, b, a))
    shade = float(ctx.options.get("shade", 0.7))

    def darker(im):
        """The leaves darker by `shade`, their alpha kept."""
        from PIL import Image
        r, g, b, a = im.convert("RGBA").split()
        r, g, b = (ch.point(lambda v: round(v * shade)) for ch in (r, g, b))
        return Image.merge("RGBA", (r, g, b, a))
    edits = {"TextureTintMask": lambda im: im.point(lambda v: 0)}
    if c:
        edits["TextureColor"] = colour
    elif shade != 1:
        edits["TextureColor"] = darker
    changes = {"g_vColorTint": [1.0, 1.0, 1.0, 0.0]}
    if ctx.options.get("unlit", False):
        changes["F_FULLBRIGHT"] = 1
    oak = mapinfo.material(ctx.tools, ctx.addon, ctx.map, OAK)
    ctx.override_material(LEAVES, changes, f"dota2-tools map renderer: the edge trees' leaves, {OAK}'s, "
                          f"the map's tint masked out", edits, source=oak)
    ctx.log("edge trees: oak leaves" + (", #%02x%02x%02x" % tuple(round(x * 255) for x in c) if c else ""))
