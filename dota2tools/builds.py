"""Old builds of Dota 2: their list, downloading them, and running one
instead of the current game.

A build is a set of depot manifests from one moment: for each depot of the
app, the manifest it had then. Steam itself only tells the current one (its
app cache); the history of every depot's manifests is on SteamDB, which has
no API and doesn't allow scraping, so it comes from SteamDB pages saved (or
copied) in your browser while signed in there: a depot's manifests page
(steamdb.info/depot/<depot>/manifests/) and an app's patches page
(steamdb.info/app/<app>/patchnotes/) for its build IDs.

What is imported stays: the list only grows. The current manifests are
recorded from Steam's app cache each time the launcher starts (and with
Refresh), so every build the native install moves through is kept too.

Each build's remark comes from its date: the patch it was on (D2-LRG's
patch dates: every patch and lettered update) and The International being
played then (Liquipedia), both fetched with Refresh (and by the launcher
when a week old) and kept in the tools folder, not here.

Downloads go into the builds folder (settings: builds.folder), one folder
per build, depot by depot with DepotDownloader (or, without a login of our
own, the Steam client's console). The active build is the one the launcher
starts; resetting it returns to the tools folder (the current build, linked
from the native install).
"""
import calendar
import gzip
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import core

APPS = {
    570: {"name": "Dota 2", "primary": 373301,
          # Content, Windows binaries and shaders, the Workshop Tools DLC and
          # its content. Not: macOS/Linux binaries, low violence, languages,
          # the other DLC.
          "depots": [373301, 373302, 373303, 373305, 381450, 381451, 381452, 381453, 381454, 381455]},
    316570: {"name": "Dota 2 (Source 1)", "primary": 571,
             # Dota 2's Source 1 depots: the content and the Windows binaries.
             "depots": [571, 573]},
}
PATCHES = "https://raw.githubusercontent.com/leamare/D2-LRG-Metadata/master/patchdates.json"
LIQUIPEDIA = "https://liquipedia.net/dota2/api.php"
USER_AGENT = "dota-workshop-linux (Dota 2 Workshop Tools for Linux; build browser)"
# Depots updated in one go land within minutes of each other.
SAME_BUILD = 3 * 3600


def _data_file(tools):
    return tools.meta / "builds.json"


def load(tools):
    f = _data_file(tools)
    d = tools.load(f, {})
    d.setdefault("depots", {})          # depot -> {app, manifests: {manifest: time}}
    d.setdefault("buildids", {})        # app -> {buildid: time}
    d.setdefault("meta", {})            # patches, ti, fetched
    if "imports" not in d:              # "depot:<n>" / "app:<n>" -> when its SteamDB page was last imported
        # Lists from before this was kept: those with more than Steam's
        # current entries were imported, by the file's date at the latest.
        when = int(f.stat().st_mtime) if f.exists() else 0
        d["imports"] = {**{f"depot:{k}": when for k, v in d["depots"].items() if len(v["manifests"]) > 1},
                        **{f"app:{k}": when for k, v in d["buildids"].items() if len(v) > 1}}
    return d


def save(tools, d):
    tools.save(_data_file(tools), d)


def folder(tools):
    return Path(tools.settings["builds"]["folder"]).expanduser()


def depots_of(tools, app):
    """The depots a build of `app` is downloaded with."""
    return [int(x) for x in tools.settings["builds"]["depots"].get(str(app), APPS[app]["depots"])]


def _app_of_depot(depot):
    for app, a in APPS.items():
        if depot in a["depots"]:
            return app
    return None


def _add(d, depot, manifest, t, app=None):
    e = d["depots"].setdefault(str(depot), {"app": app or _app_of_depot(depot), "manifests": {}})
    old = e["manifests"].get(str(manifest))
    e["manifests"][str(manifest)] = min(old, t) if old else t


# --- the lists ----------------------------------------------------------------------

