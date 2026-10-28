"""The tools folder: Steam's data, the Windows depots, links to the native
install, launching through Proton, the Steam shortcut.

Steam installs Dota 2 natively and the Workshop Tools only exist for Windows.
The tools folder holds only what is Windows-specific -- the Windows game
binaries (depots 373302, 373303) and the Workshop Tools DLC (depot 381450: the
tools and their content sources), fetched with DepotDownloader at exactly the
manifests of the build the native install is on. Everything else, the ~76 GB
of shared game content, comes from the native install: files of 1 MB and over
as symlinks, the small ones as copies, so the tools never write into Steam's
files and Steam never sees anything to repair. game/dota_addons and
content/dota_addons are links to the native install's.

Standard library only.
"""
import binascii
import functools
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import urllib.request
import zipfile
from pathlib import Path

APP = 570
# Windows-specific depots: shaders and game binaries (373302, 373303) and the
# Workshop Tools (381450). The shared content depots (373301, 381451-381455)
# come from the native install.
DEPOTS = [373302, 373303, 381450]
# The game binaries download anonymously. The tools DLC needs an account that
# has it (it is free, but anonymous Steam has no license for it): a login
# DepotDownloader remembers, or Steam's own download (import_client).
LOGIN_DEPOTS = {381450}
DEPOTDOWNLOADER = ("https://github.com/SteamRE/DepotDownloader/releases/download/"
                   "DepotDownloader_3.4.0/DepotDownloader-linux-x64.zip")
DEFAULT_DIR = Path.home() / ".local" / "share" / "dota2-workshop-tools"
STEAM_ROOT = Path.home() / ".local" / "share" / "Steam"
APP_DIR = Path(__file__).resolve().parents[1]      # this repository (or its installed copy)
APPIMAGE = os.environ.get("APPIMAGE", "")          # the AppImage this runs from, if one
# Files this size and over are linked; smaller ones copied.
LINK_MIN = 1 << 20
# Native paths never linked: Linux binaries, the native install's leftover
# Windows binaries and tools content (the depots provide current ones). The
# addon folders are linked as whole folders instead.
SKIP_DIRS = {"game/bin/linuxsteamrt64", "game/dota/bin/linuxsteamrt64", "game/bin/win64",
             "game/dota/bin/win64", "content", "game/dota_addons"}
SKIP_FILES = {"game/dota.sh"}
SHARED_DIRS = ["game/dota_addons", "content/dota_addons"]
# Addon folders that are not addons.
NOT_ADDONS = {"vpks"}

DEFAULT_SETTINGS = {
    "steam": str(STEAM_ROOT),
    "native": "",                 # empty: found from Steam's libraries
    "proton": str(STEAM_ROOT / "steamapps" / "common" / "Proton - Experimental" / "proton"),
    "username": "",
    "container": True,            # run Proton in the Steam Linux Runtime, as Steam does
    "auto_update": True,          # the launcher updates before starting the tools
    # The terminal for the VConsole client (terminal.py; empty: the desktop's own).
    "terminal": "",
    "launch": {"addon": "", "map": "", "vconsole": True, "extra": "", "tray": True,
               "vconsole_terminal": False},
    # Old builds (builds.py): where they are downloaded, the one the launcher
    # starts (empty: the tools folder, the current build), and the depots of
    # each app a build is downloaded with (empty: builds.APPS's).
    "builds": {"folder": str(DEFAULT_DIR.parent / "dota2-builds"), "active": "", "depots": {}},
    "render": {"render_addon": "dota2tools_render", "overlap": 0.15,
               # The full picture: a square, sized and placed as devilesk's
               # dota-interactive-map has it -- from its src/js/mapConstants.js
               # (repo), unless given here (size, x = [left, right], y = [top, bottom]).
               "full": {"enabled": True, "size": 0, "x": [], "y": []},
               # The map cut to its own bounds: minimap (dota_minimap_boundary),
               # world (world_bounds), gridnav, overview, or custom (rect); at
               # upp units per pixel (0: the render's own, ~1.09).
               "bounds": {"enabled": True, "source": "minimap", "rect": [], "upp": 0},
               # A smaller copy of the bounds picture for the web (size px, the
               # longer side; format jpg, webp or png; quality 0: the format's own).
               "small": {"enabled": True, "size": 8192, "format": "webp", "quality": 0},
               # Interactive map tiles (tiles.py): format, quality (0: the format's).
               "tiles": {"format": "jpg", "quality": 0, "tile": 256},
               # Tone: auto (neighbouring frames' tones matched), off (the frames as
               # SFM drew them). trend: how much of the colour the matching
               # carries across the map (0: each side SFM's own, 1: all of it).
               "match": {"mode": "auto", "trend": 0.5},
               # Console commands for the tools when rendering: the texture
               # streaming picks mips for the camera's distance, not its 1-degree
               # lens, and its dynamic budget lowers them further under memory
               # pressure, differently every render: both would leave parts of
               # the map blurred. Parallax mapping, seen from that far, smeared the
               # stone paths that have it.
               "convars": "+r_texture_stream_mip_bias -8 +r_texture_budget_dynamic 0 +r_texture_pool_size 6144 "
                           "+r_max_texture_pool_size 6144 +r_dota_allow_parallax_mapping 0",
               # The sun for the render only (pitch, yaw in degrees; empty: the
               # map's own): a copy of the map rebuilt from its source with it.
               # Pixels per unit against the default (frames of 3840 x 2160): 2
               # renders frames twice the size and saves each picture at 2x and
               # 1x; 0.5 renders and saves everything at half size.
               "scale": 1.0,
               # Frames per camera position, the sharpest kept: the texture
               # streaming falls behind now and then in a long export.
               "repeat": 2,
               # Times the shot is played in SFM before the export, for the
               # texture streaming to load what each position needs.
               "preload": 1,
               "lighting": False,
               "sun": {"pitch": "", "yaw": "", "on": True},
               # The sun's shadows' strength, 0..1: below 1 a second pass without
               # them is exported and the two blended.
               "shadows": 1.0,
               "prerun_enabled": True,
               "prerun": ["builtin:flies", "builtin:tools_materials", "builtin:edge_trees", "builtin:corners", "builtin:lava"],
               # prerun/edge_trees.py: the edge trees' leaves an oak's (tint: recoloured)
               "edge_trees": {"tint": "", "shade": 0.7, "unlit": False},
               # prerun/corners.py: the black fields beyond the corners as models on
               # the map's ground, lift units above the black mesh.
               "corners": {"lift": 2.0},
               # prerun/lava.py: the lava copied as unlit models, lift units up.
               "lava": {"lift": 1.0, "brightness": 1.5}},
}


