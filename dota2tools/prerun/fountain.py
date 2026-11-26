"""Pre-run script: no lotus-pool ambient water at mid.

npc_dota_lotus_pool spawns particles/world_fountain_of_life/fol_ambient.vpcf.
Its skinned pool surfaces have no valid unit snapshot in SFM, so the cyan
circle and basin arcs draw at world origin. Base-package resources win over
same-path addon overrides, so the render-only map's lotus entities point to
an empty particle under a distinct, same-length name instead.
"""
from dota2tools import core, mapinfo
from dota2tools.prerun import flies

ROOT = "particles/world_fountain_of_life/fol_ambient.vpcf"
HIDE = [
    ROOT,
    "particles/world_fountain_of_life/fol_ambient_pool.vpcf",
    "particles/world_fountain_of_life/fol_ambient_pool_1_fx.vpcf",
    "particles/world_fountain_of_life/fol_ambient_pool_2_fx.vpcf",
    "particles/world_fountain_of_life/fol_ambient_pool_3_fx.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg_depth.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg_edge.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg_edge_inner.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg_rings.vpcf",
    "particles/world_fountain_of_life/fol_ambient_lrg_splash.vpcf",
    "particles/world_fountain_of_life/fol_ambient_mid.vpcf",
    "particles/world_fountain_of_life/fol_ambient_mist.vpcf",
    "particles/world_fountain_of_life/fol_ambient_sml.vpcf",
    "particles/world_fountain_of_life/fol_ambient_sml_wake.vpcf",
    "particles/world_fountain_of_life/fol_ambient_spurt.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_mid_1.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_mid_2.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_mid_3.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_top_1.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_top_2.vpcf",
    "particles/world_fountain_of_life/fol_ambient_flow_top_3.vpcf",
]


def empty_name(name):
    return name.replace("/fol_", "/nil_", 1)


def run(ctx):
    pkg = mapinfo.map_package(ctx.tools, ctx.addon, ctx.map)
    vmap = f"maps/{ctx.map}.vmap_c"
    vmap_data = core.vpk_read(pkg, vmap) or b""
    if ROOT.encode() not in vmap_data:
        ctx.log(f"fountain: {ctx.map} has no lotus ambient")
        return
    for name in HIDE:
        alias = empty_name(name)
        ctx.write(alias, flies.EMPTY)
        ctx.compile([alias])
    rel = f"maps/{ctx.map}/entities/default_ents.vents_c"
    if core.vpk_read(pkg, rel) is None:
        raise SystemExit(f"fountain: {rel} is missing from {pkg}")
    old, new = ROOT.encode(), empty_name(ROOT).encode()
    ctx.rename_in_map(rel, old, new)
    ctx.edit_map(vmap, [(old, new)])
    ctx.log("fountain: lotus ambient redirected to empty render-only particles")
