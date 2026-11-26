"""Pre-run script: the Dire base's tiles from one grid layer.

dota's tile grid draws the Dire base from two layers: its western part (the
fountain's ground and the base west of it) from the dire_base layer, the
rest, the stairs and the area round the fountain included, from
defaultLayer, whose own tiles there are hidden. Where the two meet, a
straight seam runs north-south left of the fountain (x 5888) in the
renders. Under the dire_base cells defaultLayer holds the same tile set,
variations and corner heights; only a few orientations and material sets
differ. Here, in a copy of the map rebuilt from its source, those cells are
drawn by defaultLayer, with dire_base's orientations and material sets, and
hidden in dire_base: the whole base comes from one layer.

Options (settings.json, render.dire_base_tiles): "from" (the layer whose
cells move, default "dire_base"), "to" (default "defaultLayer"), "copy"
(cell arrays copied along, default cellsOrientation and cellsMaterialSet).
"""
import re

from dota2tools import maprender


def _array(text, layer_at, name):
    """(start, end) of a layer's int/bool array body, from the layer's name."""
    i = text.index(f'"{name}" "', layer_at)
    a = text.index("[", i) + 1
    return a, text.index("]", a)


def _values(text, span):
    return re.findall(r'"([^"]*)"', text[span[0]:span[1]])


def _put(text, span, values):
    body = text[span[0]:span[1]]
    items = list(re.finditer(r'"([^"]*)"', body))
    out, last = [], 0
    for it, v in zip(items, values):
        out.append(body[last:it.start(1)])
        out.append(v)
        last = it.end(1)
    out.append(body[last:])
    return text[:span[0]] + "".join(out) + text[span[1]:]


def move_cells(text, src, dst, copy):
    """The map's text with every cell drawn by layer `src` alone drawn by
    `dst` instead (arrays `copy` copied over); (text, cells moved)."""
    def at(layer):
        m = re.search(r'"CDmeDotaTileGrid"\s*\{\s*"id" "elementid" "[^"]+"\s*"name" "string" "%s"' % re.escape(layer),
                      text)
        if not m:
            raise SystemExit(f"dire base tiles: no tile grid layer {layer}")
        return m.start()

    s_at, d_at = at(src), at(dst)
    s_hidden = _values(text, _array(text, s_at, "cellsHidden"))
    d_hidden = _values(text, _array(text, d_at, "cellsHidden"))
    moved = [i for i, (a, b) in enumerate(zip(s_hidden, d_hidden)) if a == "0" and b == "1"]
    # Later edits first, so earlier spans stay valid.
    edits = []
    for name in copy:
        sv = _values(text, _array(text, s_at, name))
        dspan = _array(text, d_at, name)
        dv = _values(text, dspan)
        for i in moved:
            dv[i] = sv[i]
        edits.append((dspan, dv))
    d_span = _array(text, d_at, "cellsHidden")
    for i in moved:
        d_hidden[i] = "0"
    edits.append((d_span, d_hidden))
    s_span = _array(text, s_at, "cellsHidden")
    for i in moved:
        s_hidden[i] = "1"
    edits.append((s_span, s_hidden))
    for span, values in sorted(edits, key=lambda e: -e[0][0]):
        text = _put(text, span, values)
    return text, len(moved)


def run(ctx):
    if ctx.map != "dota" and "from" not in ctx.options:
        return
    if not maprender.map_source(ctx.tools, ctx.addon, ctx.map):
        ctx.log(f"dire base tiles: {ctx.map} has no source to rebuild")
        return
    src = ctx.options.get("from", "dire_base")
    dst = ctx.options.get("to", "defaultLayer")
    copy = ctx.options.get("copy", ["cellsOrientation", "cellsMaterialSet"])
    count = []

    def edit(t):
        t, n = move_cells(t, src, dst, copy)
        count.append(n)
        return t

    ctx.edit_source(f"dire base tiles {src}->{dst}", edit)
    ctx.log(f"dire base tiles: {src}'s own cells drawn by {dst} in the rebuilt map")