# --- Steam's files ----------------------------------------------------------

def vdf_text(text):
    """Steam's text KeyValues (libraryfolders.vdf, appmanifest_*.acf)."""
    toks, i = [], 0
    while i < len(text):
        c = text[i]
        if c == '"':
            j = i + 1
            buf = []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            toks.append("".join(buf))
            i = j + 1
        elif c in "{}":
            toks.append(c)
            i += 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
        else:
            i += 1

    def block(k):
        out = {}
        while k < len(toks) and toks[k] != "}":
            key = toks[k]
            if toks[k + 1] == "{":
                out[key], k = block(k + 2)
                k += 1
            else:
                out[key] = toks[k + 1]
                k += 2
        return out, k
    return block(0)[0]


def appinfo(steam, apps=None):
    """{appid: KeyValues} from Steam's appinfo cache (format 29 with a string
    table, or 28 without): the apps named, or every app it knows."""
    b = (Path(steam) / "appcache" / "appinfo.vdf").read_bytes()
    magic, = struct.unpack_from("<I", b, 0)
    v29 = magic == 0x07564429
    strings, pos = [], 8
    if v29:
        stoff, = struct.unpack_from("<q", b, 8)
        n, = struct.unpack_from("<I", b, stoff)
        p = stoff + 4
        for _ in range(n):
            e = b.index(b"\0", p)
            strings.append(b[p:e].decode("utf-8", "replace"))
            p = e + 1
        pos, end = 16, stoff
    else:
        end = len(b)

    def kv(p):
        d = {}
        while True:
            t = b[p]
            p += 1
            if t == 8:
                return d, p
            if v29:
                k = strings[struct.unpack_from("<I", b, p)[0]]
                p += 4
            else:
                e = b.index(b"\0", p)
                k = b[p:e].decode("utf-8", "replace")
                p = e + 1
            if t == 0:
                v, p = kv(p)
            elif t == 1:
                e = b.index(b"\0", p)
                v = b[p:e].decode("utf-8", "replace")
                p = e + 1
            elif t in (2, 3):
                v = struct.unpack_from("<i" if t == 2 else "<f", b, p)[0]
                p += 4
            elif t in (7, 10):
                v = struct.unpack_from("<Q" if t == 7 else "<q", b, p)[0]
                p += 8
            else:
                raise ValueError(f"appinfo: unknown type {t}")
            d[k] = v
    out = {}
    while pos < end:
        appid, size = struct.unpack_from("<II", b, pos)
        if appid == 0:
            break
        if apps is None or appid in apps:
            out[appid] = kv(pos + 8 + 4 + 4 + 8 + 20 + 4 + 20)[0].get("appinfo", {})
        pos += 8 + size
    return out


def find_native(steam, native=""):
    """The native Dota 2 install: (folder, appmanifest)."""
    steam = Path(steam)
    libs = vdf_text((steam / "steamapps" / "libraryfolders.vdf").read_text())
    for lib in libs.get("libraryfolders", {}).values():
        if str(APP) in lib.get("apps", {}):
            apps = Path(lib["path"]) / "steamapps"
            man = vdf_text((apps / f"appmanifest_{APP}.acf").read_text())["AppState"]
            return Path(native) if native else apps / "common" / man["installdir"], man
    raise SystemExit("Dota 2 is not installed in any Steam library")


