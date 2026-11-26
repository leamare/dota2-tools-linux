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

# The tools' own build first (the system's may be older than the game's
# resources: a new KV3 node type makes an old build throw the whole resource
# away, which reads back as empty shaders and missing blocks).
S2V_LOCAL = Path.home() / ".local" / "share" / "dota2tools" / "bin" / "source2viewer-cli"
S2V = str(S2V_LOCAL) if S2V_LOCAL.exists() else "source2viewer-cli"


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


@lru_cache(maxsize=1)
def _zstd_library():
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("zstd") or "libzstd.so.1"
    lib = ctypes.CDLL(name)
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    lib.ZSTD_getErrorName.restype = ctypes.c_char_p
    return lib


def kv3_replace_strings(data, replacements):
    """Same-length strings replaced in a final, zstd-compressed KV3 v5 DATA
    block. Its two buffers and the resource envelope are rebuilt; RED2 stays
    unchanged. Returns the complete compiled resource."""
    import ctypes
    if not data or len(data) < 16:
        raise ValueError("not a Source 2 resource")
    count = struct.unpack_from("<I", data, 12)[0]
    table = 8 + struct.unpack_from("<I", data, 8)[0]
    found = None
    for k in range(count):
        p = table + k * 12
        kind, rel, size = struct.unpack_from("<4sII", data, p)
        start = p + 4 + rel
        if kind == b"DATA":
            found = p, start, size
    if found is None:
        raise ValueError("resource has no DATA block")
    meta, start, size = found
    if start + size != len(data):
        raise ValueError("KV3 DATA must be the resource's last block")
    d = data[start:start + size]
    if len(d) < 124 or struct.unpack_from("<I", d, 0)[0] != 0x4B563305 \
            or struct.unpack_from("<I", d, 20)[0] != 2:
        raise ValueError("expected zstd-compressed binary KV3 v5")
    raw_sizes = [struct.unpack_from("<I", d, 72)[0], struct.unpack_from("<I", d, 80)[0]]
    compressed_sizes = [struct.unpack_from("<I", d, 76)[0], struct.unpack_from("<I", d, 84)[0]]
    pos = 120
    compressed = []
    for size in compressed_sizes:
        compressed.append(d[pos:pos + size])
        if len(compressed[-1]) != size:
            raise ValueError("truncated KV3 zstd buffer")
        pos += size
    tail = d[pos:]
    if len(tail) < 4:
        raise ValueError("truncated KV3 block trailer")
    lib = _zstd_library()

    def check(ret):
        if lib.ZSTD_isError(ret):
            raise ValueError(lib.ZSTD_getErrorName(ret).decode("utf-8", "replace"))
        return ret

    left = set(replacements)
    encoded = []
    for expected, source in zip(raw_sizes, compressed):
        buf = ctypes.create_string_buffer(max(1, expected))
        got = check(lib.ZSTD_decompress(buf, expected, ctypes.c_char_p(source), len(source)))
        if got != expected:
            raise ValueError(f"KV3 zstd size mismatch: {got} != {expected}")
        raw = buf.raw[:got]
        for old, new in replacements:
            if len(old) != len(new):
                raise ValueError("compiled KV3 string replacements must preserve length")
            if old in raw:
                raw = raw.replace(old, new)
                left.discard((old, new))
        if len(raw) != expected:
            raise ValueError("KV3 replacement changed a buffer's length")
        bound = lib.ZSTD_compressBound(len(raw))
        buf = ctypes.create_string_buffer(bound)
        got = check(lib.ZSTD_compress(buf, bound, ctypes.c_char_p(raw), len(raw), 3))
        encoded.append(buf.raw[:got])
    if left:
        raise ValueError(f"strings not found in KV3 DATA: {left!r}")
    header = bytearray(d[:120])
    struct.pack_into("<I", header, 76, len(encoded[0]))
    struct.pack_into("<I", header, 84, len(encoded[1]))
    struct.pack_into("<I", header, 52, sum(map(len, encoded)) + len(tail) - 4)
    out = bytearray(data[:start] + header + encoded[0] + encoded[1] + tail)
    struct.pack_into("<I", out, meta + 8, len(out) - start)
    struct.pack_into("<I", out, 0, len(out))
    return bytes(out)