def _get(url, params=None, timeout=30):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # Liquipedia's API terms: a user agent naming the program, and gzip.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def fetch_patches():
    """[(time, name)]: each patch and lettered update (7.36, 7.36a, 7.36b...)."""
    out = []
    for p in _get(PATCHES).values():
        out.append((p["main"], p["code"]))
        if p.get("add"):
            out.append((p["add"], p["code"] + "a"))
        for k, t in enumerate(p.get("dates", [])):
            out.append((t, p["code"] + chr(ord("b") + k)))
    return sorted(out)


def fetch_internationals():
    """[(start, end, "TI<n> (<year>)")]: every edition's dates, from its
    Liquipedia page (all in one request)."""
    years = range(2011, datetime.now(timezone.utc).year + 2)
    d = _get(LIQUIPEDIA, {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                          "format": "json", "formatversion": "2",
                          "titles": "|".join(f"The_International/{y}" for y in years)})
    found = []
    for page in d.get("query", {}).get("pages", []):
        text = (page.get("revisions") or [{}])[0].get("slots", {}).get("main", {}).get("content", "")
        sd = re.search(r"\|\s*sdate\s*=\s*(\d{4}-\d{2}-\d{2})", text)
        ed = re.search(r"\|\s*edate\s*=\s*(\d{4}-\d{2}-\d{2})", text)
        if sd:
            start = calendar.timegm(time.strptime(sd.group(1), "%Y-%m-%d"))
            end = calendar.timegm(time.strptime((ed or sd).group(1), "%Y-%m-%d")) + 86399
            found.append((start, end, page["title"].rsplit("/", 1)[-1]))
    found.sort()
    return [(s, e, f"TI{n} ({y})") for n, (s, e, y) in enumerate(found, 1)]


def record_current(tools, d=None):
    """The current manifests from Steam's app cache into the list (local,
    quick: the launcher does it at every start)."""
    keep = d is None
    d = d or load(tools)
    now = int(time.time())
    info = core.appinfo(tools.steam, set(APPS))
    for app in APPS:
        depots = info.get(app, {}).get("depots", {})
        public = depots.get("branches", {}).get("public", {})
        t = int(public.get("timeupdated") or now)
        if public.get("buildid"):
            d["buildids"].setdefault(str(app), {})[str(public["buildid"])] = t
        for depot in APPS[app]["depots"]:
            m = depots.get(str(depot), {}).get("manifests", {}).get("public")
            gid = m.get("gid") if isinstance(m, dict) else m
            if gid:
                _add(d, depot, gid, t, app)
    if keep:
        save(tools, d)
    return d


def stale(tools, days=7):
    """The patch and TI dates not fetched in `days`."""
    return time.time() - load(tools)["meta"].get("fetched", 0) > days * 86400


def refresh(tools, say=print):
    """The current manifests from Steam's app cache, and the patch and TI
    dates for the remarks."""
    d = record_current(tools, load(tools))
    now = int(time.time())
    try:
        d["meta"]["patches"] = fetch_patches()
        say(f"{len(d['meta']['patches'])} patches (D2-LRG)")
    except OSError as e:
        say(f"patch dates not fetched: {e}")
    try:
        d["meta"]["ti"] = fetch_internationals()
        say(f"{len(d['meta']['ti'])} Internationals (Liquipedia)")
    except OSError as e:
        say(f"TI dates not fetched: {e}")
    d["meta"]["fetched"] = now
    save(tools, d)
    return d


def _time(text):
    """A date as SteamDB writes it (ISO in data-time, or "2 September 2015 –
    00:22:33 UTC") as a UTC timestamp, or None."""
    text = text.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", text)
    if m:
        return calendar.timegm(time.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"))
    m = re.search(r"(\d{1,2}) ([A-Z][a-z]+) (\d{4})(?:\D+(\d{2}:\d{2}(?::\d{2})?))?", text)
    if m:
        clock = m.group(4) or "00:00:00"
        clock += ":00" if clock.count(":") == 1 else ""
        try:
            return calendar.timegm(time.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)} {clock}",
                                                 "%d %B %Y %H:%M:%S"))
        except ValueError:
            return None
    return None