def target(steam, native=""):
    """What the tools must match: the native install's build, branch and the
    Windows depots' manifests for it."""
    native, man = find_native(steam, native)
    uc = man.get("UserConfig", {})
    branch = uc.get("BetaKey") or uc.get("betakey") or "public"
    depots = appinfo(steam, {APP})[APP]["depots"]
    current = str(depots["branches"].get(branch, {}).get("buildid", ""))
    manifests = {}
    for d in DEPOTS:
        m = depots[str(d)]["manifests"]
        gid = m.get(branch) or m["public"]
        manifests[str(d)] = str(gid["gid"] if isinstance(gid, dict) else gid)
    return {"native": str(native), "buildid": man.get("buildid"), "branch": branch,
            "current": current, "installed": man.get("StateFlags") == "4", "manifests": manifests}


def steam_running():
    return subprocess.call(["pgrep", "-x", "steam"], stdout=subprocess.DEVNULL) == 0


@functools.lru_cache(maxsize=8)
def _vpk_index(path, _mtime):
    """A VPK's directory (version 1 or 2): ({path: (archive, offset, length,
    preload bytes)}, where archive 0x7FFF data starts), parsed once."""
    b = Path(path).read_bytes()
    _sig, ver, tree = struct.unpack_from("<III", b, 0)
    pos = 28 if ver == 2 else 12
    end = pos + tree
    out = {}

    def cstr(p):
        e = b.index(b"\0", p)
        return b[p:e].decode("utf-8", "replace"), e + 1
    while pos < end:
        ext, pos = cstr(pos)
        if not ext:
            break
        while True:
            folder, pos = cstr(pos)
            if not folder:
                break
            while True:
                name, pos = cstr(pos)
                if not name:
                    break
                _crc, preload, ai, off, ln, _ = struct.unpack_from("<IHHIIH", b, pos)
                pos += 18
                out[(f"{folder}/" if folder.strip() else "") + f"{name}.{ext}"] = (ai, off, ln, b[pos:pos + preload])
                pos += preload
    return out, end


def vpk_list(path):
    """The files in a VPK: {path: (archive index, offset, length, preload)}."""
    return _vpk_index(str(path), Path(path).stat().st_mtime)[0]


@functools.lru_cache(maxsize=8)
def _vpk_names(path, mtime):
    """A VPK's paths by their lower-case form."""
    return {k.lower(): k for k in _vpk_index(path, mtime)[0]}


def vpk_read(dir_path, rel):
    """One file out of a VPK (its directory file and archives), or None."""
    dir_path = Path(dir_path)
    key = (str(dir_path), dir_path.stat().st_mtime)
    index, end = _vpk_index(*key)
    name = _vpk_names(*key).get(rel.lower().replace("\\", "/"))
    if name is None:
        return None
    ai, off, ln, data = index[name]
    archive = dir_path if ai == 0x7FFF else dir_path.with_name(dir_path.name.replace("_dir.vpk", f"_{ai:03d}.vpk"))
    with open(archive, "rb") as f:
        f.seek(end + off if ai == 0x7FFF else off)
        return data + f.read(ln)


# --- the tools folder -------------------------------------------------------

