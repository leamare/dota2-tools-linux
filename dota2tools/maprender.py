"""Top-down map pictures rendered in Source Filmmaker, then stitched.

Source Filmmaker (Workshop Tools -> Tools -> Source Filmmaker) renders the map
from a camera with a 1-degree lens 240000 units up, keyed once a second over a
grid of frames in a serpentine; each 3840 x 2160 frame covers about 4190 x 2360
units. With so narrow a lens the frames are near-orthographic, so the stitch
places each by the camera position the session itself holds -- no feature
matching. The method, the session template and the stitch follow sikleq's
Sloppy (scripts/gen/sfm_session.py, stitch_sfm.py; MIT, see NOTICE); here the
camera grid is made for each map's own size, and looks straight down.

Steps (the launcher's Map renderer window does all but the SFM clicks):
  1. prepare: a render addon of its own (default dota2tools_render) that
     mirrors the chosen addon (its maps and assets, linked) or stands alone for
     the base game's maps; the pre-run scripts (render.prerun in the settings,
     default builtin:flies, builtin:tools_materials, builtin:edge_trees,
     builtin:corners, builtin:lava) write and compile
     material overrides and models into it; the session, with those models in
     its scene, goes to its elements/sessions.
  2. launch the tools on the render addon, Tools -> Source Filmmaker, File ->
     Open the session, File -> Export -> Movie (image sequence, PNG): the
     export folder is already set in the session.
  3. stitch the exported frames into one picture.

A pre-run script is a Python file with run(ctx) (see prerun/corners.py):
ctx.write(rel, text), ctx.copy(src, rel), ctx.dota_texture(vtex, rel) (one
of the game's own textures as an image), ctx.override_material(vmat, changes)
and ctx.compile(rels) work in the render addon's content folder;
ctx.add_model(vmdl) puts one of its models into the session; ctx.options is the settings' render.<name>
dict; ctx.addon, ctx.map, ctx.render_addon, ctx.tools, ctx.log(text).
"""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import math
import os
import re
import shutil
import uuid
from pathlib import Path

from . import core

TEMPLATE = Path(__file__).parent / "data" / "sfm_map_session.dmx"
HEIGHT = 240000.0             # camera height
FOV = 1.0                     # degrees, horizontal
FRAME_W, FRAME_H = 3840, 2160
GROUND_Z = 128.0              # typical ground height, the plane frames are laid on
QUAT = (-0.5, 0.5, 0.5, 0.5)  # x y z w: looking straight down, north up, east right
DEFAULT_RECT = (-11000.0, 11000.0, -11000.0, 11000.0)    # x0 x1 y0 y1: the Dota map with room to spare
MARGIN = 400.0                # around a map's overview bounds
CHANNEL_START = -5.0          # the template's channels clip starts 5 s before the shot
HOLD = 0.4                 # seconds the camera stays put either side of a frame
MARKER = ".dota2tools_render"
# Base-game maps worth rendering first in the list.
SUGGESTED = ["dota", "dota_ti10", "dota_winter", "dota_summer", "dota_spring", "dota_autumn",
             "dota_desert", "dota_jungle", "dota_reef", "dota_journey", "dota_coloseum", "dota_cavern",
             "dota_crownfall", "dota_737", "dota_732", "dota_728", "dota_722", "dota_719", "dota_706",
             "dota_688", "dota_685", "dota_683"]


def suggested_maps(tools, addon=""):
    """The addon's maps, or the base game's with the usual ones first."""
    maps = tools.maps(addon)
    if addon:
        return maps
    return [m for m in SUGGESTED if m in maps] + [m for m in maps if m not in SUGGESTED]


def overview(tools, addon, map_name):
    """The map's minimap bounds from resource/overviews/<map>.txt (the addon's,
    else the base game's): (x0, x1, y0, y1) or None."""
    rel = f"resource/overviews/{map_name}.txt"
    text = None
    if addon:
        p = tools.root / "game" / "dota_addons" / addon / rel
        if p.exists():
            text = p.read_text(errors="replace")
    if text is None:
        loose = tools.root / "game" / "dota" / rel
        if loose.exists():
            text = loose.read_text(errors="replace")
        else:
            data = core.vpk_read(tools.root / "game" / "dota" / "pak01_dir.vpk", rel)
            text = data.decode("utf-8", "replace") if data else None
    if not text:
        return None
    vals = {k.lower(): float(v) for k, v in re.findall(r'"?\b(pos_x|pos_y|scale)\b"?\s+"?([-\d.]+)', text, re.I)}
    if not {"pos_x", "pos_y", "scale"} <= set(vals):
        return None
    size = 1024 * vals["scale"]
    return vals["pos_x"], vals["pos_x"] + size, vals["pos_y"] - size, vals["pos_y"]


def map_rect(tools, addon, map_name):
    """The world rectangle to render: the overview's with a margin, else the
    Dota map's."""
    r = overview(tools, addon, map_name)
    if not r:
        return DEFAULT_RECT
    return r[0] - MARGIN, r[1] + MARGIN, r[2] - MARGIN, r[3] + MARGIN


def frame_size(ground=GROUND_Z):
    """One frame's ground size in units, and units per pixel."""
    upp = 2 * (HEIGHT - ground) * math.tan(math.radians(FOV) / 2) / FRAME_W
    return FRAME_W * upp, FRAME_H * upp, upp


def grid(rect, overlap=0.15):
    """Frame centres over the rectangle, in the serpentine order the camera
    flies: rows bottom to top, alternating direction. The outer frames reach
    the rectangle's edges; neighbours overlap by at least `overlap`."""
    x0, x1, y0, y1 = rect
    fw, fh, _ = frame_size()

    def axis(lo, hi, f):
        span = hi - lo
        n = max(1, math.ceil((span - f) / (f * (1 - overlap))) + 1) if span > f else 1
        if n == 1:
            return [(lo + hi) / 2]
        step = (span - f) / (n - 1)
        return [lo + f / 2 + i * step for i in range(n)]
    xs, ys = axis(x0, x1, fw), axis(y0, y1, fh)
    out = []
    for j, y in enumerate(ys):
        row = xs if j % 2 == 0 else xs[::-1]
        out += [(x, y) for x in row]
    return out, len(xs), len(ys)


def _replace_array(text, start, name, items):
    """The "<name>" array of the block after `start` replaced by items."""
    i = text.index(start)
    m = re.compile(r'("%s" "\w+_array"\s*\[)(.*?)(\])' % name, re.S).search(text, i)
    body = "\n" + ",\n".join(f'\t\t\t\t\t"{x}"' for x in items) + "\n\t\t\t\t"
    return text[:m.start(2)] + body + text[m.end(2):]


