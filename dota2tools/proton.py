"""Proton versions: the installed ones, and installing or removing others.

Steam's own Proton builds, and the Steam Linux Runtime the tools run in
("sniper"), are Steam tools: Steam installs and uninstalls them
(steam://install/<appid>), and its app cache says which there are. Custom
builds (GE-Proton) are folders in Steam's compatibilitytools.d: installed
here from their GitHub releases, and removed by deleting the folder.

Standard library only.
"""
import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from . import core

# What the tools are run with by default, and the runtime they run in.
RECOMMENDED = 1493710          # Proton Experimental
SNIPER = 1628350               # Steam Linux Runtime 3.0 (sniper)
GE_RELEASES = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases?per_page={n}"
# Steam tools named Proton that are not Proton builds, or not for this machine.
NOT_PROTON = ("Runtime", "Voice", "Contrib", "ARM64")


def libraries(steam):
    """Steam's library folders."""
    steam = Path(steam)
    try:
        libs = core.vdf_text((steam / "steamapps" / "libraryfolders.vdf").read_text())
    except OSError:
        return [steam]
    return [Path(v["path"]) for v in libs.get("libraryfolders", {}).values() if "path" in v] or [steam]


def custom_dirs(steam):
    """Where custom builds live: the user's compatibilitytools.d, then the
    system's."""
    return [Path(steam) / "compatibilitytools.d", Path("/usr/share/steam/compatibilitytools.d")]


def _version(folder):
    """A build's version file ("<timestamp> <name>"), its name part."""
    try:
        return (folder / "version").read_text().split()[-1]
    except (OSError, IndexError):
        return ""


def installed(steam):
    """Every installed Proton: [{name, path (its proton script), version,
    appid (Steam's; None for a custom build), custom (removable here)}]."""
    out = []
    for lib in libraries(steam):
        apps = lib / "steamapps"
        dirs = {}
        for man in apps.glob("appmanifest_*.acf"):
            try:
                st = core.vdf_text(man.read_text())["AppState"]
                dirs[st["installdir"]] = int(st["appid"])
            except (OSError, KeyError, ValueError):
                continue
        for script in sorted((apps / "common").glob("*/proton")):
            folder = script.parent
            out.append({"name": folder.name, "path": str(script), "version": _version(folder),
                        "appid": dirs.get(folder.name), "custom": False})
    user = custom_dirs(steam)[0]
    for base in custom_dirs(steam):
        for script in sorted(base.glob("*/proton")):
            out.append({"name": script.parent.name, "path": str(script), "version": _version(script.parent),
                        "appid": None, "custom": base == user})
    return out


def by_name(steam, name):
    """An installed Proton by its folder name (or its proton script's path)."""
    for p in installed(steam):
        if name in (p["name"], p["path"]):
            return p
    return None


def steam_builds(steam):
    """Steam's Proton builds this machine can install: [{appid, name,
    installed}], newest first."""
    have = {p["appid"] for p in installed(steam)}
    out = []
    for appid, d in core.appinfo(steam).items():
        c = d.get("common", {})
        name = str(c.get("name", ""))
        if c.get("type") == "Tool" and name.startswith("Proton") and not any(w in name for w in NOT_PROTON):
            out.append({"appid": appid, "name": name, "installed": appid in have})
    return sorted(out, key=lambda p: (p["appid"] != RECOMMENDED, -p["appid"]))


def runtime_installed(steam):
    """Whether the Steam Linux Runtime (sniper) the tools run in is there."""
    return any((lib / "steamapps" / "common" / "SteamLinuxRuntime_sniper" / "_v2-entry-point").exists()
               for lib in libraries(steam))


def runtime_entry(steam):
    """The sniper runtime's entry point, or None."""
    for lib in libraries(steam):
        p = lib / "steamapps" / "common" / "SteamLinuxRuntime_sniper" / "_v2-entry-point"
        if p.exists():
            return p
    return None


def steam_install(appid):
    """Ask Steam to install a tool (it shows its own dialog; Steam must run)."""
    subprocess.Popen(["xdg-open", f"steam://install/{appid}"])


def steam_uninstall(appid):
    subprocess.Popen(["xdg-open", f"steam://uninstall/{appid}"])


def default_proton(steam):
    """The recommended Proton's script where installed, else None."""
    for p in installed(steam):
        if p["appid"] == RECOMMENDED:
            return p["path"]
    return None


def problems(tools):
    """What stops the tools from starting: [(text, appid to install or None)]."""
    s = tools.settings
    out = []
    if not Path(s["proton"]).exists():
        out.append((f"Proton not found: {s['proton']}", None if default_proton(tools.steam) else RECOMMENDED))
    if s.get("container", True) and not runtime_installed(tools.steam):
        out.append(("The Steam Linux Runtime 3.0 (sniper) is not installed", SNIPER))
    return out


# --- GE-Proton -------------------------------------------------------------

def ge_releases(n=8):
    """GE-Proton's latest releases: [{tag, url, size, sum_url}] (x86_64)."""
    req = urllib.request.Request(GE_RELEASES.format(n=n), headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        releases = json.load(r)
    out = []
    for rel in releases:
        assets = {a["name"]: a for a in rel.get("assets", [])}
        tar = next((a for name, a in assets.items() if name.endswith("x86_64.tar.gz")), None)
        if not tar:
            tar = next((a for name, a in assets.items()
                        if name.endswith(".tar.gz") and "aarch64" not in name), None)
        if tar:
            sums = assets.get(tar["name"].replace(".tar.gz", ".sha512sum"))
            out.append({"tag": rel["tag_name"], "url": tar["browser_download_url"], "size": tar["size"],
                        "sum_url": sums["browser_download_url"] if sums else ""})
    return out


def install_ge(steam, tag=None, say=print, progress=lambda f: None):
    """A GE-Proton release (the latest, or `tag`) into compatibilitytools.d,
    checked against its sha512 sum. Returns its proton script."""
    releases = ge_releases(20)
    rel = next((r for r in releases if r["tag"] == tag), None) if tag else (releases[0] if releases else None)
    if not rel:
        raise SystemExit(f"no GE-Proton release {tag or ''}".strip())
    dest = custom_dirs(steam)[0]
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / rel["tag"] / "proton").exists():
        say(f"{rel['tag']} is installed already")
        return str(dest / rel["tag"] / "proton")
    part = dest / (rel["tag"] + ".tar.gz.part")
    say(f"downloading {rel['tag']} ({rel['size'] >> 20} MB)")
    digest = hashlib.sha512()
    with urllib.request.urlopen(rel["url"], timeout=60) as r, open(part, "wb") as f:
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress(done / max(rel["size"], 1))
    if rel["sum_url"]:
        with urllib.request.urlopen(rel["sum_url"], timeout=30) as r:
            want = r.read().decode().split()[0]
        if want != digest.hexdigest():
            part.unlink()
            raise SystemExit(f"{rel['tag']}: the download doesn't match its sha512 sum")
    say(f"unpacking into {dest}")
    with tarfile.open(part) as t:
        t.extractall(dest, filter="data")
    part.unlink()
    return str(dest / rel["tag"] / "proton")


def remove_custom(steam, name):
    """Delete a custom build from the user's compatibilitytools.d."""
    p = by_name(steam, name)
    if not p or not p["custom"]:
        raise SystemExit(f"{name}: not a custom build in {custom_dirs(steam)[0]} (Steam's own: uninstall in Steam)")
    shutil.rmtree(Path(p["path"]).parent)