def merged(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


class Tools:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_DIR).expanduser().resolve()
        self.meta = self.root / ".dota2_tools"
        self.settings_file = self.meta / "settings.json"
        self.state_file = self.meta / "state.json"
        self.links_file = self.meta / "links.json"

    def load(self, f, default):
        return json.loads(f.read_text()) if f.exists() else default

    def save(self, f, data):
        self.meta.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(f)

    @property
    def installed(self):
        return self.state_file.exists() and (self.win64 / "dota2.exe").exists()

    @property
    def settings(self):
        """The settings file over the defaults (read anew each time)."""
        return merged(DEFAULT_SETTINGS, self.load(self.settings_file, {}))

    def save_settings(self, s):
        self.save(self.settings_file, s)

    @property
    def state(self):
        return self.load(self.state_file, {})

    @property
    def steam(self):
        return Path(self.settings["steam"])

    @property
    def native(self):
        s = self.settings
        return find_native(s["steam"], s.get("native", ""))[0]

    @property
    def win64(self):
        return self.root / "game" / "bin" / "win64"

    def target(self):
        s = self.settings
        return target(s["steam"], s.get("native", ""))

    def status(self):
        """(target, state, up to date)."""
        tgt, state = self.target(), self.state
        return tgt, state, state.get("buildid") == tgt["buildid"] and state.get("manifests") == tgt["manifests"]

    @property
    def game_root(self):
        """The game the launcher starts: the active old build (builds.py),
        else the tools folder."""
        active = self.settings.get("builds", {}).get("active", "")
        return Path(active) if active and Path(active).is_dir() else self.root

    def addons(self, root=None):
        """The addons both clients see (native game/ and content/ dota_addons),
        or those of another build's folder."""
        names = set()
        for rel in SHARED_DIRS:
            d = Path(root or self.root) / rel
            if d.is_dir():
                names |= {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")}
        return sorted(names - NOT_ADDONS)

    def maps(self, addon="", root=None):
        """Maps of the addon (game/dota_addons/<addon>/maps), else the base
        game's (game/dota/maps)."""
        root = Path(root or self.root)
        d = (root / "game" / "dota_addons" / addon / "maps") if addon else (root / "game" / "dota" / "maps")
        return sorted(p.stem for p in d.glob("*.vpk")) if d.is_dir() else []


    def source_maps(self, addon):
        """The addon's maps by name, its sources (content/.../maps/*.vmap)
        and compiled ones (game/.../maps/*.vpk) both."""
        names = set()
        for rel, ext in (("content", ".vmap"), ("game", ".vpk")):
            d = self.root / rel / "dota_addons" / addon / "maps"
            if d.is_dir():
                names |= {p.stem for p in d.glob(f"*{ext}")}
        return sorted(names)


TEMPLATE_ADDON = "addon_template"
ADDON_NAME = re.compile(r"^[a-z0-9_]+$")


def _text(path):
    """A text file as Valve wrote it: (text, encoding) -- UTF-16 with its BOM
    (the resource files), else UTF-8."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    return data.decode("utf-8", "replace"), "utf-8"


def create_addon(tools, folder, title="", map_name="", base=TEMPLATE_ADDON, base_map=""):
    """A new addon `folder` (game and content) copied from addon `base` (Dota's
    addon_template by default), as Dota's own tools launcher makes one: its
    map `base_map` (the base's first) renamed `map_name` (default: the
    folder's name), addoninfo.txt naming it and marked playable rather than
    a template, the addon's display name (resource/addon_english.txt) `title`.
    Returns the new game and content folders."""
    folder, map_name = folder.strip(), (map_name or folder).strip()
    if not ADDON_NAME.match(folder):
        raise ValueError("the folder name: lowercase letters, digits and _ only")
    if not ADDON_NAME.match(map_name):
        raise ValueError("the map name: lowercase letters, digits and _ only")
    src = {k: tools.root / k / "dota_addons" / base for k in ("game", "content")}
    dst = {k: tools.root / k / "dota_addons" / folder for k in ("game", "content")}
    if not any(p.is_dir() for p in src.values()):
        raise ValueError(f"no addon {base}")
    for p in dst.values():
        if p.exists():
            raise ValueError(f"{p} exists already")
    maps = tools.source_maps(base)
    base_map = base_map or (maps[0] if maps else "")
    for k in ("game", "content"):
        if src[k].is_dir():
            # The tools' cache of the base's read-only assets is the base's
            # own; the tools write the new addon's.
            shutil.copytree(src[k], dst[k], symlinks=True,
                            ignore=shutil.ignore_patterns("readonly_tools_asset_info.bin", "*.vpk.lock"))
        else:
            dst[k].mkdir(parents=True)
    if base_map and base_map != map_name:
        for k in ("game", "content"):
            d = dst[k] / "maps"
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.name == base_map or p.name.startswith(base_map + "."):
                    p.rename(p.with_name(map_name + p.name[len(base_map):]))
    info = dst["game"] / "addoninfo.txt"
    text = info.read_text(encoding="utf-8", errors="replace") if info.exists() else '"AddonInfo"\n{\n}\n'
    text = re.sub(r'(?im)^\s*"?IsTemplate"?\s+"?\d"?\s*\n', "", text)
    names = [m for m in tools.source_maps(folder)]
    listed = " ".join([map_name] + [m for m in names if m != map_name])
    if re.search(r'(?im)^\s*"?maps"?\s', text):
        text = re.sub(r'(?im)^(\s*"?maps"?\s+)"?[^"\n]*"?', lambda mt: f'{mt.group(1)}"{listed}"', text, count=1)
    else:
        text = text.replace("{", f'{{\n\t"maps"\t\t\t"{listed}"', 1)
    if re.search(r'(?im)^\s*"?IsPlayable"?\s', text):
        text = re.sub(r'(?im)^(\s*"?IsPlayable"?\s+)"?\d"?', r'\1"1"', text, count=1)
    else:
        text = text.rstrip().rstrip("}") + '\t"IsPlayable"\t"1"\n}\n'
    info.write_text(text, encoding="utf-8")
    res = dst["game"] / "resource" / "addon_english.txt"
    if title:
        if res.exists():
            body, enc = _text(res)
        else:
            res.parent.mkdir(parents=True, exist_ok=True)
            body, enc = '"lang"\n{\n\t"Language"\t\t"English"\n\t"Tokens"\n\t{\n\t}\n}\n', "utf-16"
        esc = title.replace('"', "'")
        if re.search(r'"addon_game_name"', body):
            body = re.sub(r'("addon_game_name"\s+)"[^"]*"', lambda mt: f'{mt.group(1)}"{esc}"', body, count=1)
        else:
            body = re.sub(r'("Tokens"\s*\{)', lambda mt: f'{mt.group(1)}\n\t\t"addon_game_name"\t\t"{esc}"', body, count=1)
        res.write_bytes(body.encode(enc))
    return dst["game"], dst["content"]


# --- downloading ------------------------------------------------------------

def depotdownloader(tools):
    exe = tools.meta / "depotdownloader" / "DepotDownloader"
    if not exe.exists():
        exe.parent.mkdir(parents=True, exist_ok=True)
        zpath = exe.parent / "dd.zip"
        urllib.request.urlretrieve(DEPOTDOWNLOADER, zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(exe.parent)
        zpath.unlink()
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


class TerminalUI:
    """Where DepotDownloader talks: the terminal itself (the CLI). The GUI
    passes its own (gui.DDRunner)."""
    def say(self, text):
        print(text, flush=True)

    def run(self, cmd):
        return subprocess.call(cmd)


# Where the Steam client's download_depot puts a depot: steamapps/content
# beside the client's binaries (ubuntu12_32; steamrt64 for the 64-bit
# client; the others in case), or Steam's own steamapps.
CLIENT_DIRS = ["steamrt64", "steamrt32", "ubuntu12_64", "ubuntu12_32", ""]


def client_folder(tools, depot, app=APP):
    """Where the Steam client's console command download_depot put a depot:
    the first of the client's folders that has it, else the 32-bit client's."""
    places = [tools.steam / d / "steamapps" / "content" / f"app_{app}" / f"depot_{depot}" for d in CLIENT_DIRS]
    return next((f for f in places if f.is_dir()), places[CLIENT_DIRS.index("ubuntu12_32")])


def client_ready(tools, depot, manifest, app=APP, asked=None):
    """The folder of a finished download_depot of this manifest, or None:
    the download is there and that manifest is the one asked for (`asked`)."""
    folder = client_folder(tools, depot, app)
    return folder if folder.is_dir() and asked == str(manifest) else None


def client_command(depot, manifest, app=APP):
    return f"download_depot {app} {depot} {manifest}"


class NeedsClient(Exception):
    """A depot that has to come from the Steam client's console."""
    def __init__(self, depot, manifest, folder, app=APP, again="update again"):
        super().__init__(f"run in the Steam client's console (steam://open/console):\n  "
                         f"{client_command(depot, manifest, app)}\nwait for 'Depot download complete', then {again}")
        self.command = client_command(depot, manifest, app)
        self.folder = folder


def import_client(tools, depot, manifest, state, ui):
    """The depot as the running Steam client downloaded it (no login of our
    own): moved into the tools folder, and the files of the depot's previous
    version that this one no longer has removed. The download is used once,
    for the manifest it was asked for, then deleted."""
    marker = tools.meta / f"client_{depot}.json"
    src = client_ready(tools, depot, manifest, asked=tools.load(marker, {}).get("manifest"))
    if src is None:
        tools.save(marker, {"manifest": manifest})
        raise NeedsClient(depot, manifest, client_folder(tools, depot))
    files = []
    for dirpath, _, names in os.walk(src):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), src)
            dst = tools.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink():
                dst.unlink()
            os.replace(os.path.join(dirpath, name), dst)
            files.append(rel)
    old = set(state.get("client_files", {}).get(str(depot), [])) - set(files)
    for rel in old:
        p = tools.root / rel
        if p.is_file() and not p.is_symlink():
            p.unlink()
    shutil.rmtree(src, ignore_errors=True)
    if marker.exists():
        marker.unlink()
    state.setdefault("client_files", {})[str(depot)] = sorted(files)
    ui.say(f"imported depot {depot} from the Steam client: {len(files)} files, {len(old)} removed")


