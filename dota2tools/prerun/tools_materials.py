"""Pre-run script: no tools materials in the picture.

A map's world can keep meshes whose material is one of the tools' own
(materials/tools/...): the game doesn't draw them, the tools do. Under
dota's bridges, materials/tools/dota_nav_walkable.vmat -- the walkable
navigation drawn as a green grid -- would show in the render. In the render
addon each tools material the world uses is the same material at opacity
0; the map and its addon keep theirs.
"""
from dota2tools import mapinfo


def run(ctx):
    used = sorted(m for m in mapinfo.model_materials(mapinfo.world_models(ctx.tools, ctx.addon, ctx.map))
                  if m.startswith("materials/tools/"))
    for vmat in used:
        ctx.override_material(vmat, {"F_TRANSLUCENT": 1, "g_flOpacityScale": 0.0},
                              "dota2-tools map renderer: a tools material, not drawn")
    ctx.log("tools materials: " + (", ".join(m.split("/")[-1] for m in used) + " hidden" if used else "none in this map"))
