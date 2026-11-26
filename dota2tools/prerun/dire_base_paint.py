"""Pre-run script: remove the Dire base's paint seam, not its tiles.

West of x 5888 dota draws the base from dire_base; east of it defaultLayer
uses the same tiles but darker paint. In the render-only map rebuilt from its
source, copy dire_base's blendColor and blendOpacity to defaultLayer at the
same sample positions in the two-cell rectangle north of the stairs. The west
edge already borders dire_base and receives the donor values in full; the
other three edges feather into defaultLayer's original paint. Tiles, props,
orientations, material sets, paths, transitions and heights stay unchanged.

Options (settings.json, render.dire_base_paint): "from" (donor layer, default
"dire_base"), "to" (default "defaultLayer"), "rect" ([x0, x1, y0, y1],
default [5888, 6400, 5888, 6528] on dota, none elsewhere) and "feather"
(world units at the north, east and south edges, default 96).
"""
import re

from dota2tools import maprender

RECT = (5888, 6400, 5888, 6528)
PAINT = ("blendColor", "blendOpacity")


def _layer(text, name):
    m = re.search(r'"CDmeDotaTileGrid"\s*\{\s*"id" "elementid" "[^"]+"\s*"name" "string" "%s"'
                  % re.escape(name), text)
    if not m:
        raise SystemExit(f"dire base paint: no tile grid layer {name}")
    return m.start()


def _array(text, layer_at, name):
    i = text.index(f'"{name}" "', layer_at)
    a = text.index("[", i) + 1
    return a, text.index("]", a)


def _values(text, span):
    return re.findall(r'"([^"]*)"', text[span[0]:span[1]])


def _put(text, span, values):
    body = text[span[0]:span[1]]
    items = list(re.finditer(r'"([^"]*)"', body))
    if len(items) != len(values):
        raise ValueError("dire base paint: paint array size changed")
    out, last = [], 0
    for it, value in zip(items, values):
        out.append(body[last:it.start(1)])
        out.append(value)
        last = it.end(1)
    out.append(body[last:])
    return text[:span[0]] + "".join(out) + text[span[1]:]


def copy_paint(text, source, target, rect, feather):
    """Copy two paint arrays inside rect, feathered except at the donor's west
    boundary. Return (new map text, {array: changed sample count})."""
    s_at, d_at = _layer(text, source), _layer(text, target)
    width = int(re.search(r'"gridWidth" "int" "(\d+)"', text[d_at:]).group(1))
    height = int(re.search(r'"gridHeight" "int" "(\d+)"', text[d_at:]).group(1))
    sw, sh = width * 8 + 1, height * 8 + 1
    ox, oy = -width * 128, -height * 128
    x0, x1, y0, y1 = (float(v) for v in rect)
    if not x0 < x1 or not y0 < y1:
        raise ValueError(f"dire base paint: invalid rectangle {rect}")
    feather = max(0.0, float(feather))
    edits, changed = [], {}
    for name in PAINT:
        sv = _values(text, _array(text, s_at, name))
        dspan = _array(text, d_at, name)
        dv = _values(text, dspan)
        if len(sv) != sw * sh or len(dv) != sw * sh:
            raise ValueError(f"dire base paint: {name} is not a {sw} x {sh} paint grid")
        count = 0
        for row in range(sh):
            y = oy + row * 32
            if not y0 <= y <= y1:
                continue
            for col in range(sw):
                x = ox + col * 32
                if not x0 <= x <= x1:
                    continue
                i = row * sw + col
                distance = min(x1 - x, y - y0, y1 - y)
                weight = 1.0 if feather == 0 else max(0.0, min(1.0, distance / feather))
                if weight == 0:
                    continue
                if weight == 1:
                    value = sv[i]
                else:
                    old = tuple(int(v) for v in dv[i].split())
                    donor = tuple(int(v) for v in sv[i].split())
                    value = " ".join(str(round(a + (b - a) * weight)) for a, b in zip(old, donor))
                if value != dv[i]:
                    dv[i] = value
                    count += 1
        changed[name] = count
        edits.append((dspan, dv))
    for span, values in sorted(edits, key=lambda e: -e[0][0]):
        text = _put(text, span, values)
    return text, changed


def run(ctx):
    rect = ctx.options.get("rect", RECT if ctx.map == "dota" else None)
    if rect is None:
        return
    if not maprender.map_source(ctx.tools, ctx.addon, ctx.map):
        ctx.log(f"dire base paint: {ctx.map} has no source to rebuild")
        return
    source = ctx.options.get("from", "dire_base")
    target = ctx.options.get("to", "defaultLayer")
    feather = float(ctx.options.get("feather", 96))
    bounds = ",".join(f"{float(v):g}" for v in rect)

    def edit(text):
        text, changed = copy_paint(text, source, target, rect, feather)
        ctx.log(f"dire base paint: {source} -> {target} at {bounds}; "
                + ", ".join(f"{name} {count} samples" for name, count in changed.items()))
        return text

    ctx.edit_source(f"dire base paint {source}->{target} {bounds} feather {feather:g}", edit)