def download(tools, tgt, state, ui, qr=False, password=None, use_client=False):
    """The Windows depots at the native build's manifests, those that changed
    since the last update. DepotDownloader keeps the manifests it installed
    and fetches only the changed files."""
    s = tools.settings
    have = state.setdefault("manifests", {})
    for depot, manifest in tgt["manifests"].items():
        if have.get(depot) == manifest:
            continue
        login = int(depot) in LOGIN_DEPOTS
        if login and (use_client or (not s.get("username") and not qr)):
            import_client(tools, depot, manifest, state, ui)
            have[depot] = manifest
            continue
        cmd = [str(depotdownloader(tools)), "-app", str(APP), "-depot", depot, "-manifest", manifest,
               "-dir", str(tools.root), "-os", "windows", "-osarch", "64"]
        if tgt["branch"] != "public":
            cmd += ["-branch", tgt["branch"]]
        if login:
            # The login is remembered under the account name, which later
            # updates give (a QR login included).
            if qr:
                cmd += ["-qr", "-remember-password"]
            else:
                cmd += ["-username", s["username"], "-remember-password"]
                if password:
                    cmd += ["-password", password]
        ui.say(f"== depot {depot} manifest {manifest}")
        rc = ui.run(cmd)
        if rc != 0:
            raise SystemExit(f"DepotDownloader failed on depot {depot} (rc {rc})")
        have[depot] = manifest
        marker = tools.meta / f"client_{depot}.json"
        if marker.exists():
            marker.unlink()
        if login:
            qr, password = False, None    # one login per run


def unlink_links(tools):
    """Our symlinks out, before DepotDownloader writes: a file a Windows depot
    adds where a link stands would otherwise be written into the native
    install."""
    links = tools.load(tools.links_file, {})
    for rel, entry in list(links.items()):
        p = tools.root / rel
        if entry["kind"] == "link" and p.is_symlink():
            p.unlink()
            del links[rel]
    tools.save(tools.links_file, links)


