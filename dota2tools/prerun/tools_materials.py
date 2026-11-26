"""Pre-run script: no tools materials or stray water-edge foam in the picture.

A map's world can keep meshes whose material is one of the tools' own
(materials/tools/...): the game doesn't draw them, the tools do. Under
dota's bridges, materials/tools/dota_nav_walkable.vmat -- the walkable
navigation drawn as a green grid -- would show in the render. In the render
addon each tools material the world uses is the same material at opacity
0; the map and its addon keep theirs.

Dota's water_edges scene models draw cyan foam arcs over the Radiant mid
ramp in SFM. Their base-game resources take precedence over same-path addon
material overrides, so the render-only map package points to transparent
clones instead. The river's ordinary water surface is untouched.
"""
import re
from pathlib import Path

from dota2tools import core, mapinfo
from dota2tools.prerun import corners

HIDE = {"F_TRANSLUCENT": 1, "F_ADDITIVE_BLEND": 0, "g_flOpacityScale": 0.0}
HIDDEN = "materials/dota2tools/water_edge_hidden.vmat"


def transparent(im):
    from PIL import Image
    return Image.new("RGBA", im.size, (0, 0, 0, 0))


def hide_water_edges(ctx):
    """Redirect the map's water-edge model references to transparent clones."""
    import numpy as np
    pkg = mapinfo.map_package(ctx.tools, ctx.addon, ctx.map)
    vmap = f"maps/{ctx.map}.vmap_c"
    used = {}
    for rel in core.vpk_list(pkg):
        if rel != vmap and not rel.endswith(".vwnod_c"):
            continue
        data = core.vpk_read(pkg, rel) or b""
        found = sorted(set(re.findall(rb"models/effects/water_edges(?:_mod)?_\d+b?\.vmdl", data)))
        if found:
            used[rel] = found
    names = sorted({name for found in used.values() for name in found})
    replacements = []
    hidden = None
    for old_b in names:
        old = old_b.decode()
        decoded = mapinfo.decompile_textured(mapinfo.read(ctx.tools, ctx.addon, ctx.map, old + "_c"))
        if not decoded:
            raise SystemExit(f"tools materials: could not decompile {old}")
        positions, normals, uvs, triangles = [], [], [], []
        offset = 0
        for P, N, UV, sets in decoded:
            positions.append(P)
            normals.append(N)
            uvs.append(UV)
            for mat, tris in sets:
                if tris:
                    triangles.append(np.asarray(tris, np.int64) + offset)
                    if hidden is None:
                        hidden = ctx.override_material(
                            mat, HIDE, "dota2-tools map renderer: transparent water-edge clone",
                            edits={"TextureColor": transparent}, out=HIDDEN)
            offset += len(P)
        new = old.replace("/water_edges", "/empty_edges", 1)
        stem = Path(new).stem
        mesh = f"models/effects/{stem}.vmd.dmx"
        ctx.write(mesh, corners.dmx_text(np.concatenate(triangles), np.concatenate(positions),
                                         np.concatenate(normals), 0, uv_values=np.concatenate(uvs)))
        ctx.write(new, corners.VMDL.format(material=hidden, name=stem, mesh=mesh))
        ctx.compile([new])
        replacements.append((old_b, new.encode()))
    for rel, old_names in used.items():
        ctx.edit_map(rel, [(old, old.replace(b"/water_edges", b"/empty_edges", 1)) for old in old_names])
    return len(replacements)


def run(ctx):
    mats = mapinfo.model_materials(mapinfo.world_models(ctx.tools, ctx.addon, ctx.map))
    used = sorted(m for m in mats if m.startswith("materials/tools/"))
    for vmat in used:
        ctx.override_material(vmat, HIDE,
                              "dota2-tools map renderer: a tools material, not drawn")
    water_edges = hide_water_edges(ctx)
    parts = []
    if used:
        parts.append(", ".join(m.split("/")[-1] for m in used) + " hidden")
    parts.append(f"water_edges replaced by transparent clones ({water_edges} models)")
    ctx.log("tools materials: " + "; ".join(parts))
