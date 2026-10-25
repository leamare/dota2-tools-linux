"""dota2-tools: Dota 2 Workshop Tools beside a native Linux Dota 2.

    dota2-tools                              the launcher window (the installer if nothing is installed yet)
    dota2-tools install [--username NAME] [--qr | --client] [--dir DIR] [--native DIR] [--proton PATH]
    dota2-tools update [--if-needed] [--username NAME] [--qr | --client]
    dota2-tools status | link | add-to-steam
    dota2-tools launch [--addon NAME] [--map MAP] [--cfg] [--no-update] [--vconsole-terminal] [-- game arguments]
    dota2-tools vconsole [--port N] [--terminal]   the game's console (waits for the game; --terminal: in a new window)
    dota2-tools proton [list | use NAME | install NAME | install-ge [TAG] | remove NAME | runtime]
    dota2-tools new-addon FOLDER [--title NAME] [--map MAP] [--from ADDON] [--from-map MAP]
    dota2-tools render maps [--addon NAME]
    dota2-tools render auto --map MAP [--addon NAME]      prepare, export in a hidden SFM, stitch
    dota2-tools render prepare --map MAP [--addon NAME] [--frames DIR] [--rect X0 X1 Y0 Y1]
    dota2-tools render stitch --map MAP [--addon NAME] --frames DIR --out FOLDER [--session FILE]
    dota2-tools render tiles --picture FILE --out FOLDER [--format jpg|webp|png] [--quality Q]

The tools folder is --dir, else $DOTA2_TOOLS_DIR, else the one this copy is
installed in, else ~/.local/share/dota2-workshop-tools. See README.md.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import core, proton


def locate(path=None):
    if path:
        return core.Tools(path)
    if os.environ.get("DOTA2_TOOLS_DIR"):
        return core.Tools(os.environ["DOTA2_TOOLS_DIR"])
    if core.APP_DIR.parent.name == ".dota2_tools":
        return core.Tools(core.APP_DIR.parent.parent)
    return core.Tools(core.DEFAULT_DIR)


def set_login(tools, args):
    """--username kept for later updates; a QR login is remembered under the
    account name too, so it needs the name given."""
    s = tools.settings
    if getattr(args, "username", None):
        s["username"] = args.username
    if getattr(args, "qr", False) and not s.get("username"):
        raise SystemExit("--qr needs --username NAME too: later updates log in by that name")
    tools.save_settings(s)


def run_update(tools, args, if_needed=False):
    try:
        return core.update(tools, qr=args.qr, use_client=args.client, if_needed=if_needed)
    except core.NeedsClient as e:
        print(f"The Workshop Tools depot needs your Steam account, or the Steam client:\n{e}", file=sys.stderr)
        sys.exit(2)


def cmd_install(tools, args):
    s = tools.settings
    for key in ("steam", "native", "proton"):
        if getattr(args, key):
            s[key] = str(Path(getattr(args, key)).expanduser())
    tools.save_settings(s)
    set_login(tools, args)
    run_update(tools, args)
    print(f"launcher: {tools.root / core.LAUNCHER} (also in the desktop menu); "
          f"add it to Steam with: {tools.root / core.LAUNCHER} add-to-steam")


def cmd_launch(tools, args, extra):
    s = tools.settings
    for text, appid in proton.problems(tools):
        print(text + (f": dota2-tools proton install {appid}" if appid else ""), file=sys.stderr)
    if proton.problems(tools):
        return 1
    if not args.no_update and s.get("auto_update", True):
        try:
            if core.update(tools, if_needed=True):
                print("updated to the native install's build")
        except (SystemExit, core.NeedsClient) as e:
            if tools.state.get("buildid") != tools.target()["buildid"]:
                raise SystemExit(f"could not update the tools: {e}")
    if args.cfg:
        exe, game = "dota2cfg.exe", ["-steam", "-dx11"] + extra
    else:
        exe, game = "dota2.exe", core.game_args(args.addon or "", args.map or "", not args.no_vconsole,
                                                " ".join(extra))
    cmd, env, cwd = core.launch_command(tools, exe, game)
    print(" ".join(cmd), flush=True)
    if not args.cfg and (args.vconsole_terminal or s["launch"].get("vconsole_terminal")):
        core.open_vconsole(tools)
    return subprocess.call(cmd, env=env, cwd=cwd)


def cmd_proton(tools, args):
    steam = tools.steam
    what, name = (args.what or "list"), args.name
    if what == "list":
        use = core.proton_script(tools)
        for p in proton.installed(steam):
            kind = "custom" if p["custom"] or p["appid"] is None else f"Steam {p['appid']}"
            print(f"{'*' if p['path'] == use else ' '} {p['name']:28s} {p['version']:36s} {kind}")
        missing = [b for b in proton.steam_builds(steam) if not b["installed"]]
        if missing:
            print("installable through Steam: " + ", ".join(f"{b['name']} ({b['appid']})" for b in missing))
        print(f"runtime (sniper): {'installed' if proton.runtime_installed(steam) else 'missing'}")
    elif what == "use":
        p = proton.by_name(steam, name or "")
        if not p:
            raise SystemExit(f"no installed Proton {name!r}: dota2-tools proton list")
        s = tools.settings
        s["proton"] = p["path"]
        tools.save_settings(s)
        print(f"using {p['name']}")
    elif what == "install":
        build = next((b for b in proton.steam_builds(steam)
                      if name in (str(b["appid"]), b["name"])), None) if name else None
        appid = build["appid"] if build else proton.RECOMMENDED if not name else None
        if appid is None:
            raise SystemExit(f"no Steam Proton {name!r}: dota2-tools proton list")
        proton.steam_install(appid)
        print(f"Steam asked to install {appid}")
    elif what == "install-ge":
        print(proton.install_ge(steam, name))
    elif what == "remove":
        proton.remove_custom(steam, name or "")
        print(f"removed {name}")
    elif what == "runtime":
        if proton.runtime_installed(steam):
            print("the sniper runtime is installed")
        else:
            proton.steam_install(proton.SNIPER)
            print("Steam asked to install the Steam Linux Runtime 3.0 (sniper)")
    return 0


def cmd_status(tools, args):
    tgt, state, current = tools.status()
    print(f"native:  {tgt['native']} build {tgt['buildid']} ({tgt['branch']}), current {tgt['current']}"
          f"{'' if tgt['installed'] else ', update pending'}")
    print(f"tools:   {tools.root} build {state.get('buildid', 'not installed')}")
    for d, m in tgt["manifests"].items():
        print(f"  depot {d}: wants {m}, has {state.get('manifests', {}).get(d)}")
    print("up to date" if current else "update needed")


def cmd_render(tools, args):
    from . import maprender as mr
    if args.what == "maps":
        for m in mr.suggested_maps(tools, args.addon or ""):
            print(m)
    elif args.what == "prepare":
        if not args.map:
            raise SystemExit("--map MAP")
        frames = args.frames or str(Path.home() / "Pictures" / "dota2-maprender" / (args.addon or "dota")
                                    / args.map / "frames")
        mr.prepare(tools, args.addon or "", args.map, frames, tuple(args.rect) if args.rect else None)
        print(mr.sfm_steps(tools, args.map))
    elif args.what == "auto":
        from . import sfmauto
        if not args.map:
            raise SystemExit("--map MAP")
        base = Path.home() / "Pictures" / "dota2-maprender" / (args.addon or "dota") / args.map
        frames = args.frames or str(base / "frames")
        mr.prepare(tools, args.addon or "", args.map, frames, tuple(args.rect) if args.rect else None)
        try:
            sfmauto.render_all(tools, args.map, frames)
            made = mr.stitch(tools, frames, args.out or str(base), args.addon or "", args.map)
        except (SystemExit, Exception) as e:
            core.notify(f"Render of {args.map}: stopped", str(e))
            raise
        core.notify(f"Render of {args.map} done", f"in {args.out or base}" if made is not None else "")
    elif args.what == "tiles":
        from . import tiles
        if not (args.picture and args.out):
            raise SystemExit("--picture FILE --out FOLDER [--format jpg|webp|png] [--quality Q]")
        tiles.make(args.picture, args.out, args.format, args.quality)
    elif args.what == "stitch":
        if not (args.frames and args.out and args.map):
            raise SystemExit("--map MAP --frames DIR --out FOLDER")
        mr.stitch(tools, args.frames, args.out, args.addon or "", args.map, args.session)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="dota2-tools", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="the tools folder")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="the launcher window (default)")
    for name in ("install", "update"):
        p = sub.add_parser(name, help="first download and setup" if name == "install"
                           else "download what changed and relink")
        p.add_argument("--username", help="Steam account with the Workshop Tools DLC (asked once)")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--qr", action="store_true", help="log in by QR code with the Steam mobile app")
        g.add_argument("--client", action="store_true",
                       help="no login of our own: the Steam client's console downloads the tools DLC")
        if name == "install":
            p.add_argument("--steam", help=f"Steam's folder (default {core.STEAM_ROOT})")
            p.add_argument("--native", help="the native Dota 2 folder (default: found in Steam's libraries)")
            p.add_argument("--proton", help="the proton script (default Proton - Experimental)")
        else:
            p.add_argument("--if-needed", action="store_true", help="only when the native build moved on")
    sub.add_parser("status", help="builds of the native install and the tools")
    sub.add_parser("link", help="relink the native install's files")
    sub.add_parser("add-to-steam", help="the launcher as a non-Steam game (Steam closed)")
    p = sub.add_parser("launch", help="the tools (anything after -- goes to the game)")
    p.add_argument("--addon")
    p.add_argument("--map", help="start the addon on this map")
    p.add_argument("--cfg", action="store_true", help="Dota's own tools launcher (dota2cfg.exe) instead")
    p.add_argument("--no-vconsole", action="store_true")
    p.add_argument("--no-update", action="store_true")
    p.add_argument("--vconsole-terminal", action="store_true", help="open a terminal with the game's console too")
    p = sub.add_parser("vconsole", help="the game's console in this terminal (waits for the game)")
    p.add_argument("--port", type=int, default=29000)
    p.add_argument("--terminal", action="store_true", help="in a new terminal window instead")
    p = sub.add_parser("proton", help="installed Proton versions; install, choose, remove")
    p.add_argument("what", nargs="?", choices=["list", "use", "install", "install-ge", "remove", "runtime"])
    p.add_argument("name", nargs="?", help="a Proton's folder name, a Steam app id, or a GE-Proton tag")
    p = sub.add_parser("new-addon", help="a new addon from Dota's addon_template (or another addon)")
    p.add_argument("folder", help="its folder name (lowercase, digits, _)")
    p.add_argument("--title", default="", help="its name in the game")
    p.add_argument("--map", default="", help="its map's name (default: the folder name)")
    p.add_argument("--from", dest="base", default=core.TEMPLATE_ADDON, help="the addon to copy (default addon_template)")
    p.add_argument("--from-map", default="", help="the base's map to rename (default: its first)")
    p = sub.add_parser("render", help="top-down map pictures through Source Filmmaker")
    p.add_argument("what", choices=["maps", "prepare", "auto", "stitch", "tiles"])
    p.add_argument("--picture", help="tiles: the picture to cut (a full picture)")
    p.add_argument("--format", default="jpg", choices=["jpg", "webp", "png"], help="tiles: their format")
    p.add_argument("--quality", type=int, default=0, help="tiles: jpg/webp quality (0: 25 for jpg, 30 for webp)")
    p.add_argument("--addon", help="the addon whose map to render (none: the base game's maps)")
    p.add_argument("--map")
    p.add_argument("--frames", help="the folder SFM exports the frames into")
    p.add_argument("--rect", nargs=4, type=float, metavar=("X0", "X1", "Y0", "Y1"))
    p.add_argument("--out", help="the folder for the stitched pictures (full and/or bounds, settings' render)")
    p.add_argument("--session", help="the session to read the camera from (default: the frames folder's copy)")
    args = ap.parse_args(argv)
    tools = locate(args.dir)
    if args.cmd in (None, "gui"):
        from . import gui
        return gui.main(tools)
    if args.cmd == "install":
        return cmd_install(tools, args)
    if args.cmd == "update":
        if not tools.settings_file.exists():
            raise SystemExit(f"no tools at {tools.root}: install first")
        set_login(tools, args)
        if not run_update(tools, args, args.if_needed):
            print("tools already at the native build")
        return 0
    if args.cmd == "status":
        return cmd_status(tools, args)
    if args.cmd == "link":
        return core.link(tools, tools.native)
    if args.cmd == "add-to-steam":
        print(core.add_to_steam(tools))
        return 0
    if args.cmd == "launch":
        return cmd_launch(tools, args, extra)
    if args.cmd == "vconsole":
        if args.terminal:
            core.open_vconsole(tools, args.port)
            return 0
        from . import vconsole
        vconsole.shell(args.port)
        return 0
    if args.cmd == "proton":
        return cmd_proton(tools, args)
    if args.cmd == "render":
        return cmd_render(tools, args)
    if args.cmd == "new-addon":
        try:
            game, content = core.create_addon(tools, args.folder, args.title, args.map, args.base, args.from_map)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"{game}\n{content}")
        return 0