def _model_dag(vmdl, name, position=(0, 0, 0)):
    """A model in the session's scene (KeyValues2): a group holding the
    game model, the model at `position`."""
    def ident(pos=(0, 0, 0)):
        return (f'"transform" "DmeTransform"\n{{\n"id" "elementid" "{uuid.uuid4()}"\n'
                f'"position" "vector3" "{pos[0]} {pos[1]} {pos[2]}"\n"orientation" "quaternion" "0 0 0 1"\n}}\n')
    return (f'"DmeDag"\n{{\n"id" "elementid" "{uuid.uuid4()}"\n"name" "string" "{name}"\n{ident()}'
            f'"shape" "element" ""\n"visible" "bool" "1"\n"children" "element_array"\n[\n'
            f'"DmeGameModel"\n{{\n"id" "elementid" "{uuid.uuid4()}"\n"name" "string" "{name}"\n{ident(position)}'
            f'"shape" "element" ""\n"visible" "bool" "1"\n"children" "element_array"\n[\n]\n'
            f'"flexWeights" "float_array"\n[\n]\n"modelName" "string" "{vmdl}"\n"skin" "int" "0"\n'
            f'"meshGroupMask" "uint64" "0xffffffffffffffff"\n"bones" "element_array"\n[\n]\n'
            f'"globalFlexControllers" "element_array"\n[\n]\n"computeBounds" "bool" "1"\n'
            f'"evaluateProceduralBones" "bool" "1"\n}}\n]\n}},\n')


def render_convars(tools, addon="", map_name=""):
    """The console commands the tools start with for this render: the
    settings', and for a map whose lights draw caustics through the height
    fog (dota_reef: an underwater shimmer that covers the whole map from
    above), the height fog off."""
    c = tools.settings["render"].get("convars", "")
    if map_name:
        from . import mapinfo
        if any("caustics" in str(e.get("height_fog_texture0", "")) + str(e.get("height_fog_texture1", ""))
               for e in mapinfo.entities(tools, addon, map_name)):
            c += " +r_deferred_height_fog 0"
    return c.strip()


def render_scale(tools):
    """Pixels per unit against the default render: 0.5, 1 or 2."""
    try:
        return float(tools.settings["render"].get("scale", 1.0)) or 1.0
    except (TypeError, ValueError):
        return 1.0


def session_text(map_name, frames_dir, rect, overlap=0.15, models=(), shadows=True, repeat=2):
    """The SFM session (KeyValues2 text) for the map: its camera grid, the
    export folder (a Linux path, as Wine sees it), the shot's length, and
    the pre-run scripts' models ([(vmdl, name, position)]) in its scene."""
    s = TEMPLATE.read_text(encoding="utf-8")
    centres, nx, ny = grid(rect, overlap)
    # The first position twice: SFM writes the first frame before the
    # session's models have loaded; the stitch drops that one.
    centres = [centres[0]] + centres
    # Each position `repeat` times; the stitch keeps the sharpest. In a long
    # export the texture streaming falls behind here and there, differently
    # every run, and catches up a frame later.
    centres = [c for c in centres for _ in range(max(1, repeat))]
    n = len(centres)
    s = s.replace('"mapname" "string" "maps/dota.vmap"', f'"mapname" "string" "maps/{map_name}.vmap"')
    path = core.winpath(Path(frames_dir)).rstrip("\\") + "\\"
    s = s.replace('"FRAMES_DIR"', '"' + path.replace("\\", "\\\\") + '"')
    s = s.replace("-0.4964516461 0.5035233498 0.5035233498 0.4964516461", " ".join(f"{q:.10f}" for q in QUAT))
    pos = '"name" "string" "transform_pos"'
    # Each position held from 0.4 s before its frame to 0.4 s after: SFM's
    # motion blur samples the camera over the shutter, and a camera still
    # gliding to the next position smeared the frame along its path.
    times, values = [], []
    for k, (x, y) in enumerate(centres):
        for dt in (-HOLD, HOLD):
            times.append(f"{k - CHANNEL_START + dt:.4f}")
            values.append(f"{x:.4f} {y:.4f} {HEIGHT:.4f}")
    s = _replace_array(s, pos, "times", times)
    s = _replace_array(s, pos, "values", values)
    s = s.replace('"TimeRangeEndTime" "float" "66"', f'"TimeRangeEndTime" "float" "{n}"')
    s = s.replace('"duration" "time" "66.0000"', f'"duration" "time" "{n:.4f}"')
    s = s.replace('"duration" "time" "70.0000"', f'"duration" "time" "{n + 4:.4f}"')
    if models:
        # Into the scene's children, before its Cameras group.
        i = s.rfind('"DmeDag"', 0, s.index('"name" "string" "Cameras"'))
        s = s[:i] + "".join(_model_dag(*m) for m in models) + s[i:]
    if not shadows:
        s = s.replace('"globalLightShadowEnabled" "bool" "1"', '"globalLightShadowEnabled" "bool" "0"')
    return s, nx, ny


# --- the render addon and the pre-run scripts --------------------------------

def render_paths(tools, name):
    return (tools.root / "game" / "dota_addons" / name, tools.root / "content" / "dota_addons" / name)


def prepare_addon(tools, addon, name, log=print):
    """The render addon: its own (marked) folders, the source addon's game
    folder mirrored in -- top-level entries linked, materials/ as real folders
    of linked files so overrides can go in beside them. Rebuilt every time."""
    if name == addon:
        raise SystemExit("the render addon must not be the addon itself")
    game, content = render_paths(tools, name)
    for d in (game, content):
        if d.exists() and not (d / MARKER).exists():
            raise SystemExit(f"{d} exists and is not a render addon: choose another render addon name")
    for d in (game, content):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    for d in (game, content):
        (d / MARKER).write_text("made by dota2-tools' map renderer; rebuilt on every render\n")
    # Which addon it mirrors, for the tools' launch later (render_convars).
    (game / ".dota2tools_source").write_text(addon or "")
    src = tools.root / "game" / "dota_addons" / addon if addon else None
    if src and (src / "addoninfo.txt").exists():
        shutil.copy2(src / "addoninfo.txt", game / "addoninfo.txt")
    else:
        (game / "addoninfo.txt").write_text('"AddonInfo"\n{\n\t"TeamCount"\t\t"2"\n\t"maps"\t\t\t"dota"\n'
                                            '\t"IsPlayable"\t"1"\n}\n')
    linked = 0
    if src:
        for entry in src.iterdir():
            # The tools' own caches: written while the tools run on the render
            # addon, they would be written into the source addon.
            if entry.name in ("addoninfo.txt", "elements", MARKER, ".dota2tools_source", "tools_asset_info.bin",
                              "tools_thumbnail_cache.sqlite3", "tools_thumbnail_cache.sqlite3-journal"):
                continue
            # Real folders of linked files where a pre-run script may put its
            # own files beside the addon's (materials, the map's models).
            if entry.name in ("materials", "maps") and entry.is_dir():
                for dirpath, _, names in os.walk(entry):
                    rel = Path(dirpath).relative_to(src)
                    (game / rel).mkdir(parents=True, exist_ok=True)
                    for f in names:
                        (game / rel / f).symlink_to(Path(dirpath) / f)
                        linked += 1
            else:
                (game / entry.name).symlink_to(entry)
                linked += 1
    (game / "elements" / "sessions").mkdir(parents=True, exist_ok=True)
    log(f"render addon {name}: {game}" + (f" (mirrors {addon}, {linked} links)" if src else ""))
    return game, content