def link(tools, native, ui=None):
    """Every file of the native install the Windows depots don't provide:
    1 MB and over linked, smaller copied (and copied again when the native
    file changes). Entries whose native file is gone are removed."""
    ui = ui or TerminalUI()
    native = Path(native)
    links = tools.load(tools.links_file, {})
    seen = set()
    made = copied = 0
    for dirpath, dirnames, filenames in os.walk(native):
        reld = os.path.relpath(dirpath, native)
        reld = "" if reld == "." else reld
        dirnames[:] = [d for d in dirnames if (f"{reld}/{d}" if reld else d) not in SKIP_DIRS]
        for name in filenames:
            rel = f"{reld}/{name}" if reld else name
            if rel in SKIP_FILES or name.endswith(".mdmp"):
                continue
            src = Path(dirpath) / name
            dst = tools.root / rel
            try:
                st = src.stat()
            except FileNotFoundError:
                continue
            entry = links.get(rel)
            if entry is None and (dst.exists() or dst.is_symlink()):
                continue      # a Windows depot's file, or one the tools wrote
            seen.add(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if st.st_size >= LINK_MIN:
                if not (dst.is_symlink() and os.readlink(dst) == str(src)):
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    dst.symlink_to(src)
                    made += 1
                links[rel] = {"kind": "link"}
            else:
                sig = [st.st_size, int(st.st_mtime)]
                if entry is None or entry.get("src") != sig or not dst.exists() or dst.is_symlink():
                    if dst.is_symlink():
                        dst.unlink()
                    shutil.copy2(src, dst)
                    copied += 1
                links[rel] = {"kind": "copy", "src": sig}
    gone = [rel for rel in links if rel not in seen]
    for rel in gone:
        p = tools.root / rel
        if p.is_symlink() or p.is_file():
            p.unlink()
        del links[rel]
    for rel in SHARED_DIRS:
        src, dst = native / rel, tools.root / rel
        src.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            continue
        if dst.exists():
            # A depot put files here (addon templates): moved over to the
            # native folder, unless it already has them.
            for item in dst.iterdir():
                if not (src / item.name).exists():
                    shutil.move(str(item), str(src / item.name))
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    tools.save(tools.links_file, links)
    ui.say(f"linked {made} new, copied {copied}, removed {len(gone)}; {len(links)} native files in the tools")


def update(tools, ui=None, qr=False, password=None, use_client=False, if_needed=False):
    """Bring the tools to the native install's build. True when anything was
    done."""
    ui = ui or TerminalUI()
    tgt, state, current = tools.status()
    if not tgt["installed"] or tgt["buildid"] != tgt["current"]:
        raise SystemExit(f"Native Dota 2 is not up to date (installed build {tgt['buildid']}, "
                         f"current {tgt['current']}): update it in Steam first.")
    if if_needed and current:
        return False
    unlink_links(tools)
    try:
        download(tools, tgt, state, ui, qr=qr, password=password, use_client=use_client)
    finally:
        # Linked again even when a depot could not be had, and what did
        # download is remembered.
        link(tools, tgt["native"], ui)
        tools.save(tools.state_file, state)
    if tools.win64.exists():
        (tools.win64 / "steam_appid.txt").write_text(f"{APP}\n")
    state.update(buildid=tgt["buildid"], native=tgt["native"])
    tools.save(tools.state_file, state)
    install_app(tools)
    ui.say(f"tools at build {tgt['buildid']}: {tools.root}")
    return True


# --- the app itself: launcher, menu entry, Steam shortcut --------------------

LAUNCHER = "dota2-tools"
DESKTOP = Path.home() / ".local" / "share" / "applications" / "dota2-workshop-tools.desktop"


def notify(title, body=""):
    """A desktop notification (notify-send, else the notifications service
    over D-Bus); nothing if neither is there."""
    try:
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Dota 2 Workshop Tools", "-i", "applications-graphics",
                              title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("gdbus"):
            subprocess.Popen(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                              "--object-path", "/org/freedesktop/Notifications",
                              "--method", "org.freedesktop.Notifications.Notify", "Dota 2 Workshop Tools", "0",
                              "applications-graphics", title, body, "[]", "{}", "-1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def install_app(tools):
    """The launcher in the tools folder and the desktop menu entry. From a
    checkout, this program is copied into the tools folder (.dota2_tools/app)
    and runs from there from now on, whatever happens to the checkout; from
    an AppImage, the launcher starts that AppImage (run it again from its new
    place if it moves)."""
    # The settings file written out whole: every setting, current or default.
    tools.save_settings(tools.settings)
    launcher = tools.root / LAUNCHER
    head = "#!/bin/sh\n# Dota 2 Workshop Tools: no arguments opens the launcher window.\n"
    if APPIMAGE:
        launcher.write_text(head + f'DOTA2_TOOLS_DIR="{tools.root}" exec "{APPIMAGE}" "$@"\n')
    else:
        dst = tools.meta / "app"
        if APP_DIR.resolve() != dst.resolve():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(APP_DIR, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".github",
                                                                        "packaging"))
        launcher.write_text(head + f'exec "$(dirname "$(readlink -f "$0")")/.dota2_tools/app/{LAUNCHER}" "$@"\n')
    launcher.chmod(0o755)
    icon = dota_icon(tools.steam)
    DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP.write_text("[Desktop Entry]\nType=Application\nName=Dota 2 Workshop Tools\n"
                       "Comment=Dota 2 Workshop Tools through Proton\n"
                       f"Exec=\"{launcher}\"\nIcon={icon or 'steam'}\nCategories=Game;Development;\n"
                       "Terminal=false\nStartupWMClass=dota2-tools\n")


def dota_icon(steam):
    """Dota's own icon, as Steam caches it (the one .jpg at the top of its
    library cache folder)."""
    icons = sorted((Path(steam) / "appcache" / "librarycache" / str(APP)).glob("*.jpg"))
    return str(icons[0]) if icons else ""


def proton_script(tools):
    """The proton script to run with: the settings', else the recommended
    one where that is gone (a Proton uninstalled in Steam)."""
    from . import proton
    p = tools.settings["proton"]
    if Path(p).exists():
        return p
    return proton.default_proton(tools.steam) or p


def launch_command(tools, exe="dota2.exe", args=()):
    """(command, environment, working folder) running a Windows program of
    the tools (or of the active old build) through Proton, in the Steam
    Linux Runtime unless turned off."""
    s = tools.settings
    steam = tools.steam
    prefix = tools.meta / "prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    native = tools.state.get("native", "")
    root = tools.game_root
    bindir = root / "game" / "bin" / "win64"
    if not (bindir / exe).exists() and (root / exe).exists():
        bindir = root                   # a Source 1 build: dota.exe at the top
    env = dict(os.environ,
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam),
               STEAM_COMPAT_DATA_PATH=str(prefix),
               STEAM_COMPAT_INSTALL_PATH=str(root),
               STEAM_COMPAT_APP_ID=str(APP), SteamAppId=str(APP), SteamGameId=str(APP),
               STEAM_COMPAT_MOUNTS=":".join(dict.fromkeys(p for p in (str(tools.root), str(root), native) if p)))
    from . import proton
    cmd = [proton_script(tools), "waitforexitandrun", str(bindir / exe)] + list(args)
    sniper = proton.runtime_entry(steam)
    if sniper and s.get("container", True):
        cmd = [str(sniper), "--verb=waitforexitandrun", "--"] + cmd
    return cmd, env, str(bindir)