def _lz4_block(src, size):
    """An LZ4 block (no frame) decoded to `size` bytes."""
    out = bytearray()
    i = 0
    while i < len(src):
        token = src[i]
        i += 1
        n = token >> 4
        if n == 15:
            while True:
                b = src[i]
                i += 1
                n += b
                if b != 255:
                    break
        out += src[i:i + n]
        i += n
        if i >= len(src):
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        n = token & 15
        if n == 15:
            while True:
                b = src[i]
                i += 1
                n += b
                if b != 255:
                    break
        for _ in range(n + 4):
            out.append(out[-off])
    if len(out) != size:
        raise ValueError(f"LZ4 size mismatch: {len(out)} != {size}")
    return bytes(out)


def _lz4_literal(raw):
    """A valid LZ4 block holding `raw` as one run of literals."""
    out = bytearray()
    n = len(raw)
    if n >= 15:
        out.append(0xF0)
        n -= 15
        while n >= 255:
            out.append(255)
            n -= 255
        out.append(n)
    else:
        out.append(n << 4)
    return bytes(out) + raw


def resource_replace_strings(data, replacements):
    """Same-length strings replaced everywhere in a compiled resource: its
    plain blocks as bytes, and inside its binary KV3 v5 blocks (zstd- or
    LZ4-compressed buffers, rewritten). Material names, for instance, sit
    both in RERL (plain) and in a model's MDAT (compressed KV3)."""
    import ctypes
    table = 8 + struct.unpack_from("<I", data, 8)[0]
    count = struct.unpack_from("<I", data, 12)[0]
    blocks = []
    for k in range(count):
        p = table + k * 12
        kind, rel, size = struct.unpack_from("<4sII", data, p)
        blocks.append((p, p + 4 + rel, size))
    for old, new in replacements:
        if len(old) != len(new):
            raise ValueError("resource string replacements must preserve length")
    lib = None
    new_blocks = {}
    for p, start, size in blocks:
        d = data[start:start + size]
        if len(d) >= 120 and struct.unpack_from("<I", d, 0)[0] == 0x4B563305 \
                and struct.unpack_from("<I", d, 20)[0] in (1, 2):
            method = struct.unpack_from("<I", d, 20)[0]
            raw_sizes = [struct.unpack_from("<I", d, 72)[0], struct.unpack_from("<I", d, 80)[0]]
            sizes = [struct.unpack_from("<I", d, 76)[0], struct.unpack_from("<I", d, 84)[0]]
            pos, raws = 120, []
            for rs, cs in zip(raw_sizes, sizes):
                src = d[pos:pos + cs]
                pos += cs
                if method == 1:
                    raws.append(_lz4_block(src, rs))
                else:
                    lib = lib or _zstd_library()
                    buf = ctypes.create_string_buffer(max(1, rs))
                    got = lib.ZSTD_decompress(buf, rs, ctypes.c_char_p(src), len(src))
                    if lib.ZSTD_isError(got) or got != rs:
                        raise ValueError("KV3 zstd buffer could not be read")
                    raws.append(buf.raw[:got])
            if not any(old in r for r in raws for old, _ in replacements):
                continue
            for old, new in replacements:
                raws = [r.replace(old, new) for r in raws]
            if method == 1:
                enc = [_lz4_literal(r) for r in raws]
            else:
                enc = []
                for r in raws:
                    bound = lib.ZSTD_compressBound(len(r))
                    buf = ctypes.create_string_buffer(bound)
                    got = lib.ZSTD_compress(buf, bound, ctypes.c_char_p(r), len(r), 3)
                    enc.append(buf.raw[:got])
            header = bytearray(d[:120])
            struct.pack_into("<I", header, 76, len(enc[0]))
            struct.pack_into("<I", header, 84, len(enc[1]))
            tail = d[pos:]
            struct.pack_into("<I", header, 52, sum(map(len, enc)) + max(len(tail) - 4, 0))
            new_blocks[start] = bytes(header + enc[0] + enc[1] + tail)
        else:
            e = d
            for old, new in replacements:
                e = e.replace(old, new)
            if e != d:
                new_blocks[start] = e
    if not new_blocks:
        return data
    # Rebuilt in their order, each at its original alignment.
    order = sorted(blocks, key=lambda b: b[1])
    out = bytearray(data[:order[0][1]])
    placed = {}
    for p, start, size in order:
        while len(out) % 16 != start % 16:
            out.append(0)
        placed[p] = len(out)
        out += new_blocks.get(start, data[start:start + size])
    for p, start, size in order:
        body = new_blocks.get(start)
        struct.pack_into("<II", out, p + 4, placed[p] - (p + 4), len(body) if body is not None else size)
    struct.pack_into("<I", out, 0, len(out))
    return bytes(out)