class Context:
    """What a pre-run script gets."""
    def __init__(self, tools, addon, map_name, render_addon, options, log):
        self.tools, self.addon, self.map, self.render_addon = tools, addon, map_name, render_addon
        self.game, self.content = render_paths(tools, render_addon)
        self.options, self.log = options, log
        self.models = []

    def add_model(self, vmdl, name=None, position=(0, 0, 0)):
        """A model (models/....vmdl: the render addon's or the game's) put in
        the SFM session at `position` (default the world's origin: a model
        whose vertices are where it stands)."""
        self.models.append((vmdl, name or Path(vmdl).stem, tuple(position)))

    def write(self, rel, text):
        p = self.content / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return rel

    def copy(self, src, rel):
        p = self.content / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, p)
        return rel

    def unshadow(self, rel):
        """Before compiling content file `rel` into the render addon: no link
        into the source addon may stand on the way to its output, or the
        compiler writes through it into the addon itself. Each linked folder
        on the way becomes a real folder of links to what it held; a linked
        output file is removed."""
        unshadow(self.game, rel + "_c")

    def compile(self, rels):
        """resourcecompiler on content files of the render addon (forced),
        their output into its game folder."""
        for rel in rels:
            self.unshadow(rel)
            ok, out = core.resource_compile(self.tools, self.content / rel)
            if not ok:
                raise SystemExit(f"resourcecompiler failed on {rel}:\n" + "\n".join(out.splitlines()[-20:]))
            self.log(f"compiled {rel}")

    def dota_texture(self, vtex, rel, alpha=False, addon=None, map=None):
        """One of the game's own compiled textures (materials/....vtex: from
        the map's package, the addon, else pak01) decoded into the render
        addon's content as an image file, for a material to use."""
        from . import mapinfo
        img = mapinfo.texture_image(self.tools, self.addon if addon is None else addon,
                                    self.map if map is None else map, vtex)
        p = self.content / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        (img if alpha else img.convert("RGB")).save(p)
        return rel


    def override_material(self, vmat, changes, comment="dota2-tools map renderer: an override", edits=None,
                          out=None, source=None):
        """A material of the map or the game (materials/....vmat) put in the
        render addon as it is but for `changes` ({param: value}, vectors as
        lists, F_ flags as ints): its compiled form read back to source, its
        textures decoded and, where `edits` has their source parameter,
        passed through it (edits[param](image) -> image). A texture whose
        source parameter the compiled
        material doesn't name (a shader default) is named from the
        compiler's complaint and compiled again. `out`: written under that
        path instead (a new material); `source`: the material dict to start
        from instead of vmat's (mapinfo.material's form)."""
        from . import mapinfo
        import re as _re
        m = source or mapinfo.material(self.tools, self.addon, self.map, vmat)
        vmat = out or vmat
        if not m:
            raise SystemExit(f"{vmat}: not in the map or the game")
        names = {g: mapinfo.source_name(m["vectors"], g) for g in m["textures"]}
        for _ in range(4):
            textures = {}
            for g, vtex in m["textures"].items():
                stem = Path(vtex).name.split("_psd_")[0].split("_vmat_")[0].split(".")[0]
                rel = self.dota_texture(vtex, f"materials/dota2tools/textures/{stem}.png", alpha=True)
                if "TintMask" in names[g]:
                    # Compiled, a mask sits in whichever channel the format
                    # chose (green, in a generated one); as a source it is
                    # read as grey. The strongest channel, as grey.
                    from PIL import Image, ImageChops
                    p = self.content / rel
                    im = Image.open(p).convert("RGB")
                    r, gg, b = im.split()
                    ImageChops.lighter(ImageChops.lighter(r, gg), b).save(p)
                if names[g] == "TextureNormal":
                    _unpack_normal(self.content / rel)
                if edits and names[g] in edits:
                    from PIL import Image
                    p = self.content / rel
                    edits[names[g]](Image.open(p)).save(p)
                textures[names[g]] = rel
            ints = {**m["ints"], **{k: v for k, v in changes.items() if k.startswith("F_")}}
            if (ints.get("F_ALPHA_TEST") or ints.get("F_TRANSLUCENT")) and "TextureColor" in textures \
                    and "TextureTranslucency" not in textures:
                # Compiled, the colour map carries the translucency in its
                # alpha; as a source the shader takes it from its own input.
                from PIL import Image
                src = self.content / textures["TextureColor"]
                rel = str(Path(textures["TextureColor"]).with_name(src.stem + "_translucency.png"))
                Image.open(src).convert("RGBA").split()[3].save(self.content / rel)
                textures["TextureTranslucency"] = rel
            self.write(vmat, mapinfo.source_vmat(m, textures, changes, comment))
            self.unshadow(vmat)
            ok, out = core.resource_compile(self.tools, self.content / vmat)
            if ok:
                self.log(f"compiled {vmat}")
                return vmat
            bad = _re.search(r'for "(Texture\w+)" when compiling.*?\n.*?Error processing texture (g_t\w+)', out, _re.S)
            if not bad or names.get(bad.group(2)) == bad.group(1):
                break
            names[bad.group(2)] = bad.group(1)
        raise SystemExit(f"resourcecompiler failed on {vmat}:\n" + "\n".join(out.splitlines()[-12:]))


def _unpack_normal(path):
    """A compiled normal map decoded as stored -- X in alpha, Y in green, red
    and blue empty -- rewritten as the RGB normal a source texture is."""
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(path).convert("RGBA"), np.float32) / 255
    if a[:, :, 0].max() > 0.01 or a[:, :, 2].max() > 0.01:
        return
    x, y = a[:, :, 3] * 2 - 1, a[:, :, 1] * 2 - 1
    z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
    rgb = np.stack([x, y, z], 2) * 0.5 + 0.5
    Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).save(path)


def unshadow(game, rel):
    """Make `game`/`rel` safe to write: every folder on the way that is a
    link (into the source addon) replaced by a real folder of links to its
    entries, and the file itself, if a link, removed."""
    path = Path(game)
    for part in Path(rel).parent.parts:
        path = path / part
        if path.is_symlink():
            target = path.resolve()
            path.unlink()
            path.mkdir()
            if target.is_dir():
                for child in target.iterdir():
                    (path / child.name).symlink_to(child)
        elif not path.exists():
            path.mkdir()
    out = Path(game) / rel
    if out.is_symlink():
        out.unlink()
    if out.resolve().parts[:len(Path(game).resolve().parts)] != Path(game).resolve().parts:
        raise SystemExit(f"{out} leads outside the render addon: not writing it")
    return out


