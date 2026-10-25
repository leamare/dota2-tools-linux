"""What a compiled map holds, read from its package (maps/<map>.vpk): its
bounds, its entities, its terrain materials, the mesh that draws its black
corners; and compiled materials, read back into source form.

Compiled resources (entities, materials, models) are read with
source2viewer-cli (ValveResourceFormat's command line), which prints them as
KV3 text and decompiles models; compiled textures are decoded here
(core.vtex_image).
"""
import collections
import re
import shutil
import struct
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from . import core

S2V = "source2viewer-cli"


def map_package(tools, addon, map_name):
    """The map's package: the addon's game/maps, else the base game's."""
    if addon:
        return tools.root / "game" / "dota_addons" / addon / "maps" / f"{map_name}.vpk"
    return tools.root / "game" / "dota" / "maps" / f"{map_name}.vpk"


def read(tools, addon, map_name, rel):
    """A file of the map: its package, else the addon's loose files, else the
    base game's pak01."""
    pkg = map_package(tools, addon, map_name)
    if pkg.exists():
        d = core.vpk_read(pkg, rel)
        if d is not None:
            return d
    if addon:
        p = tools.root / "game" / "dota_addons" / addon / rel
        if p.exists():
            return p.read_bytes()
    return core.vpk_read(tools.root / "game" / "dota" / "pak01_dir.vpk", rel)


def s2v_text(data, suffix, block="DATA"):
    """A compiled resource's block as source2viewer-cli prints it (KV3 text)."""
    if not shutil.which(S2V):
        raise SystemExit("source2viewer-cli is needed to read compiled resources (ValveResourceFormat)")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / f"x{suffix}"
        f.write_bytes(data)
        out = subprocess.run([S2V, "-i", str(f), "-b", block], capture_output=True, text=True,
                             errors="replace", cwd=d).stdout
    i = out.find(f'--- Data for block "{block}" ---')
    return out[i:] if i >= 0 else ""


def decompile_model(data):
    """A compiled model's meshes (source2viewer-cli -d): [(positions, normals,
    [triangles per face set])], world space for map aggregates."""
    from . import dmxbin
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.vmdl_c"
        f.write_bytes(data)
        subprocess.run([S2V, "-i", str(f), "-o", str(Path(d) / "out"), "-d"], capture_output=True, cwd=d)
        meshes = []
        for dmx in sorted(Path(d).glob("*meshset*.dmx")) + sorted(Path(d).glob("out/**/*meshset*.dmx")):
            x = dmxbin.Dmx(dmx.read_bytes())
            vd = [e for e in x.elements if e.type == "DmeVertexData"][0]
            pos = vd.attrs["position$0"]
            pidx = vd.attrs["position$0Indices"]
            nrm = vd.attrs.get("normal$0", [])
            nidx = vd.attrs.get("normal$0Indices", [])
            P = [tuple(pos[i * 3:i * 3 + 3]) if isinstance(pos[0], float) else tuple(pos[i]) for i in pidx]
            N = [tuple(nrm[i * 3:i * 3 + 3]) if nrm and isinstance(nrm[0], float) else (tuple(nrm[i]) if nrm else (0, 0, 1))
                 for i in nidx] if nidx else [(0, 0, 1)] * len(P)
            sets = []
            for e in x.elements:
                if e.type == "DmeFaceSet":
                    faces = e.attrs["faces"]
                    tris, poly = [], []
                    for k in faces:
                        if k < 0:
                            tris += [(poly[0], poly[j], poly[j + 1]) for j in range(1, len(poly) - 1)]
                            poly = []
                        else:
                            poly.append(k)
                    sets.append(tris)
            meshes.append((P, N, sets))
    return meshes