def game_command(tools, cfg=False, addon="", map_name="", vconsole=True, extra=""):
    """(exe, arguments) for the launcher's Launch: the tools on an addon, or
    Dota's tools launcher (cfg); for an active Source 1 build, its game."""
    root = tools.game_root
    if root != tools.root and not (root / "game" / "bin" / "win64" / "dota2.exe").exists():
        exe = "dota.exe" if (root / "dota.exe").exists() else "dota2.exe"
        return exe, ["-steam", "-console", "-novid"] + extra.split()
    if cfg:
        return "dota2cfg.exe", ["-steam", "-dx11"] + extra.split()
    return "dota2.exe", game_args(addon, map_name, vconsole, extra)


def open_vconsole(tools, port=29000):
    """A terminal window with the VConsole client (dota2-tools vconsole),
    which waits for the game if it isn't listening yet."""
    from . import terminal
    launcher = tools.root / LAUNCHER
    argv = [str(launcher if launcher.exists() else APP_DIR / LAUNCHER), "--dir", str(tools.root),
            "vconsole", "--port", str(port)]
    cmd = terminal.command(tools.settings.get("terminal", ""), argv)
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cmd


def game_args(addon="", map_name="", vconsole=True, extra=""):
    """dota2.exe arguments for the tools on an addon."""
    args = ["-steam", "-tools", "-nop4"]
    if addon:
        args += ["-addon", addon]
    if vconsole:
        args.append("-vconsole")
    if addon and map_name:
        args += ["+dota_launch_custom_game", addon, map_name]
    return args + extra.split()


def proton_run(tools, exe, args, prefix_name="cli_prefix"):
    """A console tool of the tools (dmxconvert, resourcecompiler), no
    display, in a prefix of its own; returns (rc, output). Run with Proton's
    own wine rather than `proton run`, which swallows the tools' messages
    (warnings, errors, the compile summary). The prefix is made by Proton the
    first time. DXVK is named in WINEDLLOVERRIDES, which proton would set:
    without it resourcecompiler stops on "Failed to create the DXGI Factory"."""
    script = proton_script(tools)
    prefix = tools.meta / prefix_name
    prefix.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(prefix),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=str(tools.steam), PROTON_USE_XALIA="0",
               __GLX_VENDOR_LIBRARY_NAME="mesa", LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe")
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    if not (prefix / "pfx" / "system.reg").exists():
        subprocess.run([script, "run", "cmd", "/c", "exit"], cwd=tools.win64, env=env, capture_output=True)
    wine = Path(script).parent / "files" / "bin" / "wine"
    env.update(WINEPREFIX=str(prefix / "pfx"), WINEDEBUG="-all",
               WINEDLLOVERRIDES="dxgi,d3d11,d3d10core,d3d9,d3d12,d3d12core=n,b")
    p = subprocess.run([str(wine), exe] + list(args), cwd=tools.win64, env=env,
                       capture_output=True, text=True, errors="replace")
    out = "\n".join(ln for ln in (p.stdout + p.stderr).splitlines()
                    if ln.strip() and not ln.startswith(("info:", "warn:", "err:", "fixme:"))
                    and "lsfg-vk" not in ln and "ntsync" not in ln)
    return p.returncode, out