def prerun_module(spec):
    """builtin:<name> (dota2tools/prerun/<name>.py) or a path to a .py file."""
    if spec.startswith("builtin:"):
        path = Path(__file__).parent / "prerun" / f"{spec.split(':', 1)[1]}.py"
    else:
        path = Path(spec).expanduser()
    if not path.exists():
        raise SystemExit(f"pre-run script {spec} not found ({path})")
    m = importlib.util.spec_from_file_location(f"dota2tools_prerun_{path.stem}", path)
    mod = importlib.util.module_from_spec(m)
    m.loader.exec_module(mod)
    return path.stem, mod


def no_progress(_text, _fraction):
    pass


def sun_override(tools):
    """The render's sun, (pitch, yaw), or None for the map's own (custom
    lighting off, or its sun direction off)."""
    r = tools.settings["render"]
    sun = r.get("sun", {})
    if not r.get("lighting") or not sun.get("on", True):
        return None
    try:
        if str(sun.get("pitch", "")).strip() == "" or str(sun.get("yaw", "")).strip() == "":
            return None
        return float(sun["pitch"]), float(sun["yaw"])
    except (TypeError, ValueError):
        return None


def shadow_strength(tools):
    """The sun's shadows' strength in the pictures, 0..1 (1: as rendered, or
    custom lighting off)."""
    r = tools.settings["render"]
    if not r.get("lighting"):
        return 1.0
    try:
        return max(0.0, min(1.0, float(r.get("shadows", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def noshadow_dir(frames_dir):
    """Where the second pass, without the sun's shadows, is exported."""
    f = Path(frames_dir)
    return f.with_name(f.name + "_noshadow")


def map_source(tools, addon, map_name):
    """The map's source (content/.../maps/<map>.vmap), or None."""
    base = tools.root / "content" / ("dota_addons/" + addon if addon else "dota")
    p = base / "maps" / f"{map_name}.vmap"
    return p if p.exists() else None


def relit_map(tools, addon, map_name, game, content, sun, log=print, progress=no_progress):
    """A copy of the map with its sun at `sun` (pitch, yaw) compiled into the
    render addon (game, content): its source converted to text, the global
    light's angles and every ent_dota_lightinfo's light direction changed,
    converted back and compiled there. The map and its addon stay as they
    are. Kept in the tools' cache by the source and the angles. False when
    the map has no source."""
    import hashlib
    src = map_source(tools, addon, map_name)
    if not src:
        log(f"sun: {map_name} has no source (maps/{map_name}.vmap) to rebuild: its own sun stays")
        return False
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_size}|{st.st_mtime_ns}|{sun[0]:g}|{sun[1]:g}".encode()).hexdigest()[:16]
    cache = tools.meta / "cache" / "relit" / f"{addon or 'dota'}_{map_name}_{key}.vpk"
    out = unshadow(game, f"maps/{map_name}.vpk")
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        shutil.copy2(cache, out)
        log(f"sun: {map_name} at pitch {sun[0]:g}, yaw {sun[1]:g} (rebuilt before)")
        return True
    log(f"sun: rebuilding a copy of {map_name} with its sun at pitch {sun[0]:g}, yaw {sun[1]:g} "
        f"(a few minutes; once per source and angles)")
    work = content / "maps"
    work.mkdir(parents=True, exist_ok=True)
    text_in, text_out, vmap = work / f"{map_name}_text.vmap", work / f"{map_name}_relit_text.vmap", \
        work / f"{map_name}.vmap"
    progress("sun: the map's source as text", 0.0)
    rc, msg = core.proton_run(tools, "dmxconvert.exe", ["-i", core.winpath(src), "-o", core.winpath(text_in),
                                                        "-oe", "keyvalues2"])
    if rc != 0 or not text_in.exists():
        raise SystemExit(f"dmxconvert could not read {src} (rc {rc})")
    t = text_in.read_text(encoding="utf-8", errors="surrogateescape")
    angles = f"{sun[0]:g} {sun[1]:g} 0"
    blocks, n = t.split('"CMapEntity"'), 0
    for i, b in enumerate(blocks):
        if '"classname" "string" "env_global_light"' in b:
            b = re.sub(r'("angles" "string" )"[^"]*"', lambda m: f'{m.group(1)}"{angles}"', b, count=1)
            blocks[i] = b
            n += 1
        elif '"classname" "string" "ent_dota_lightinfo"' in b:
            b = re.sub(r'("light_direction_(?:day|night)" "string" )"[^"]*"', lambda m: f'{m.group(1)}"{angles}"', b)
            blocks[i] = b
            n += 1
    if not n:
        log(f"sun: {map_name}'s source has no global light: its own sun stays")
        text_in.unlink()
        return False
    text_out.write_text('"CMapEntity"'.join(blocks), encoding="utf-8", errors="surrogateescape")
    text_in.unlink()
    progress("sun: back to the map's format", 0.3)
    rc, msg = core.proton_run(tools, "dmxconvert.exe", ["-i", core.winpath(text_out), "-o", core.winpath(vmap),
                                                        "-oe", "binary"])
    text_out.unlink()
    if rc != 0 or not vmap.exists():
        raise SystemExit(f"dmxconvert could not write the relit map (rc {rc})")
    progress("sun: compiling the copy", 0.5)
    ok, msg = core.resource_compile(tools, vmap)
    if not ok or not out.exists():
        raise SystemExit("resourcecompiler failed on the relit map:\n" + "\n".join(msg.splitlines()[-15:]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    for old in cache.parent.glob(f"{addon or 'dota'}_{map_name}_*.vpk"):
        old.unlink()
    shutil.copy2(out, cache)
    log(f"sun: {map_name} rebuilt with its sun at pitch {sun[0]:g}, yaw {sun[1]:g}")
    return True


def prepare(tools, addon, map_name, frames_dir, rect=None, log=print, progress=no_progress):
    """Steps before SFM: render addon, pre-run scripts, session. Returns the
    session's path (binary if dmxconvert managed, else the text)."""
    r = tools.settings["render"]
    name = r["render_addon"]
    rect = rect or cover(products(tools, addon, map_name)) or map_rect(tools, addon, map_name)
    progress("the render addon", 0.0)
    game, content = prepare_addon(tools, addon, name, log)
    sun = sun_override(tools)
    if sun:
        relit_map(tools, addon, map_name, game, content, sun, log,
                  lambda text, f: progress(text, 0.4 * f))
    models = []
    scripts = r.get("prerun", []) if r.get("prerun_enabled", True) else []
    for i, spec in enumerate(scripts):
        stem, mod = prerun_module(spec)
        progress(f"pre-run {spec}", (0.4 if sun else 0) + (0.6 if sun else 1) * (i + 0.05) / (len(scripts) + 1))
        log(f"pre-run {spec}")
        ctx = Context(tools, addon, map_name, name, r.get(stem, {}), log)
        mod.run(ctx)
        models += ctx.models
    progress("the session", len(scripts) / (len(scripts) + 1))
    sessions = game / "elements" / "sessions"
    # A second pass without the sun's shadows when they are to be weaker:
    # the stitch blends the two.
    passes = [("", Path(frames_dir), True)]
    if shadow_strength(tools) < 1:
        passes.append(("_noshadow", noshadow_dir(frames_dir), False))
    elif noshadow_dir(frames_dir).exists():
        shutil.rmtree(noshadow_dir(frames_dir))
    first = None
    for suffix, frames, shadows in passes:
        frames.mkdir(parents=True, exist_ok=True)
        text, nx, ny = session_text(map_name, frames, rect, r.get("overlap", 0.15), models, shadows,
                                    int(r.get("repeat", 2)))
        base = f"topdown_{map_name}{suffix}"
        tpath = sessions / f"{base}_text.dmx"
        tpath.write_text(text, encoding="utf-8")
        # A copy outside the addon: the stitch reads the camera path from it.
        (frames / "session_text.dmx").write_text(text, encoding="utf-8")
        bpath = sessions / f"{base}.dmx"
        rc, out = core.proton_run(tools, "dmxconvert.exe", ["-i", core.winpath(tpath), "-o", core.winpath(bpath),
                                                            "-oe", "binary"])
        if rc != 0 or not bpath.exists():
            log(f"dmxconvert failed (rc {rc}); the text session works in SFM too: {tpath.name}")
            bpath = tpath
        log(f"session {bpath.name}: {nx} x {ny} positions, {len(camera_path(text)[0])} frames, "
            f"rect x {rect[0]:.0f}..{rect[1]:.0f}, y {rect[2]:.0f}..{rect[3]:.0f}; frames go to {frames}")
        first = first or bpath
    return first


def sfm_steps(tools, map_name):
    name = tools.settings["render"]["render_addon"]
    return (f"In the tools (launched on {name}): Tools -> Source Filmmaker; File -> Open -> "
            f"topdown_{map_name}.dmx; File -> Export -> Movie (Ctrl+M), image sequence, PNG -- the "
            f"folder is already set; wait for every frame, then Stitch."
            + (f" Shadows below 100 %: export topdown_{map_name}_noshadow.dmx the same way too."
               if shadow_strength(tools) < 1 else ""))


# --- stitching (after sikleq's Sloppy stitch_sfm.py, MIT) ---------------------

def _array(name, block):
    m = re.search(r'"%s" "\w+_array"\s*\[(.*?)\]' % name, block, re.S)
    return [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()]


def _float(name, text):
    return float(re.search(r'"%s" "float" "([-\d.e]+)"' % name, text).group(1))


def camera_path(text):
    """Per exported frame: the camera position; the orientation quaternion
    (x y z w), the lens and the frame width."""
    import numpy as np
    pos_block = text[text.find('"name" "string" "transform_pos"'):]
    times = np.array([float(t) for t in _array("times", pos_block)])
    values = np.array([[float(v) for v in s.split()] for s in _array("values", pos_block)])
    rot_block = text[text.find('"name" "string" "transform_rot"'):]
    quat = [float(v) for v in _array("values", rot_block)[0].split()]
    chan = text[text.find('"name" "string" "animSetEditorChannels"'):]
    start = float(re.search(r'"start" "time" "([-\d.]+)"', chan).group(1))
    movie = text[text.find('"movieSettings"'):]
    width = int(re.search(r'"width" "int" "(\d+)"', movie).group(1))
    t0, t1 = _float("TimeRangeStartTime", text), _float("TimeRangeEndTime", text)
    fps = _float("frameRate", text)
    frames = []
    for k in range(int(round((t1 - t0) * fps))):
        t = t0 + k / fps - start
        frames.append(np.array([np.interp(t, times, values[:, a]) for a in range(3)]))
    return frames, quat, _float("fieldOfView", text), width


def _rotate(q, v):
    import numpy as np
    x, y, z, w = q
    u = np.array([x, y, z])
    v = np.asarray(v, float)
    return v + 2 * np.cross(u, np.cross(u, v) + w * v)


def footprints(frames, quat, fov, width, ground=GROUND_Z):
    """Per frame: the ground point under the image centre and the units per
    pixel on the ground plane."""
    fwd = _rotate(quat, [1, 0, 0])
    out = []
    for p in frames:
        dist = (p[2] - ground) / -fwd[2]
        centre = p + fwd * dist
        out.append((centre[:2], 2 * dist * math.tan(math.radians(fov) / 2) / width))
    return out


def _shift(a, b):
    """How far frame b's picture is moved against frame a's (phase correlation)."""
    import numpy as np
    A = np.asarray(a.convert("L").reduce(2), np.float32)
    B = np.asarray(b.convert("L").reduce(2), np.float32)
    F = np.fft.fft2(A) * np.conj(np.fft.fft2(B))
    r = np.fft.ifft2(F / (np.abs(F) + 1e-9)).real
    iy, ix = np.unravel_index(np.argmax(r), r.shape)
    h, w = A.shape
    return 2 * (ix - w if ix > w // 2 else ix), 2 * (iy - h if iy > h // 2 else iy)


def measured_upp(files, frames, pairs=8):
    """Units per pixel from neighbouring frames of a row (the lens maths is
    off by ~0.1%: the ground isn't at one height)."""
    import numpy as np
    from PIL import Image
    got = []
    # From the middle of the map out: the first rows lie over the corners'
    # repeating ground, where a shift can match one repeat off.
    mid = (len(frames) - 1) / 2
    for k in sorted(range(len(frames) - 1), key=lambda k: abs(k - mid)):
        d = frames[k + 1] - frames[k]
        if abs(d[1]) > 1 or abs(d[0]) < 1:
            continue
        a, b = Image.open(files[k]), Image.open(files[k + 1])
        dx, dy = _shift(a, b)
        px = dx - a.width if dx > 0 else dx
        if abs(dy) <= 2 and px:
            got.append(abs(d[0]) / abs(px))
        if len(got) >= pairs:
            break
    return float(np.median(got)) if got else None


def _ramp(pos, lo, hi, blend, has_lo, has_hi):
    import numpy as np
    w = np.ones_like(pos)
    if has_lo:
        w = np.minimum(w, np.clip((pos - (lo - blend)) / (2 * blend), 0, 1))
    if has_hi:
        w = np.minimum(w, np.clip(((hi + blend) - pos) / (2 * blend), 0, 1))
    return w


def _frame(path):
    """A frame as an RGB float32 array (OpenCV decodes on all cores)."""
    import cv2
    import numpy as np
    a = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if a is None:
        raise SystemExit(f"cannot read {path}")
    return np.ascontiguousarray(a[:, :, ::-1], np.float32)


def _region(im, c, upp, xa, xb, ya, yb, size):
    """The world rectangle (xa, xb, ya, yb) of frame array `im` (centre c,
    upp units per pixel) resampled to `size` (W, H), bicubic."""
    import cv2
    import numpy as np
    h, w = im.shape[:2]
    bx0, by0 = w / 2 + (xa - c[0]) / upp, h / 2 - (yb - c[1]) / upp
    bx1, by1 = w / 2 + (xb - c[0]) / upp, h / 2 - (ya - c[1]) / upp
    W, H = size
    sx, sy = (bx1 - bx0) / W, (by1 - by0) / H
    # Source pixel of each destination pixel's centre (pixel centres at +0.5).
    M = np.float32([[sx, 0, bx0 + 0.5 * sx - 0.5], [0, sy, by0 + 0.5 * sy - 0.5]])
    return cv2.warpAffine(im, M, (W, H), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def frame_gains(files, prints, log=print, scale=8, trend=0.5, reach=1.5):
    """Per frame a gain per channel (ICE's gain compensation): where two
    neighbouring frames overlap they should agree; the gains that make them,
    least squares, the mean gain 1. SFM draws each frame a little greener or
    bluer by what it shows, and water, glints and the odd streamed texture
    differ from frame to frame; without this a frame showed as a rectangle
    of its own tone.

    The overlaps' differences also add up across the map: left whole, the
    gains turned Dire 7% greener than Radiant (and with one gain for all
    channels instead, the frames' tones showed at every seam). So of each
    frame's colour (its gains against their mean) what sets it apart from
    the frames around it is kept whole, and of what it shares with them
    (their average over `reach` grid steps) the fraction `trend`
    (render.match.trend, default 0.5): the seams matched, the two sides'
    colours between SFM's own (0) and the fully matched (1)."""
    import cv2
    import numpy as np
    centres = np.array([c for c, _ in prints])
    n = len(files)
    flag = {2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}[scale]

    def load(path):
        # Decoded at 1/scale directly, every frame at once.
        return np.asarray(cv2.imread(str(path), flag)[:, :, ::-1], np.float32)
    with ThreadPoolExecutor(max_workers=8) as pool:
        small = dict(enumerate(pool.map(load, files)))

    def img(k):
        return small[k]
    xs, ys = np.unique(np.round(centres[:, 0])), np.unique(np.round(centres[:, 1]))
    rows = []
    for a in range(n):
        for b in range(a + 1, n):
            d = centres[b] - centres[a]
            ia = (abs(abs(d[0]) - np.diff(xs).mean()) < 1 and abs(d[1]) < 1) if len(xs) > 1 else False
            ib = (abs(abs(d[1]) - np.diff(ys).mean()) < 1 and abs(d[0]) < 1) if len(ys) > 1 else False
            if not (ia or ib):
                continue
            A, B = img(a), img(b)
            h, w = A.shape[:2]
            upp = prints[a][1] * scale
            sx, sy = int(round(d[0] / upp)), int(round(-d[1] / upp))
            # A's pixel (x, y) is B's (x - sx, y - sy).
            ax0, ay0 = max(0, sx), max(0, sy)
            ax1, ay1 = min(w, w + sx), min(h, h + sy)
            m = 4
            ra = A[ay0 + m:ay1 - m, ax0 + m:ax1 - m]
            rb = B[ay0 - sy + m:ay1 - sy - m, ax0 - sx + m:ax1 - sx - m]
            if ra.size == 0 or ra.shape != rb.shape:
                continue
            rows.append((a, b, ra.reshape(-1, 3).mean(0), rb.reshape(-1, 3).mean(0), ra.shape[0] * ra.shape[1]))
    gains = np.ones((n, 3), np.float32)
    if not rows:
        return gains
    for c in range(3):
        M, y = [], []
        for a, b, ma, mb, cnt in rows:
            wgt = math.sqrt(cnt) / 40
            r = np.zeros(n)
            r[a], r[b] = ma[c] * wgt, -mb[c] * wgt
            M.append(r)
            y.append(0.0)
        # Gains near 1, all together at 1 on average.
        prior = 0.05 * max(np.mean([ma[c] for _, _, ma, _, _ in rows]), 1)
        for k in range(n):
            r = np.zeros(n)
            r[k] = prior
            M.append(r)
            y.append(prior)
        g, *_ = np.linalg.lstsq(np.array(M), np.array(y), rcond=None)
        gains[:, c] = g / g.mean()
    if trend < 1:
        lg = np.log(np.maximum(gains, 1e-3))
        tone = lg - lg.mean(1, keepdims=True)
        step = np.array([np.diff(xs).mean() if len(xs) > 1 else 1.0, np.diff(ys).mean() if len(ys) > 1 else 1.0])
        near = np.exp(-(((centres[:, None, :] - centres[None, :, :]) / step) ** 2).sum(2) / (2 * reach ** 2))
        tone -= (1 - trend) * (near @ tone / near.sum(1, keepdims=True))
        gains = np.exp(lg.mean(1, keepdims=True) + tone).astype(np.float32)
        gains /= gains.mean(0)
    log(f"frame gains: {gains.min(0).round(3)} .. {gains.max(0).round(3)}")
    return gains


def _lowpass(img, sig, step=8):
    """A wide Gaussian blur (sigma `sig` px, x and y), done at 1/step size: at
    full size a 140-px blur of every frame took most of the stitch."""
    import cv2
    h, w = img.shape[:2]
    if min(sig) < 2 * step or min(h, w) < 4 * step:
        return cv2.GaussianBlur(img, (0, 0), sigmaX=sig[0], sigmaY=sig[1], borderType=cv2.BORDER_REFLECT)
    sw, sh = max(1, round(w / step)), max(1, round(h / step))
    small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), sigmaX=sig[0] * sw / w, sigmaY=sig[1] * sh / h,
                             borderType=cv2.BORDER_REFLECT)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def stitch_frames(files, prints, rect, size, blend_px=64, low_units=150.0, gains=None, progress=None):
    """The frames over world rectangle `rect` (x0, x1, y0, y1) as a picture of
    `size` (W, H) -- x and y scales may differ, as the interactive map's do.
    Two bands (as ICE's multi-band blending): each frame's detail cross-fades
    over 2 x blend_px at every border between cells (water's glints depend on
    where it is in a frame: a hard cut showed as a line), its tone (a blur of
    low_units world units) over most of the overlap, so what a frame differs
    by in tone fades out across its neighbours instead of showing its
    rectangle. Frames scaled by their gains (frame_gains) first. One row of
    frames at a time."""
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    x0, x1, y0, y1 = rect
    W, H = size
    ux, uy = (x1 - x0) / W, (y1 - y0) / H
    canvas = Image.new("RGB", (W, H))
    centres = np.array([c for c, _ in prints])
    xs, ys = np.unique(np.round(centres[:, 0])), np.unique(np.round(centres[:, 1]))[::-1]
    bx = np.concatenate([[x0], (xs[1:] + xs[:-1]) / 2, [x1]])
    by = np.concatenate([[y1], (ys[1:] + ys[:-1]) / 2, [y0]])
    # A frame's ground size: its own pixels (the render's scale) at its units
    # per pixel.
    with Image.open(files[0]) as im0:
        fw, fh = im0.size[0] * prints[0][1], im0.size[1] * prints[0][1]
    # Half the overlap, a little in: as far as both neighbours reach.
    ovx = (fw - (np.diff(xs).mean() if len(xs) > 1 else fw)) / 2 * 0.9
    ovy = (fh - (abs(np.diff(ys)).mean() if len(ys) > 1 else fh)) / 2 * 0.9
    hx, hy = min(blend_px * ux, ovx), min(blend_px * uy, ovy)
    lx, ly = max(hx, ovx), max(hy, ovy)
    col_x = x0 + (np.arange(W) + 0.5) * ux
    sig = (low_units / ux, low_units / uy)
    prev, prev_top = None, 0
    for j, yc in enumerate(ys):
        if progress:
            progress(j / len(ys))
        top, bottom = by[j], by[j + 1]
        if bottom >= y1 or top <= y0:
            continue
        st = min(top + (ly if j else 0), y1)
        sb = max(bottom - (ly if j < len(ys) - 1 else 0), y0)
        r0, r1 = int(round((y1 - st) / uy)), int(round((y1 - sb) / uy))
        if r1 <= r0:
            continue
        row_y = y1 - (np.arange(r0, r1) + 0.5) * uy
        wyl = _ramp(row_y, bottom, top, ly, j < len(ys) - 1, j > 0)
        wyh = _ramp(row_y, bottom, top, hy, j < len(ys) - 1, j > 0)
        strip = np.zeros((r1 - r0, W, 3), np.float32)
        row = [k for k, (c, _u) in enumerate(prints) if abs(round(c[1]) - yc) <= 0.5]
        with ThreadPoolExecutor(max_workers=min(8, len(row) or 1)) as pool:
            loaded = dict(zip(row, pool.map(lambda k: _frame(files[k]), row)))
        for k, (path, (c, upp)) in enumerate(zip(files, prints)):
            if k not in loaded:
                continue
            i = int(np.argmin(abs(xs - round(c[0]))))
            xa, xb = max(bx[i] - (lx if i else 0), x0), min(bx[i + 1] + (lx if i < len(xs) - 1 else 0), x1)
            c0, c1 = int(round((xa - x0) / ux)), int(round((xb - x0) / ux))
            if c1 <= c0:
                continue
            part = _region(loaded.pop(k), c, upp, x0 + c0 * ux, x0 + c1 * ux,
                           y1 - r1 * uy, y1 - r0 * uy, (c1 - c0, r1 - r0))
            if gains is not None:
                part *= gains[k].astype(np.float32)
            low = _lowpass(part, sig)
            wxl = _ramp(col_x[c0:c1], bx[i], bx[i + 1], lx, i > 0, i < len(xs) - 1)
            wxh = _ramp(col_x[c0:c1], bx[i], bx[i + 1], hx, i > 0, i < len(xs) - 1)
            # low * wl + (part - low) * wh = part * wh + low * (wl - wh), in
            # place: temporaries of a whole frame cost more than the work.
            wh = (wyh[:, None] * wxh[None, :]).astype(np.float32)[:, :, None]
            wl = (wyl[:, None] * wxl[None, :]).astype(np.float32)[:, :, None]
            np.multiply(part, wh, out=part)
            np.subtract(wl, wh, out=wl)
            np.multiply(low, wl, out=low)
            dst = strip[:, c0:c1]
            dst += part
            dst += low
        if prev is not None:
            ov0 = max(0, r0 - prev_top)
            n = min(prev.shape[0] - ov0, strip.shape[0])
            strip[:n] += prev[ov0:ov0 + n]
            canvas.paste(Image.fromarray(np.clip(prev[:ov0] + 0.5, 0, 255).astype(np.uint8)), (0, prev_top))
        prev, prev_top = strip, r0
    if prev is not None:
        canvas.paste(Image.fromarray(np.clip(prev + 0.5, 0, 255).astype(np.uint8)), (0, prev_top))
    return canvas


def read_session(tools, path):
    """A session as KeyValues2 text (SFM saves binary; dmxconvert converts)."""
    path = Path(path)
    if b"keyvalues2" in path.read_bytes()[:64]:
        return path.read_text(encoding="utf-8")
    out = path.with_name(path.stem + "_totext.dmx")
    rc, msg = core.proton_run(tools, "dmxconvert.exe", ["-i", core.winpath(path), "-o", core.winpath(out),
                                                       "-oe", "keyvalues2"])
    if rc != 0 or not out.exists():
        raise SystemExit(f"dmxconvert could not read {path} (rc {rc})")
    return out.read_text(encoding="utf-8")


# --- what a render is stitched into -----------------------------------------------------

# The full picture's default: the square the interactive map's 7.33+ picture
# covers, map_w px, its edges these world coordinates (y top to bottom).
FULL = {"size": 20480, "x": [-10829.42, 11487.75], "y": [11351.48, -10939.96]}


def full_rect(tools):
    """The full picture: its world rectangle (x0, x1, y0, y1) and its size in
    px, from the settings, else FULL."""
    ia = tools.settings["render"]["full"]
    c = {"size": ia.get("size"), "x": ia.get("x"), "y": ia.get("y")}
    if not (c["size"] and c["x"] and c["y"]):
        c = FULL
    (x0, x1), (ytop, ybot) = c["x"], c["y"]
    return (x0, x1, ybot, ytop), int(c["size"])


def products(tools, addon, map_name):
    """The pictures a render makes, from the settings: [{name, rect, size or
    upp}]; their union is what the camera has to cover."""
    from . import mapinfo
    r = tools.settings["render"]
    out = []
    if r["full"].get("enabled", True):
        rect, size = full_rect(tools)
        out.append({"name": "full", "rect": rect, "size": (size, size)})
    bd = r["bounds"]
    if bd.get("enabled", True):
        src = bd.get("source", "minimap")
        b = mapinfo.bounds(tools, addon, map_name)
        rect = tuple(bd["rect"]) if src == "custom" and bd.get("rect") else \
            b.get(src) or b.get("overview") or b.get("gridnav") or map_rect(tools, addon, map_name)
        out.append({"name": f"bounds_{src}", "rect": tuple(rect), "upp": bd.get("upp") or 0})
    return out


def cover(prods, margin=64.0):
    """The rectangle every product lies in."""
    if not prods:
        return None
    rs = [p["rect"] for p in prods]
    return (min(r[0] for r in rs) - margin, max(r[1] for r in rs) + margin,
            min(r[2] for r in rs) - margin, max(r[3] for r in rs) + margin)


def sharpest(files, frames, log=print):
    """Of each run of frames at one camera position (session_text) the
    sharpest -- the first position's first frame left out (drawn before the
    session's models loaded). Indices of the frames kept."""
    import cv2
    import numpy as np
    groups, cur = [], [0]
    for k in range(1, len(frames)):
        if abs(frames[k] - frames[k - 1]).max() < 1e-3:
            cur.append(k)
        else:
            groups.append(cur)
            cur = [k]
    groups.append(cur)
    if len(groups[0]) > 1:
        groups[0] = groups[0][1:]

    def sharp(k):
        g = cv2.imread(str(files[k]), cv2.IMREAD_REDUCED_GRAYSCALE_2).astype(np.float32)
        return float(np.abs(g - cv2.GaussianBlur(g, (0, 0), 1.5)).mean())
    keep, swapped = [], 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for g in groups:
            if len(g) == 1:
                keep.append(g[0])
                continue
            scores = list(pool.map(sharp, g))
            best = g[int(np.argmax(scores))]
            swapped += best != g[-1]
            keep.append(best)
    if any(len(g) > 1 for g in groups[1:]):
        log(f"{len(keep)} positions; {swapped} took an earlier, sharper frame than the last")
    return keep


def blend_shadows(files, other_dir, strength, log=print, progress=no_progress):
    """Each frame and its shadowless twin (the same name in other_dir)
    blended: the shadows at `strength` of their rendered depth. Written
    beside the frames (<frames>_blend); returns the blended files."""
    import cv2
    import numpy as np
    other = Path(other_dir)
    if not all((other / f.name).exists() for f in files):
        raise SystemExit(f"the shadowless pass is missing or incomplete in {other}: render it (Prepare writes "
                         f"its session) or set the shadows to 100 %")
    out = Path(files[0]).parent.with_name(Path(files[0]).parent.name + "_blend")
    out.mkdir(exist_ok=True)
    log(f"shadows at {strength * 100:.0f} %")

    def one(f):
        a = cv2.imread(str(f), cv2.IMREAD_COLOR).astype(np.float32)
        b = cv2.imread(str(other / f.name), cv2.IMREAD_COLOR).astype(np.float32)
        dst = out / f.name
        cv2.imwrite(str(dst), np.clip(b + strength * (a - b) + 0.5, 0, 255).astype(np.uint8),
                    [cv2.IMWRITE_PNG_COMPRESSION, 1])
        return dst
    progress("blending the shadows", 0.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, files))


def stitch(tools, frames_dir, out_dir, addon="", map_name="dota", session=None, prods=None, log=print,
           progress=no_progress):
    """The exported frames into each product picture, in out_dir:
    <map>_<product>.png."""
    import importlib.util
    if not (importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL")):
        raise SystemExit("stitching needs numpy and Pillow (python-numpy, python-pillow)")
    from PIL import Image
    frames_dir, out_dir = Path(frames_dir), Path(out_dir)
    text = read_session(tools, session) if session else (frames_dir / "session_text.dmx").read_text(encoding="utf-8")
    frames, quat, fov, width = camera_path(text)
    files = sorted(p for p in frames_dir.glob("*.png"))
    if len(files) != len(frames):
        raise SystemExit(f"{len(files)} frames in {frames_dir}, the session exports {len(frames)}")
    strength = shadow_strength(tools)
    if strength < 1:
        files = blend_shadows(files, noshadow_dir(frames_dir), strength, log, progress)
    # The frames' own width: the export dialog sets it (the render's scale),
    # not the session.
    with Image.open(files[0]) as im0:
        width = im0.size[0]
    keep = sharpest(files, frames, log)
    frames, files = [frames[k] for k in keep], [files[k] for k in keep]
    prints = footprints(frames, quat, fov, width)
    m = measured_upp(files, frames)
    lens = prints[0][1]
    log(f"units per pixel in a frame: lens {lens:.4f}, measured {m and round(m, 4)}")
    if m and abs(m / lens - 1) < 0.01:
        prints = [(c, m) for c, _ in prints]
    native = prints[0][1]
    scale = render_scale(tools)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Tone: auto (neighbouring frames' tones matched to each other), off
    # (the frames as SFM drew them).
    gains = None
    if tools.settings["render"]["match"].get("mode", "auto") != "off":
        progress("matching the frames' tones", 0.0)
        gains = frame_gains(files, prints, log, trend=float(tools.settings["render"]["match"].get("trend", 0.5)))
    made = []
    prods = prods if prods is not None else products(tools, addon, map_name)
    # At another scale: fixed sizes and given resolutions scaled with it
    # (the frames' own already are).
    if scale != 1:
        prods = [dict(p, size=(round(p["size"][0] * scale), round(p["size"][1] * scale)) if p.get("size") else None,
                      upp=(p["upp"] / scale) if p.get("upp") else 0) for p in prods]
    # Each picture's share of the rest, by its pixels: stitching 85%,
    # saving the rest.
    sizes = []
    for p in prods:
        x0, x1, y0, y1 = p["rect"]
        upp = p.get("upp") or native
        sz = p.get("size") or (round((x1 - x0) / upp), round((y1 - y0) / upp))
        sizes.append(sz[0] * sz[1])
    done, total, base = 0.0, float(sum(sizes)) or 1.0, 0.1
    for p, px in zip(prods, sizes):
        share = (1 - base) * px / total
        at = base + (1 - base) * done / total
        done += px
        x0, x1, y0, y1 = p["rect"]
        upp = p.get("upp") or native
        size = p.get("size") or (round((x1 - x0) / upp), round((y1 - y0) / upp))
        log(f"{p['name']}: x {x0:.0f}..{x1:.0f}, y {y0:.0f}..{y1:.0f} at {size[0]} x {size[1]} px")
        img = stitch_frames(files, prints, p["rect"], size, gains=gains,
                            progress=lambda f, at=at, share=share, n=p["name"]: progress(f"stitching {n}",
                                                                                         at + share * 0.85 * f))
        from . import pngfast
        tag = "" if scale == 1 else f"_{scale:g}x"
        path = out_dir / f"{map_name}_{p['name']}{tag}.png"
        progress(f"saving {path.name}", at + share * 0.92)
        pngfast.save(img, path)
        log(f"{path}")
        made.append(path)
        if scale > 1:
            # And at the normal scale.
            img = img.resize((round(img.size[0] / scale), round(img.size[1] / scale)), Image.LANCZOS)
            path = out_dir / f"{map_name}_{p['name']}.png"
            progress(f"saving {path.name}", at + share * 0.96)
            pngfast.save(img, path)
            log(f"{path}")
            made.append(path)
        sm = tools.settings["render"].get("small", {})
        if sm.get("enabled", True) and p["name"].startswith("bounds"):
            from . import tiles
            n = int(sm.get("size", 8192))
            k = n / max(img.size)
            small = img.resize((round(img.size[0] * k), round(img.size[1] * k)), Image.LANCZOS)
            sp = tiles.save_web(small, out_dir / f"{map_name}_{p['name']}_{n}", sm.get("format", "webp"),
                                sm.get("quality") or None)
            log(f"{sp}")
            made.append(sp)
    progress("done", 1.0)
    return made