# Depots' descriptions (Steam's app cache has no names for them).
DEPOT_NAMES = {373301: "content", 373302: "Windows binaries", 373303: "Windows 64-bit binaries",
               381450: "Workshop Tools (DLC)", 381451: "Workshop Tools content", 381452: "Workshop Tools content",
               381453: "Workshop Tools content", 381454: "Workshop Tools content",
               381455: "Workshop Tools content", 571: "Source 1 content", 573: "Source 1 Windows binaries"}


def page_url(kind, ident):
    return (f"https://steamdb.info/depot/{ident}/manifests/" if kind == "depot"
            else f"https://steamdb.info/app/{ident}/patchnotes/")


def sources(tools):
    """The SteamDB pages old builds need: each depot builds are downloaded
    with, and each app's patches page (its build IDs). [{kind, id, label,
    url, imported (time or None), entries}]."""
    d = load(tools)
    out = []
    for app in APPS:
        for depot in depots_of(tools, app):
            name = DEPOT_NAMES.get(depot, "")
            out.append({"kind": "depot", "id": depot, "label": f"{app} · depot {depot}" + (f" ({name})" if name else ""),
                        "entries": len(d["depots"].get(str(depot), {}).get("manifests", {}))})
        out.append({"kind": "app", "id": app, "label": f"{app} · patches (build IDs)",
                    "entries": len(d["buildids"].get(str(app), {}))})
    for x in out:
        x["url"] = page_url(x["kind"], x["id"])
        x["imported"] = d["imports"].get(f"{x['kind']}:{x['id']}")
    return out


def page_source(text):
    """What a saved SteamDB page is of: ("depot", n), ("app", n), or None
    (copied text, which doesn't say)."""
    m = re.search(r"/depot/(\d+)/(?:manifests|history)", text)
    if m:
        return "depot", int(m.group(1))
    m = re.search(r"/app/(\d+)/patchnotes", text)
    return ("app", int(m.group(1))) if m else None


def import_page(tools, text, depot=None, app=None):
    """A SteamDB page saved or copied from the browser: a depot's manifests
    (manifest IDs with their dates) or an app's patches (build IDs with
    their dates). The depot or app is read from a saved page; for copied
    text it has to be given. Returns (kind, id, entries added)."""
    d = load(tools)
    if depot is None and app is None:
        kind, ident = page_source(text) or (None, None)
        depot, app = (ident, None) if kind == "depot" else (None, ident)
    rows = re.findall(r"<tr.*?</tr>", text, re.S) or text.splitlines()
    added = 0
    for row in rows:
        when = re.search(r'data-time="([^"]+)"', row)
        t = _time(when.group(1)) if when else _time(re.sub(r"<[^>]+>", " ", row))
        if t is None:
            continue
        if depot is not None:
            m = re.search(r"changeid=M:(\d+)", row) or re.search(r"(?<!\d)(\d{15,20})(?!\d)", re.sub(r"<[^>]+>", " ", row))
            if m:
                _add(d, depot, m.group(1), t)
                added += 1
        elif app is not None:
            m = re.search(r"/patchnotes/(\d+)", row) or re.search(r"(?<!\d)(\d{5,9})(?!\d)", re.sub(r"<[^>]+>", " ", row))
            if m:
                d["buildids"].setdefault(str(app), {})[m.group(1)] = t
                added += 1
    if depot is None and app is None:
        raise ValueError("no depot or app found in the page: give one")
    d["imports"]["depot:%d" % depot if depot is not None else "app:%d" % app] = int(time.time())
    save(tools, d)
    return ("depot", depot, added) if depot is not None else ("app", app, added)