def decompile_model(data):
    """A compiled model's meshes (source2viewer-cli -d): [(positions, normals,
    [triangles per face set])], world space for map aggregates."""
    from . import dmxbin
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.vmdl_c"
        f.write_bytes(data)
        subprocess.run([S2V, "-i", str(f), "-o", str(Path(d) / "out"), "-d"], capture_output=True, cwd=d)
        meshes = []
        # Newer source2viewer names a model's mesh DMX after the model, not
        # *meshset*; take every DMX the decompile wrote.
        found = sorted(Path(d).glob("**/*.dmx"))
        for dmx in found:
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


def decompile_textured(data):
    """Compiled model meshes with per-face-corner positions, normals and UVs:
    [(P, N, UV, [(material path, triangles), ...])]. Used when selected cards
    need copying without losing their original texture coordinates."""
    import numpy as np
    from . import dmxbin
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.vmdl_c"
        f.write_bytes(data)
        subprocess.run([S2V, "-i", str(f), "-o", str(Path(d) / "out"), "-d"], capture_output=True, cwd=d)
        meshes = []
        for dmx in sorted(Path(d).glob("**/*.dmx")):
            x = dmxbin.Dmx(dmx.read_bytes())
            vd = [e for e in x.elements if e.type == "DmeVertexData"][0]

            def stream(name, width, default):
                if name not in vd.attrs or name + "Indices" not in vd.attrs:
                    return []
                vals = np.asarray(vd.attrs[name], np.float32).reshape(-1, width)
                return vals[np.asarray(vd.attrs[name + "Indices"], np.int64)]

            P = stream("position$0", 3, (0, 0, 0))
            N = stream("normal$0", 3, (0, 0, 1))
            UV = stream("texcoord$0", 2, (0, 0))
            if not len(N):
                N = np.tile(np.asarray((0, 0, 1), np.float32), (len(P), 1))
            if not len(UV):
                UV = np.zeros((len(P), 2), np.float32)
            sets = []
            for e in x.elements:
                if e.type != "DmeFaceSet":
                    continue
                tris, poly = [], []
                for k in e.attrs["faces"]:
                    if k < 0:
                        tris += [(poly[0], poly[j], poly[j + 1]) for j in range(1, len(poly) - 1)]
                        poly = []
                    else:
                        poly.append(k)
                mi = e.attrs.get("material", -1)
                mat = x.elements[mi].attrs.get("mtlName", "") if 0 <= mi < len(x.elements) else ""
                sets.append((mat, tris))
            meshes.append((P, N, UV, sets))
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
        found = sorted(Path(d).glob("**/*.dmx"))
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
    float_changes = {k: v for k, v in changes.items()
                     if not k.startswith("F_") and isinstance(v, (int, float))}
    for k in dict.fromkeys([*m["floats"], *float_changes]):
        lines.append(f'{k} "{float(changes.get(k, m["floats"].get(k, 0.0))):.6f}"')
    vector_changes = {k: v for k, v in changes.items() if isinstance(v, (list, tuple))}
    for k in dict.fromkeys([*m["vectors"], *vector_changes]):
        if k.startswith("Texture"):
            continue
        v = changes[k] if k in changes else m["vectors"][k]
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
