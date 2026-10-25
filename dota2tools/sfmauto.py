"""Source Filmmaker without clicks: the tools in a hidden display, SFM driven by
keystrokes, the movie exported, the tools closed.

SFM in Dota has no console command that opens it, a session or an export, and
no scripting, so it is driven through its menus: the tools run in gamescope's
headless backend (a display of its own, nothing on screen, the GPU still
rendering), and xdotool types into its X server. Pointer events don't reach
the windows there, keys do; Qt menus don't open on Alt+letter sent by
xdotool, but on keys sent to the window itself they do, and so do SFM's own
shortcuts. gamescopectl screenshot shows the hidden display: a failed step
leaves one beside the frames folder.

The sequence (Dota 2 Workshop Tools, 2026-10):
  Asset Browser: Tools menu (Alt+T) -> Tools (Right) -> the 6th tool, Source
  Filmmaker (Down x5, Enter); SFM's start dialog: from the Name field, Tab x7
  is Open..., Space; the file dialog: the session's path, Enter; SFM: Ctrl+M
  is Export Movie, its dialog filled from the session (folder, PNG,
  3840x2160, the shot's range), Enter; frames are written one by one.
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import core

TIMEOUT_START = 900        # the tools starting (first start: shaders)
TIMEOUT_STEP = 120


def available():
    return bool(shutil.which("gamescope") and shutil.which("xdotool") and shutil.which("gamescopectl"))


class Hidden:
    """The tools in a headless gamescope, and xdotool on its display."""

    def __init__(self, tools, args, log):
        self.tools, self.log = tools, log
        self.envfile = tools.meta / "gamescope.env"
        self.envfile.write_text("")
        cmd, env, cwd = core.launch_command(tools, "dota2.exe", args)
        wrap = ["sh", "-c", 'env | grep -E "^(DISPLAY|GAMESCOPE_WAYLAND_DISPLAY)=" > "$0"; exec "$@"',
                str(self.envfile)] + cmd
        gs = ["gamescope", "--backend", "headless", "-W", "1920", "-H", "1080", "-w", "1920", "-h", "1080",
              "--"] + wrap
        self.proc = subprocess.Popen(gs, env=env, cwd=cwd, stdout=open(tools.meta / "game.log", "w"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.env = None

    def alive(self):
        return self.proc.poll() is None

    def display(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end and self.alive():
            text = self.envfile.read_text()
            if "DISPLAY=" in text:
                self.env = dict(os.environ, **dict(ln.split("=", 1) for ln in text.split() if "=" in ln))
                return True
            time.sleep(1)
        return False

    def x(self, *args):
        p = subprocess.run(["xdotool", *args], env=self.env, capture_output=True, text=True)
        return p.stdout.strip()

    def windows(self):
        """{window id: name} of the visible windows."""
        out = {}
        for w in self.x("search", "--onlyvisible", "--name", ".").split():
            out[w] = self.x("getwindowname", w)
        return out

    def wait(self, pred, timeout=TIMEOUT_STEP, what="a window"):
        """The first window whose name satisfies pred."""
        end = time.time() + timeout
        while time.time() < end and self.alive():
            for w, name in self.windows().items():
                if pred(name):
                    return w
            time.sleep(1)
        raise Failed(f"no {what} after {timeout} s")

    def key(self, window, *keys, pause=0.3):
        for k in keys:
            self.x("key", "--window", window, k)
            time.sleep(pause)

    def click(self, window, x, y):
        """A click at (x, y) inside the window. It opens no menu here, but it
        makes Wine activate the window, and Qt takes shortcuts only in the
        active one."""
        g = dict(ln.split("=", 1) for ln in self.x("getwindowgeometry", "--shell", window).split())
        self.x("mousemove", "--sync", str(int(g["X"]) + x), str(int(g["Y"]) + y))
        time.sleep(0.3)
        self.x("mousedown", "1")
        time.sleep(0.1)
        self.x("mouseup", "1")
        time.sleep(0.5)

    def type(self, window, text):
        self.x("type", "--window", window, "--delay", "15", text)

    def screenshot(self, path):
        """The hidden display into path (gamescope writes it a moment later)."""
        path = Path(path)
        if path.exists():
            path.unlink()
        subprocess.run(["gamescopectl", "screenshot", str(path)], env=self.env, capture_output=True)
        end = time.time() + 10
        while time.time() < end and not path.exists():
            time.sleep(0.5)
        time.sleep(1)

    def close(self):
        if self.alive():
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


class Failed(Exception):
    pass


def frame_count(frames_dir):
    """How many frames the session in the frames folder exports."""
    from . import maprender
    text = (Path(frames_dir) / "session_text.dmx").read_text(encoding="utf-8")
    return len(maprender.camera_path(text)[0])


def preload(h, sfm, seconds, passes, log, progress=lambda _t, _f: None):
    """Play the shot in SFM's viewport `passes` times before exporting: the
    texture streaming loads what each camera position needs while it plays
    (an export draws too few frames per position for it to catch up).
    Space plays, at the shot's own pace, and playing stops at its end; a
    click in the viewport first makes Wine activate SFM."""
    h.x("windowfocus", "--sync", sfm)
    h.click(sfm, 1280, 350)
    for i in range(passes):
        log(f"preloading textures: playing the shot ({seconds} s){f' {i + 1} of {passes}' if passes > 1 else ''}")
        h.key(sfm, "Home", "space", pause=0.5)
        if os.environ.get("DOTA2TOOLS_SHOT_PLAY"):
            for k in (5, 15):
                time.sleep(5 if k == 5 else 10)
                h.screenshot(os.environ["DOTA2TOOLS_SHOT_PLAY"] + f"_{i}_{k}s.png")
        end = time.time() + seconds + 3
        while time.time() < end:
            progress(f"preloading textures ({i + 1} of {passes})",
                     0.12 * (i + 1 - (end - time.time()) / (seconds + 3)) / passes)
            time.sleep(1)
        h.key(sfm, "Home", pause=0.5)
        if os.environ.get("DOTA2TOOLS_SHOT_PLAY"):
            h.screenshot(os.environ["DOTA2TOOLS_SHOT_PLAY"] + f"_{i}_end.png")


def set_resolution(h, dlg, w, hgt):
    """In the Export Movie dialog (its Output Path focused): the resolution
    preset to its last entry, Custom, then the width and height typed.
    Tab order: path, "...", Export, Format, the three checkboxes, the
    preset; width and height come before it and take focus once Custom
    unlocks them."""
    h.key(dlg, *(["Tab"] * 7), pause=0.25)
    h.key(dlg, "End", pause=0.6)
    for value in (hgt, w):
        h.key(dlg, "shift+Tab", "ctrl+a", pause=0.3)
        h.type(dlg, str(value))
        time.sleep(0.3)


def render(tools, map_name, frames_dir, log=print, progress=lambda _t, _f: None, suffix=""):
    """Export the prepared session's movie (maprender.prepare first) into
    frames_dir, without a window or a click. True when every frame is there."""
    if not available():
        raise SystemExit("needs gamescope and xdotool (pacman -S gamescope xdotool)")
    if not core.steam_running():
        raise SystemExit("Steam is not running: the tools need it")
    frames = Path(frames_dir)
    want = frame_count(frames)
    # The last export's files: SFM would ask before overwriting them.
    for pattern in ("*.png", "*.wav"):
        for old in frames.glob(pattern):
            old.unlink()
    r = tools.settings["render"]
    from . import maprender as _mr
    k = _mr.render_scale(tools)
    size = (round(3840 * k), round(2160 * k))
    session = (tools.root / "game" / "dota_addons" / r["render_addon"] / "elements" / "sessions"
               / f"topdown_{map_name}{suffix}.dmx")
    if not session.exists():
        session = session.with_name(f"topdown_{map_name}{suffix}_text.dmx")
    from . import maprender
    addon = (tools.root / "game" / "dota_addons" / r["render_addon"] / ".dota2tools_source").read_text().strip() \
        if (tools.root / "game" / "dota_addons" / r["render_addon"] / ".dota2tools_source").exists() else ""
    h = Hidden(tools, core.game_args(r["render_addon"], "", False,
                                     "-novid " + maprender.render_convars(tools, addon, map_name)), log)
    try:
        if not h.display():
            raise Failed("gamescope did not start")
        log("the tools are starting in a hidden display")
        progress("starting the tools", 0.0)
        ab = h.wait(lambda n: n == "Asset Browser", TIMEOUT_START, "Asset Browser")
        time.sleep(5)
        log("opening Source Filmmaker")
        progress("opening Source Filmmaker", 0.08)
        h.x("windowfocus", ab)
        h.key(ab, "alt+t", "Right", "Down", "Down", "Down", "Down", "Down", "Return", pause=0.6)
        sfm = h.wait(lambda n: "Source Filmmaker" in n, TIMEOUT_STEP, "Source Filmmaker")
        start = h.wait(lambda n: n == "", TIMEOUT_STEP, "start dialog")
        time.sleep(3)
        h.key(start, *(["Tab"] * 7), "space")
        dlg = h.wait(lambda n: n == "Open Document", TIMEOUT_STEP, "file dialog")
        time.sleep(1)
        h.type(dlg, core.winpath(session))
        h.key(dlg, "Return")
        log(f"opening {session.name}")
        progress(f"loading {session.name}", 0.1)
        sfm = h.wait(lambda n: session.name in n and "Source Filmmaker" in n, TIMEOUT_START, "loaded session")
        if os.environ.get("DOTA2TOOLS_SHOT_LOADED"):
            time.sleep(40)
            h.screenshot(os.environ["DOTA2TOOLS_SHOT_LOADED"])
            raise Failed("stopped with the session loaded (DOTA2TOOLS_SHOT_LOADED)")
        # SFM takes Ctrl+M (Export Movie) only once the map is in: asked
        # again every 10 s until the dialog shows.
        # Qt takes a window's shortcuts only in the active window, and Wine
        # activates one on a click: one in the Element Viewer's empty space.
        def export_dialog():
            exp, end = None, time.time() + TIMEOUT_START
            while exp is None and time.time() < end and h.alive():
                h.x("windowfocus", "--sync", sfm)
                h.click(sfm, 150, 600)
                h.key(sfm, "ctrl+m")
                try:
                    exp = h.wait(lambda n: n.startswith("Export Movie"), 10, "Export Movie dialog")
                except Failed:
                    pass
            if exp is None:
                raise Failed("no Export Movie dialog")
            return exp
        exp = export_dialog()
        passes = int(r.get("preload", 1))
        if passes:
            # The dialog answering shows the map is in: closed again, the
            # shot played, then the dialog anew.
            h.key(exp, "Escape", pause=2)
            preload(h, sfm, want, passes, log, progress)
            exp = export_dialog()
        if size != (3840, 2160):
            set_resolution(h, exp, *size)
            log(f"export resolution {size[0]} x {size[1]}")
        if os.environ.get("DOTA2TOOLS_SHOT_EXPORT"):
            time.sleep(3)
            h.screenshot(os.environ["DOTA2TOOLS_SHOT_EXPORT"])
            raise Failed("stopped at the Export Movie dialog (DOTA2TOOLS_SHOT_EXPORT)")
        known = set(h.windows())
        for attempt in range(4):
            time.sleep(3)
            target = exp
            # A question in between ("This will overwrite an existing
            # layoff. Continue?"): Yes is its default.
            new = [w for w in h.windows() if w not in known]
            if new:
                target = new[-1]
                log("answering SFM's question with its default (Yes)")
            h.key(target, "Return")
            try:
                h.wait(lambda n: n.startswith("Movie Layoff"), 30, "export progress")
                break
            except Failed:
                if attempt == 3:
                    raise
                known = set(h.windows()) - set(new)
        log(f"exporting {want} frames")
        progress(f"exporting frames: 0 / {want}", 0.15)
        end, last, seen = time.time() + max(600, want * 30), time.time(), 0
        while time.time() < end and h.alive():
            n = len(list(frames.glob("*.png")))
            if n != seen:
                seen, last = n, time.time()
                log(f"  {n} / {want}")
                progress(f"exporting frames: {n} / {want}", 0.15 + 0.85 * min(n, want) / want)
            if n >= want and not any(name.startswith("Movie Layoff") for name in h.windows().values()):
                time.sleep(3)
                break
            if time.time() - last > 180:
                raise Failed(f"no new frame for 3 minutes ({n} of {want}); windows: "
                             + ", ".join(sorted(set(h.windows().values()))))
            time.sleep(2)
        n = len(list(frames.glob("*.png")))
        if n < want:
            raise Failed(f"{n} of {want} frames")
        log(f"all {want} frames in {frames}")
        return True
    except Failed as e:
        shot = frames.parent / "render_failed.png"
        h.screenshot(shot)
        raise SystemExit(f"Rendering stopped: {e}. The hidden display at that moment: {shot}")
    finally:
        h.close()


def render_all(tools, map_name, frames_dir, log=print, progress=lambda _t, _f: None):
    """Every pass the prepared render needs: the frames, and their shadowless
    twins when the shadows are to be weaker (maprender.shadow_strength)."""
    from . import maprender
    if maprender.shadow_strength(tools) >= 1:
        return render(tools, map_name, frames_dir, log, progress)
    ok = render(tools, map_name, frames_dir, log, lambda t, f: progress(t, f / 2))
    log("the second pass: without the sun's shadows")
    return ok and render(tools, map_name, maprender.noshadow_dir(frames_dir), log,
                         lambda t, f: progress("shadowless pass: " + t, 0.5 + f / 2), suffix="_noshadow")