def import_file(tools, path, depot=None, app=None):
    """import_page on a saved page (gzipped too, as web archives keep them)."""
    data = Path(path).expanduser().read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return import_page(tools, data.decode("utf-8", "replace"), depot, app)


# --- builds -------------------------------------------------------------------------

def remark(meta, t):
    """The patch the build was on, and the TI being played then."""
    out = []
    patch = None
    for pt, name in meta.get("patches", []):
        if pt <= t + 6 * 3600:
            patch = name
    if patch:
        out.append(patch)
    for start, end, name in meta.get("ti", []):
        if start <= t <= end:
            out.append(f"during {name}")
    return ", ".join(out)


def _key(app, t, manifest):
    return f"{app}_{time.strftime('%Y-%m-%d_%H%M', time.gmtime(t))}_{manifest}"


def rows(tools):
    """Every known build, newest first: [{key, app, time, manifests {depot:
    manifest}, missing [depots], buildid, remark, status, path}]. A build
    is a manifest of the app's primary depot; each other depot takes the
    manifest it had then.

    status: "latest" for the tools folder's build (Steam's, which the
    tools folder follows; "latest, active" while the launcher starts it),
    "active" for the old build the launcher starts instead, "downloaded",
    "partial", or empty."""
    d = load(tools)
    base = folder(tools)
    root = tools.game_root
    active = str(root) if root != tools.root else ""
    # The tools folder's build: the newest whose Windows depots are at the
    # manifests it was last updated to.
    have = tools.state.get("manifests", {})
    latest = None
    out = []
    for app, a in APPS.items():
        hist = {dep: sorted((t, m) for m, t in d["depots"].get(str(dep), {}).get("manifests", {}).items())
                for dep in depots_of(tools, app)}
        builds = sorted(d["buildids"].get(str(app), {}).items(), key=lambda b: b[1])
        for t, manifest in hist.get(a["primary"], []):
            mans, missing = {}, []
            for dep, h in hist.items():
                pick = [m for mt, m in h if mt <= t + SAME_BUILD]
                if pick:
                    mans[dep] = pick[-1]
                else:
                    missing.append(dep)
            near = [b for b, bt in builds if abs(bt - t) <= SAME_BUILD]
            key = _key(app, t, manifest)
            path = base / key
            done = _read(path / "build.json").get("done", {})
            got = [dep for dep, m in mans.items() if done.get(str(dep)) == m]
            status = ("active" if str(path) == active else "downloaded") if mans and len(got) == len(mans) \
                else "partial" if got else ""
            if app == core.APP and have and all(str(mans.get(int(dep))) == m for dep, m in have.items()) \
                    and (latest is None or t > latest[0]):
                latest = (t, len(out))
            out.append({"key": key, "app": app, "time": t, "manifest": manifest, "manifests": mans,
                        "missing": missing, "buildid": near[0] if near else "", "remark": remark(d["meta"], t),
                        "status": status, "path": str(path)})
    if latest:
        r = out[latest[1]]
        r["status"] = "latest" + (", active" if not active else "") + (f"; {r['status']}" if r["status"] else "")
    return sorted(out, key=lambda r: -r["time"])


def find(tools, ref):
    """A build by its key, its primary manifest or its build ID."""
    for r in rows(tools):
        if ref in (r["key"], r["manifest"], str(r["buildid"])):
            return r
    return None


def _read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def needs_login(app, depot):
    """Anonymous Steam has app 570's game depots, at any manifest; not the
    tools DLC, nor app 316570 ("Dota 2 Workshop Tools Alpha" on Steam,
    which Source 1 Dota's depots are listed under now)."""
    return app != core.APP or depot in core.LOGIN_DEPOTS