def decompile_streams(data):
    """A compiled model's first mesh with every vertex stream, per face
    corner: ({stream name: array (n, k)}, triangles (m, 3)) -- texcoord$1..3
    of a map's blend meshes are its vertex paint (VertexPaintBlendParams,
    ...BlendParams1, VertexPaintTintColor)."""
    import numpy as np
    from . import dmxbin
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.vmdl_c"
        f.write_bytes(data)
        subprocess.run([S2V, "-i", str(f), "-o", str(Path(d) / "out"), "-d"], capture_output=True, cwd=d)
        found = sorted(Path(d).glob("*meshset*.dmx")) + sorted(Path(d).glob("out/**/*meshset*.dmx"))
        if not found:
            return {}, np.zeros((0, 3), np.int64)
        x = dmxbin.Dmx(found[0].read_bytes())
    vd = [e for e in x.elements if e.type == "DmeVertexData"][0]
    streams = {}
    for name in vd.attrs.get("vertexFormat", []):
        if name + "Indices" not in vd.attrs:
            continue
        vals = np.asarray(vd.attrs[name], np.float32)
        idx = np.asarray(vd.attrs[name + "Indices"], np.int64)
        streams[name] = vals.reshape(len(vals), -1)[idx] if vals.ndim > 1 else vals[idx][:, None]
    tris = []
    for e in x.elements:
        if e.type == "DmeFaceSet":
            poly = []
            for k in e.attrs["faces"]:
                if k < 0:
                    tris += [(poly[0], poly[j], poly[j + 1]) for j in range(1, len(poly) - 1)]
                    poly = []
                else:
                    poly.append(k)
    return streams, np.asarray(tris, np.int64)


# --- entities and bounds ----------------------------------------------------------

def _kv3_values(text):
    """The `values = { ... }` blocks of an entity lump's KV3 text, as dicts of
    strings (numbers and vectors kept as their text)."""
    out = []
    for m in re.finditer(r"\bvalues\s*=\s*\{(.*?)\n\t*\}", text, re.S):
        d = {}
        for k, v in re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", m.group(1), re.M):
            d[k] = v.strip().strip('"')
        out.append(d)
    return out