def resource_compile(tools, content_file):
    """resourcecompiler -f on one content file; (ok, its messages). It exits
    0 on a failed file too: the summary line tells."""
    rc, out = proton_run(tools, "resourcecompiler.exe",
                         ["-f", "-nop4", "-game", winpath(tools.root / "game" / "dota"),
                          "-i", winpath(content_file)], prefix_name="rc_prefix")
    ok = rc == 0 and " 0 failed" in out and (" compiled" in out)
    return ok, out


def vtex_image(data):
    """A compiled texture (.vtex_c bytes) as a Pillow image: its largest mip,
    DXT1/DXT5/BC7/RGBA8888/BGRA8888 (Pillow decodes the block formats)."""
    from PIL import Image
    _, _, _, _, count = struct.unpack_from("<IHHII", data, 0)
    blocks = {}
    for k in range(count):
        p = 8 + 4 + 4 + k * 12
        kind, rel, size = struct.unpack_from("<4sII", data, p)
        blocks[kind] = (p + 4 + rel, size)
    dpos, _ = blocks[b"DATA"]
    w, h, _depth, fmt, _mips = struct.unpack_from("<HHHBB", data, dpos + 4 + 16)
    bpp = {1: 0.5, 2: 1, 20: 1, 4: 4, 28: 4}.get(fmt)
    if bpp is None:
        raise ValueError(f"texture format {fmt} not supported")
    size = int(max(w, 4) * max(h, 4) * bpp)
    raw = data[-size:]                     # mips run smallest to largest
    if fmt in (4, 28):
        im = Image.frombuffer("RGBA", (w, h), raw, "raw", "RGBA" if fmt == 4 else "BGRA", 0, 1)
    else:
        im = Image.frombuffer("RGBA", (w, h), raw, "bcn", {1: 1, 2: 3, 20: 7}[fmt])
    return im.copy()


def winpath(path):
    """A Linux path as Wine sees it."""
    return "Z:" + str(path).replace("/", "\\")


# Steam's binary KeyValues (shortcuts.vdf): 0 map, 1 string, 2 int32, 8 end.
def bvdf_load(b):
    def kv(p):
        d = {}
        while True:
            t = b[p]
            p += 1
            if t == 8:
                return d, p
            e = b.index(b"\0", p)
            k = b[p:e].decode("utf-8", "replace")
            p = e + 1
            if t == 0:
                d[k], p = kv(p)
            elif t == 1:
                e = b.index(b"\0", p)
                d[k] = b[p:e].decode("utf-8", "replace")
                p = e + 1
            elif t == 2:
                d[k] = struct.unpack_from("<i", b, p)[0]
                p += 4
            else:
                raise ValueError(f"shortcuts.vdf: unknown type {t}")
    return kv(0)[0] if b else {}


def bvdf_dump(d):
    out = bytearray()
    for k, v in d.items():
        kb = k.encode() + b"\0"
        if isinstance(v, dict):
            out += b"\0" + kb + bvdf_dump(v)
        elif isinstance(v, int):
            out += b"\2" + kb + struct.pack("<i", v)
        else:
            out += b"\1" + kb + str(v).encode() + b"\0"
    return bytes(out + b"\x08")


def add_to_steam(tools):
    """The launcher as a non-Steam game. Steam must be closed: it rewrites
    shortcuts.vdf on exit. Returns a message."""
    steam = tools.steam
    if steam_running():
        raise SystemExit("Steam is running: close it first (it rewrites shortcuts.vdf on exit)")
    users = vdf_text((steam / "config" / "loginusers.vdf").read_text()).get("users", {})
    ids = [sid for sid, u in users.items() if u.get("MostRecent") == "1"] or list(users)
    if not ids:
        raise SystemExit("no Steam user found")
    account = int(ids[0]) - 76561197960265728
    path = steam / "userdata" / str(account) / "config" / "shortcuts.vdf"
    data = bvdf_load(path.read_bytes()) if path.exists() else {}
    shortcuts = data.setdefault("shortcuts", {})
    exe = f'"{tools.root / LAUNCHER}"'
    name = "Dota 2 Workshop Tools"
    for sc in shortcuts.values():
        if sc.get("Exe") == exe or sc.get("AppName", sc.get("appname")) == name:
            return "already in Steam"
    appid = binascii.crc32((exe + name).encode()) | 0x80000000
    appid = appid - (1 << 32) if appid >= 1 << 31 else appid
    shortcuts[str(len(shortcuts))] = {
        "appid": appid, "AppName": name, "Exe": exe, "StartDir": f'"{tools.root}"',
        "icon": dota_icon(steam), "ShortcutPath": "", "LaunchOptions": "",
        "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1, "OpenVR": 0, "Devkit": 0,
        "DevkitGameID": "", "DevkitOverrideAppID": 0, "LastPlayTime": 0, "FlatpakAppID": "",
        "tags": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".vdf.bak"))
    path.write_bytes(bvdf_dump(data))
    return f"added to Steam ({path}); start Steam to see it"