def _import_client(tools, app, depot, manifest, path, ui):
    """A depot the running Steam client downloaded (download_depot in its
    console), moved into the build's folder."""
    marker = tools.meta / f"client_build_{depot}.json"
    src = core.client_ready(tools, depot, manifest, app, asked=_read(marker).get("manifest"))
    if src is None:
        tools.save(marker, {"manifest": manifest})
        raise core.NeedsClient(depot, manifest, core.client_folder(tools, depot, app), app,
                               again="download the build again")
    for dirpath, _, names in os.walk(src):
        for name in names:
            dst = path / os.path.relpath(os.path.join(dirpath, name), src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(os.path.join(dirpath, name), dst)
    shutil.rmtree(src, ignore_errors=True)
    if marker.exists():
        marker.unlink()
    ui.say(f"imported depot {depot} from the Steam client")


def download(tools, row, ui=None, qr=False, password=None, use_client=False):
    """Every depot of the build into its folder, those not there yet:
    DepotDownloader, anonymously where Steam allows it, else with the
    remembered login (or a QR code), else the Steam client's console."""
    ui = ui or core.TerminalUI()
    s = tools.settings
    path = Path(row["path"])
    path.mkdir(parents=True, exist_ok=True)
    marker = path / "build.json"
    state = _read(marker)
    state.update(app=row["app"], time=row["time"], manifest=row["manifest"], buildid=row["buildid"],
                 remark=row["remark"], manifests={str(k): v for k, v in row["manifests"].items()})
    done = state.setdefault("done", {})
    for depot, manifest in row["manifests"].items():
        if done.get(str(depot)) == manifest:
            continue
        login = needs_login(row["app"], depot)
        if login and (use_client or (not s.get("username") and not qr)):
            _import_client(tools, row["app"], depot, manifest, path, ui)
        else:
            cmd = [str(core.depotdownloader(tools)), "-app", str(row["app"]), "-depot", str(depot),
                   "-manifest", str(manifest), "-dir", str(path), "-os", "windows", "-osarch", "64"]
            if login and qr:
                cmd += ["-qr", "-remember-password"]
            elif login:
                cmd += ["-username", s["username"], "-remember-password"] + (["-password", password] if password else [])
            ui.say(f"== {row['key']}: depot {depot} manifest {manifest}")
            rc = ui.run(cmd)
            if rc != 0:
                raise SystemExit(f"DepotDownloader failed on depot {depot} (rc {rc})"
                                 + (f": the account needs app {row['app']}" if row["app"] != core.APP else ""))
            if login:
                qr, password = False, None    # one login per run
        done[str(depot)] = manifest
        marker.write_text(json.dumps(state, indent=1))
    # Started outside Steam, the game asks it for the app in steam_appid.txt.
    for d in (path / "game" / "bin" / "win64", path):
        if any(d.glob("dota*.exe")):
            (d / "steam_appid.txt").write_text(f"{core.APP}\n")
    ui.say(f"{row['key']}: downloaded into {path}")


def client_commands(row):
    """The Steam client console's commands downloading the build's depots
    (into steamapps/content/app_<app>/ beside the client's binaries:
    ubuntu12_32, or steamrt64 for the 64-bit client)."""
    return [core.client_command(depot, manifest, row["app"]) for depot, manifest in row["manifests"].items()]


def delete(tools, row):
    if tools.settings["builds"].get("active") == row["path"]:
        activate(tools, None)
    shutil.rmtree(row["path"], ignore_errors=True)


def activate(tools, row):
    """The build the launcher starts (None: back to the current one, the
    tools folder)."""
    s = tools.settings
    s["builds"]["active"] = row["path"] if row else ""
    tools.save_settings(s)


def executable(root):
    """The game program in a build's folder: (exe path, Source 2 or not)."""
    root = Path(root)
    s2 = root / "game" / "bin" / "win64" / "dota2.exe"
    if s2.exists():
        return s2, True
    for name in ("dota.exe", "dota2.exe"):
        if (root / name).exists():
            return root / name, False
    return None, False