def _vec(s):
    return [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", s)]


@lru_cache(maxsize=16)
def entities(tools, addon, map_name):
    """Every entity of the map (all its entity lumps), as dicts."""
    pkg = map_package(tools, addon, map_name)
    if not pkg.exists():
        return []
    out = []
    for rel in core.vpk_list(pkg):
        if rel.endswith(".vents_c"):
            out += _kv3_values(s2v_text(core.vpk_read(pkg, rel), ".vents_c"))
    return out


def gridnav_rect(tools, addon, map_name):
    data = read(tools, addon, map_name, f"maps/{map_name}.gnv")
    if not data:
        return None
    _magic, cell, _a, _b, nx, ny, ox, oy = struct.unpack_from("<Ifffiiii", data, 0)
    return ox * cell, (ox + nx) * cell, oy * cell, (oy + ny) * cell


def bounds(tools, addon, map_name):
    """The map's rectangles (x0, x1, y0, y1) by source: minimap (its two
    dota_minimap_boundary entities), world (world_bounds), gridnav (its .gnv
    grid), overview (resource/overviews/<map>.txt). Missing ones are left out."""
    out = {}
    ents = entities(tools, addon, map_name)
    mm = [_vec(e["origin"]) for e in ents if e.get("classname") == "dota_minimap_boundary" and "origin" in e]
    if len(mm) >= 2:
        xs, ys = [p[0] for p in mm], [p[1] for p in mm]
        out["minimap"] = (min(xs), max(xs), min(ys), max(ys))
    wb = [e for e in ents if e.get("classname") == "world_bounds" and "min" in e and "max" in e]
    if wb:
        a, b = _vec(wb[0]["min"]), _vec(wb[0]["max"])
        out["world"] = (a[0], b[0], a[1], b[1])
    g = gridnav_rect(tools, addon, map_name)
    if g:
        out["gridnav"] = g
    from . import maprender
    ov = maprender.overview(tools, addon, map_name)
    if ov:
        out["overview"] = ov
    return out


# --- materials -----------------------------------------------------------------------

def material(tools, addon, map_name, vmat):
    """A compiled material (materials/....vmat) as {shader, ints, floats,
    vectors, textures}; None if the map and game have no such material."""
    data = read(tools, addon, map_name, vmat + "_c")
    if data is None:
        return None
    t = s2v_text(data, ".vmat_c")
    # Entries print as { m_name = ... m_value = ... }, in either order, the
    # strings plain or prefixed (resource:"...", resource_name:"...").
    q = r'(?:\w+:)?"([^"]+)"'
    m = {"shader": (re.search(r"m_shaderName = " + q, t) or [None, ""])[1],
         "ints": {}, "floats": {}, "vectors": {}, "textures": {}}
    for kind, key, field in (("ints", "m_intParams", "m_nValue"), ("floats", "m_floatParams", "m_flValue"),
                             ("vectors", "m_vectorParams", "m_value"), ("textures", "m_textureParams", "m_pValue")):
        sec = re.search(r"%s = \s*\[(.*?)\n\t\]" % key, t, re.S)
        if not sec:
            continue
        pairs = []
        for entry in re.findall(r"\{([^{}]*)\}", sec.group(1)):
            n = re.search(r"m_name = " + q, entry)
            v = re.search(r"%s = ([^\n]+)" % field, entry)
            if n and v:
                pairs.append((n.group(1), v.group(1)))
        for n, v in pairs:
            v = v.strip()
            if kind == "ints":
                m[kind][n] = int(float(v))
            elif kind == "floats":
                m[kind][n] = float(v)
            elif kind == "vectors":
                m[kind][n] = _vec(v)
            else:
                m[kind][n] = re.sub(r'^\w+:"|^"|"$', "", v)
    return m


def texture_image(tools, addon, map_name, vtex):
    """A compiled texture (materials/....vtex) decoded (needs Pillow)."""
    data = read(tools, addon, map_name, vtex + "_c")
    if data is None:
        raise SystemExit(f"{vtex} is not in the map or the game")
    return core.vtex_image(data)


def source_name(vectors, gname):
    """The source parameter a compiled texture came from: g_tColor ->
    TextureColor; g_tWindMask -> TextureWindMaskLow (the vector params name
    the sources); TextureX for one only the shader defaults."""
    base = "Texture" + gname[3:]
    if gname.startswith("g_tSpecular"):
        # Compiled from the reflectance map (bloom and self-illum packed in).
        base = "TextureReflectance" + gname[len("g_tSpecular"):]
    if base in vectors:
        return base
    for v in vectors:
        if v.startswith(base):
            return v
    # Not named among the vectors (a shader default, such as wind noise):
    # given explicitly all the same, or the compiler looks for the default's
    # source, which the tools don't ship.
    return base


def source_vmat(m, textures, changes=None, comment=""):
    """Source .vmat text for material dict `m` with its textures replaced by
    `textures` ({source param: content path}) and `changes` applied
    ({param: value}; vectors as lists)."""
    changes = changes or {}
    lines = [f'shader "{m["shader"]}"']
    for k, v in {**m["ints"], **{k: v for k, v in changes.items() if k.startswith("F_")}}.items():
        lines.append(f"{k} {int(v)}")
    for k, v in m["floats"].items():
        lines.append(f'{k} "{float(changes.get(k, v)):.6f}"')
    for k, v in m["vectors"].items():
        if k.startswith("Texture"):
            continue
        v = changes.get(k, v)
        lines.append(f'{k} "[' + " ".join(f"{x:.6f}" for x in v) + ']"')
    for k, path in textures.items():
        lines.append(f'{k} "{path}"')
    return (f"// {comment}\n\n" if comment else "") + "Layer0\n{\n" + "".join(f"\t{ln}\n" for ln in lines) + "}\n"


# --- the terrain and the corners --------------------------------------------------------

def world_models(tools, addon, map_name):
    """The map's world-node models: {path: bytes}."""
    pkg = map_package(tools, addon, map_name)
    if not pkg.exists():
        return {}
    return {rel: core.vpk_read(pkg, rel) for rel in core.vpk_list(pkg)
            if "/worldnodes/" in rel and rel.endswith(".vmdl_c")}


@lru_cache(maxsize=4)
def aggregate_tints(tools, addon, map_name):
    """{world model (maps/.../x.vmdl): (r, g, b) 0..255}: the tint the map's
    world nodes draw each aggregate model with (its meshes' most common
    m_vTintColor), which the materials' tint masks apply."""
    pkg = map_package(tools, addon, map_name)
    if not pkg.exists():
        return {}
    out = {}
    for rel in core.vpk_list(pkg):
        if not rel.endswith(".vwnod_c"):
            continue
        text = s2v_text(core.vpk_read(pkg, rel), ".vwnod_c")
        parts = text.split("m_renderableModel = ")
        for prev, part in zip(parts, parts[1:]):
            name = re.search(r'resource:"([^"]+)"', part)
            block = prev[prev.rfind("m_aggregateMeshes"):]
            tints = re.findall(r"m_vTintColor = \[ ([\d, ]+) \]", block)
            if name and tints:
                c = collections.Counter(tints).most_common(1)[0][0]
                out[name.group(1)] = tuple(int(v) for v in c.split(","))
    return out


def _aggregates(tools, addon, map_name):
    """The world nodes' aggregate scene objects: [(model path, [(draw call,
    3 x 4 transform or None)])] -- a merged aggregate's draw calls stand where
    they are, an instanced one's (a prop placed many times) each where its
    transform puts it."""
    import numpy as np
    pkg = map_package(tools, addon, map_name)
    out = []
    for rel in core.vpk_list(pkg):
        if not rel.endswith(".vwnod_c"):
            continue
        text = s2v_text(core.vpk_read(pkg, rel), ".vwnod_c")
        i = text.find("m_aggregateSceneObjects =")
        if i < 0:
            continue
        parts = text[i:].split("m_renderableModel = ")
        for prev, part in zip(parts, parts[1:]):
            name = re.search(r'resource:"([^"]+)"', part)
            block = prev[prev.rfind("m_aggregateMeshes"):]
            frags = re.findall(r"m_bHasTransform = (\w+).*?m_nDrawCallIndex = (\d+)", block, re.S)
            tf = block[block.find("m_fragmentTransforms"):]
            nums = [[float(v) for v in re.findall(r"-?[\d.]+(?:e-?\d+)?", m)]
                    for m in re.findall(r"\[([^\[\]]+)\]", tf)]
            mats = [np.array(v, np.float32).reshape(3, 4) for v in nums if len(v) == 12]
            k, placed = 0, []
            for has, dc in frags:
                if has == "true":
                    placed.append((int(dc), mats[k] if k < len(mats) else None))
                    k += 1
                else:
                    placed.append((int(dc), None))
            if name:
                out.append((name.group(1) + "_c", placed))
    return out


@lru_cache(maxsize=2)
def world_meshes(tools, addon, map_name):
    """The world's geometry, world space: [(model path, positions (n, 3),
    triangles (m, 3))], every aggregate's draw calls placed (instanced props
    by their transforms) -- for working out what stands where (heights,
    shadows). Decompiles every world-node model: slow, kept. (The world
    nodes' other scene objects -- overlays, effects -- are left out.)"""
    import os
    from concurrent.futures import ThreadPoolExecutor
    import numpy as np
    models = world_models(tools, addon, map_name)
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        decoded = dict(zip(models, pool.map(decompile_model, models.values())))
    out = []
    for rel, placed in _aggregates(tools, addon, map_name):
        meshes = decoded.get(rel)
        if not meshes:
            continue
        P, _N, sets = meshes[0]
        P = np.asarray(P, np.float32)
        done = set()
        for dc, m in placed:
            if dc >= len(sets) or (m is None and dc in done):
                continue
            done.add(dc)
            tris = np.asarray([t for t in sets[dc] if max(t) < len(P)], np.int64)
            if not len(tris):
                continue
            used = np.unique(tris)
            remap = np.full(len(P), -1, np.int64)
            remap[used] = np.arange(len(used))
            V = P[used] if m is None else P[used] @ m[:, :3].T + m[:, 3]
            out.append((rel, V, remap[tris]))
    return out


def model_materials(models):
    """{material: number of world models using it}."""
    c = collections.Counter()
    for data in models.values():
        for m in set(re.findall(rb"(?:materials|maps)/[\w/\-.]+\.vmat", data)):
            c[m.decode()] += 1
    return c


def terrain_layers(tools, addon, map_name, models=None):
    """The ground each side of the map stands on: {"radiant": layer,
    "dire": layer}, layer = {material, texture (vtex), tint, scale}. From the
    map's most used multiblend materials, by name (radiant / dire), else the
    most used for both; the layer is 1 (Dota's main ground: Radiant's grass,
    Dire's dirt) where there is one, else 0."""
    models = models if models is not None else world_models(tools, addon, map_name)
    used = model_materials(models)
    blends = []
    for mat, n in used.most_common():
        if "blend" not in mat:
            continue
        m = material(tools, addon, map_name, mat)
        if m and m["shader"].startswith("multiblend"):
            blends.append((mat, n, m))
    if not blends:
        return {}

    def pick(word, other):
        named = [b for b in blends if word in b[0].split("/")[-1] and other not in b[0].split("/")[-1]]
        # The side's main ground, not one of its paths, riverbeds, lava or
        # destroyed variants (ti10's most used "radiant" blend is a destroyed
        # path's, its most used "dire" one the lava).
        def penalty(b):
            n = b[0].split("/")[-1]
            return 2 if re.search(r"riverbed|lava|destruction|cliff|river", n) else 1 if "path" in n else 0
        return min(named or blends, key=penalty)
    out = {}
    for side, other in (("radiant", "dire"), ("dire", "radiant")):
        mat, _, m = pick(side, other)
        layer = 1 if "g_tColor1" in m["textures"] else 0
        out[side] = {"material": mat, "texture": m["textures"].get(f"g_tColor{layer}"),
                     "tint": m["vectors"].get(f"g_vColorTint{layer}", [1, 1, 1, 0])[:3],
                     "scale": m["floats"].get(f"g_flTexCoordScale{layer}", 1.0)}
    return out


def edge_layers(tools, addon, map_name, side, models=None, width=1500.0):
    """The ground along the map's edge next to a side's black corner, in a
    band `width` units wide along the corner's inner sides: {"flat": (blend
    material, layer), "steep": (...)}, the layers covering most of its flat
    ground and of its slopes (another than the flat one), by the world's
    vertex paint (VertexPaintBlendParams, layer 0 what the three painted
    ones leave), lava left out. {} if the map has no corner or no blend
    there."""
    import numpy as np
    models = models if models is not None else world_models(tools, addon, map_name)
    found = black_corners(tools, addon, map_name, models)
    if not found or not found[1].get(side):
        return {}
    _, sides, P, _ = found
    P = np.asarray(P)
    idx = np.unique(np.asarray(sides[side]).ravel())
    x0, x1, y0, y1 = P[idx, 0].min(), P[idx, 0].max(), P[idx, 1].min(), P[idx, 1].max()
    # Dire's corner is the top right: the band west of it and south of it;
    # Radiant's the bottom left: east and north.
    bands = [(x0 - width, x0, y0, y1), (x0, x1, y0 - width, y0)] if side == "dire" else \
        [(x1, x1 + width, y0, y1), (x0, x1, y1, y1 + width)]
    cover = {"flat": collections.Counter(), "steep": collections.Counter()}
    for rel, data in models.items():
        if "agg_merge" not in rel:
            continue
        blends = [k for k in model_materials({rel: data}) if "/blends/" in k and "lava" not in k.split("/")[-1]]
        if len(blends) != 1:
            continue
        try:
            streams, tris = decompile_streams(data)
        except Exception:
            continue
        pos, w = streams.get("position$0"), streams.get("texcoord$1")
        if pos is None or w is None or not len(tris):
            continue
        sel = np.zeros(len(pos), bool)
        for bx0, bx1, by0, by1 in bands:
            sel |= (pos[:, 0] >= bx0) & (pos[:, 0] <= bx1) & (pos[:, 1] >= by0) & (pos[:, 1] <= by1)
        if not sel.any():
            continue
        # How level each vertex's ground is: the most level of its triangles.
        tri = np.asarray(tris)
        n = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
        up = abs(n[:, 2]) / (np.linalg.norm(n, axis=1) + 1e-9)
        level = np.zeros(len(pos))
        for k in range(3):
            np.maximum.at(level, tri[:, k], up)
        for kind, part in (("flat", sel & (level > 0.9)), ("steep", sel & (level < 0.7))):
            if not part.any():
                continue
            # The shader's order: layer 1 first, 2 and 3 in what is left, 0 the rest.
            ww = np.clip(w[part][:, :3], 0, 1)
            l1 = ww[:, 0]
            l2 = np.minimum(ww[:, 1], 1 - l1)
            l3 = np.minimum(ww[:, 2], 1 - l1 - l2)
            for k, v in enumerate((1 - l1 - l2 - l3, l1, l2, l3)):
                cover[kind][(blends[0], k)] += float(v.sum())
    out = {}
    if cover["flat"]:
        out["flat"] = cover["flat"].most_common(1)[0][0]
    steep = [k for k, _ in cover["steep"].most_common() if k != out.get("flat")]
    if steep:
        out["steep"] = steep[0]
    return out


def black_corners(tools, addon, map_name, models=None):
    """The model that draws the black fields beyond the map's corners (one
    world-node model, all on materials/dev/black.vmat): (its path, its face
    sets split by side {"radiant": [...], "dire": [...]}, positions,
    normals); None if the map has none."""
    models = models if models is not None else world_models(tools, addon, map_name)
    hits = [rel for rel, data in models.items() if b"materials/dev/black.vmat" in data]
    if not hits:
        return None
    rel = hits[0]
    meshes = decompile_model(models[rel])
    if not meshes:
        return None
    P, N, sets = meshes[0]
    sides = {"radiant": [], "dire": []}
    for tris in sets:
        if not tris:
            continue
        cx = sum(P[i][0] for t in tris for i in t) / (3 * len(tris))
        cy = sum(P[i][1] for t in tris for i in t) / (3 * len(tris))
        sides["radiant" if cx + cy < 0 else "dire"].extend(tris)
    return rel, sides, P, N
