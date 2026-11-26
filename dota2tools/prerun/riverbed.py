"""Pre-run script: the stray Radiant riverbed tile at mid.

One tile of dota's grid under the river by mid (cell 35, 35: x -512..-256,
y -512..-256) is on the Radiant tile set while the riverbed around it is on
the Dire one, so it draws with the Radiant riverbed material, a pale green
rectangle. Switching that cell's tile set in Hammer fixes it but also turns
its stairs and banks Dire.

Here only its riverbed changes: the map is rebuilt from its source (as for
custom lighting) with a render-only clone of its Radiant tile set, whose
tile sets each gain a material set mapping the Radiant riverbed material to
the Dire tile set's and everything else to itself, and that cell alone uses
it. The map and its tile sets stay as they are.

Options (settings.json, render.riverbed): "cells" ([[column, row], ...];
default [[35, 35]] on dota, none elsewhere) and "layer" (the tile grid
layer, default "defaultLayer").
"""
import re
import uuid
from pathlib import PurePosixPath

from dota2tools import maprender

CELLS = {"dota": [[35, 35]]}


def _uuids(block):
    return re.sub(r'"id" "elementid" "[0-9a-f-]+"', lambda m: f'"id" "elementid" "{uuid.uuid4()}"', block)


def _tile_sets(text):
    """Names of a tile set file's tile sets (CMapTileSet)."""
    return re.findall(r'\n"CMapTileSet"\s*\{\s*"id" "elementid" "[^"]+"\s*"name" "string" "([^"]*)"', text)


def _material_sets(text, name):
    """(start, end, [set blocks]) of tile set `name`'s materialSets array."""
    m = re.search(r'\n"CMapTileSet"\s*\{\s*"id" "elementid" "[^"]+"\s*"name" "string" "%s"' % re.escape(name), text)
    i = text.index('\t"materialSets" "element_array"', m.start())
    a = text.index("[", i) + 1
    e = text.index("\n\t]", a)
    body = text[a:e]
    starts = [s.start() for s in re.finditer(r'\n\t\t"CTileSetMaterialSet"', body)]
    return a, e, [body[s:t].rstrip().rstrip(",") for s, t in zip(starts, starts[1:] + [len(body)])]


def _names(block):
    m = re.search(r'"materialNames" "string_array"\s*\[(.*?)\]', block, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def _with_names(block, names, label):
    block = re.sub(r'("name" "string" )"[^"]*"', lambda m: f'{m.group(1)}"{label}"', block, count=1)
    arr = "\n".join(f'\t\t\t\t"{n}"' + ("," if k < len(names) - 1 else "") for k, n in enumerate(names))
    return re.sub(r'("materialNames" "string_array"\s*\[)(.*?)(\n\t\t\t\])',
                  lambda m: f"{m.group(1)}\n{arr}{m.group(3)}", block, count=1, flags=re.S)


def riverbed_material(text):
    """The riverbed material a tile set file's tiles use."""
    found = sorted(set(re.findall(r'"(materials/[\w/\-.]*riverbed[\w/\-.]*\.vmat)"', text)))
    return found[0] if found else None


def clone(text, old, new):
    """The tile set file with a material set at its next free index (the
    same in each tile set) mapping `old` to `new`: (text, index). A set's
    first list names the materials it remaps, in order: `old` is added to it
    where missing."""
    text = text.replace("\r\n", "\n")
    index = max(len(_material_sets(text, n)[2]) for n in _tile_sets(text))
    for name in _tile_sets(text):
        a, e, blocks = _material_sets(text, name)
        own = _names(blocks[0])
        if old not in own:
            own.append(old)
            blocks[0] = _with_names(blocks[0], own, "Original Materials")
        while len(blocks) < index:
            blocks.append(_uuids(blocks[0]))
        blocks.append(_with_names(_uuids(blocks[0]), [new if n == old else n for n in own],
                                  "dota2tools: riverbed swapped"))
        text = text[:a] + ",".join(blocks) + "\n" + text[e:]
    return text.replace("\n", "\r\n"), index


def set_cells(text, layer, cells, value):
    """The map's text with `cells` ([[column, row]]) of tile grid layer
    `layer` on material set `value`."""
    m = re.search(r'"name" "string" "%s"' % re.escape(layer), text)
    if not m:
        raise SystemExit(f"riverbed: no tile grid layer {layer}")
    width = int(re.search(r'"gridWidth" "int" "(\d+)"', text[m.start():]).group(1))
    a = text.index("[", text.index('"cellsMaterialSet" "int_array"', m.start())) + 1
    e = text.index("]", a)
    body = text[a:e]
    for col, row in cells:
        it = list(re.finditer(r'"(-?\d+)"', body))[row * width + col]
        body = body[:it.start(1)] + str(value) + body[it.end(1):]
    return text[:a] + body + text[e:]


def run(ctx):
    cells = ctx.options.get("cells", CELLS.get(ctx.map, []))
    if not cells:
        return
    src = maprender.map_source(ctx.tools, ctx.addon, ctx.map)
    if not src:
        ctx.log(f"riverbed: {ctx.map} has no source to rebuild")
        return
    text = maprender.vmap_text(ctx.tools, src)
    tile_sets = {}
    for k, p in re.findall(r'"targetMapPath(\d?)" "string" "(maps/tilesets/[^"]+\.vmap)"', text):
        tile_sets.setdefault(int(k or 0), p)
    if 0 not in tile_sets or 1 not in tile_sets:
        ctx.log("riverbed: no Radiant and Dire tile sets in the map's tile grid")
        return

    def source(rel):
        for base in ((ctx.tools.root / "content" / "dota_addons" / ctx.addon) if ctx.addon else None,
                     ctx.tools.root / "content" / "dota"):
            if base is not None and (base / rel).exists():
                return maprender.vmap_text(ctx.tools, base / rel)
        raise SystemExit(f"riverbed: tile set {rel} not found")

    radiant, dire = source(tile_sets[0]), source(tile_sets[1])
    old, new = riverbed_material(radiant), riverbed_material(dire)
    if not old or not new:
        ctx.log("riverbed: no riverbed materials in the tile sets")
        return
    cloned, index = clone(radiant, old, new)
    path = PurePosixPath(tile_sets[0])
    rel = str(path.with_name(path.stem + "_d2t.vmap"))
    layer = ctx.options.get("layer", "defaultLayer")

    def edit(t):
        t = t.replace(f'"string" "{tile_sets[0]}"', f'"string" "{rel}"')
        return set_cells(t, layer, cells, index)

    ctx.edit_source(f"riverbed {cells} {old.split('/')[-1]}->{new.split('/')[-1]}", edit, [(rel, cloned)])
    ctx.log(f"riverbed: cells {cells} of {layer} on {rel}'s material set {index} ({old} -> {new})")
