"""The launcher's Map renderer tab (maprender.py does the work)."""
import shutil
import subprocess
from pathlib import Path

from . import core
from . import maprender as mr
from .gui import QtCore, QtWidgets, path_row

BASE = "(base game)"


def num(text, default=0.0):
    try:
        return float(text)
    except ValueError:
        return default


def fold(title, inner):
    """`inner` under a button that shows and hides it (hidden at first)."""
    w = QtWidgets.QWidget()
    v = QtWidgets.QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    b = QtWidgets.QToolButton()
    b.setText(title)
    b.setCheckable(True)
    b.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setArrowType(QtCore.Qt.ArrowType.RightArrow)
    b.setAutoRaise(True)
    inner.setVisible(False)

    def toggle(on):
        inner.setVisible(on)
        b.setArrowType(QtCore.Qt.ArrowType.DownArrow if on else QtCore.Qt.ArrowType.RightArrow)
    b.toggled.connect(toggle)
    v.addWidget(b)
    v.addWidget(inner)
    return w


class RenderTab(QtWidgets.QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        t = app.tools
        r = t.settings["render"]
        outer = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        intro = QtWidgets.QLabel("Renders a map from above in Source Filmmaker and stitches the frames into pictures.")
        intro.setWordWrap(True)
        v.addWidget(intro)

        g = QtWidgets.QGroupBox("Map")
        f = QtWidgets.QFormLayout(g)
        self.c_addon = QtWidgets.QComboBox()
        self.c_addon.addItems([BASE] + [a for a in t.addons() if a != r["render_addon"]])
        f.addRow("Addon", self.c_addon)
        self.c_map = QtWidgets.QComboBox()
        self.c_map.setEditable(True)
        f.addRow("Map", self.c_map)
        v.addWidget(g)

        # Pictures
        g = QtWidgets.QGroupBox("Pictures")
        f = QtWidgets.QFormLayout(g)
        ia, bd, sm, mt = r["full"], r["bounds"], r.get("small", {}), r.get("match", {})
        self.full_note = QtWidgets.QLabel()
        f.addRow("Full picture", self.full_note)
        self.bd_on = QtWidgets.QCheckBox("Cut to the map's bounds:")
        self.bd_on.setChecked(bd.get("enabled", True))
        self.bd_src = QtWidgets.QComboBox()
        self.bd_src.addItems(["minimap", "world", "gridnav", "overview", "custom"])
        self.bd_src.setCurrentText(bd.get("source", "minimap"))
        self.bounds_note = QtWidgets.QLabel()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.bd_on)
        row.addWidget(self.bd_src)
        row.addWidget(self.bounds_note, 1)
        f.addRow(row)
        self.sm_on = QtWidgets.QCheckBox("Save preview:")
        self.sm_on.setChecked(sm.get("enabled", True))
        self.sm_size = QtWidgets.QSpinBox()
        self.sm_size.setRange(256, 16384)
        self.sm_size.setSingleStep(512)
        self.sm_size.setValue(int(sm.get("size", 8192)))
        self.sm_size.setSuffix(" px")
        self.sm_fmt = QtWidgets.QComboBox()
        self.sm_fmt.addItems(["webp", "jpg", "png"])
        self.sm_fmt.setCurrentText(sm.get("format", "webp"))
        self.sm_q = self.quality_box(sm.get("quality", 0))
        row = QtWidgets.QHBoxLayout()
        for w in (self.sm_on, self.sm_size, self.sm_fmt, QtWidgets.QLabel("quality"), self.sm_q):
            row.addWidget(w)
        row.addStretch(1)
        f.addRow(row)
        self.match_mode = QtWidgets.QComboBox()
        for key, label in (("auto", "Automatic"), ("off", "Off: the frames as SFM drew them")):
            self.match_mode.addItem(label, key)
        self.match_mode.setCurrentIndex(max(0, self.match_mode.findData(mt.get("mode", "auto"))))
        self.match_mode.setToolTip("Automatic: neighbouring frames' tones matched to each other where they "
                                   "overlap. Off: the frames as SFM drew them.")
        f.addRow("Tone adjustment", self.match_mode)
        self.trend = QtWidgets.QSpinBox()
        self.trend.setRange(0, 100)
        self.trend.setSuffix(" %")
        self.trend.setValue(round(float(mt.get("trend", 0.5)) * 100))
        self.trend.setToolTip("How far the matching carries colour across the map: neighbouring frames are always "
                              "matched; 0 % leaves each side SFM's own colours, 100 % matches them all to each other "
                              "(Dire turned greener, Radiant colder)")
        self.match_mode.currentIndexChanged.connect(lambda _i: self.trend.setEnabled(self.match_mode.currentData() == "auto"))
        self.trend.setEnabled(self.match_mode.currentData() == "auto")
        f.addRow("Colour across the map", self.trend)
        self.scale = QtWidgets.QComboBox()
        for k, label in ((0.5, "0.5× (half size)"), (1.0, "1×"), (2.0, "2× (and 1× copies)")):
            self.scale.addItem(label, k)
        self.scale.setCurrentIndex(max(0, self.scale.findData(float(r.get("scale", 1.0)))))
        self.scale.setToolTip("frames at this many times the resolution; 2× saves every picture at 2× and 1×, "
                              "0.5× everything at half size")
        self.scale.currentIndexChanged.connect(lambda _i: self.show_grid())
        f.addRow("Scale", self.scale)
        v.addWidget(g)

        # Lighting, for the render only
        g = QtWidgets.QGroupBox("Custom lighting")
        g.setCheckable(True)
        sun = r.get("sun", {})
        g.setChecked(bool(r.get("lighting", False)))
        self.light_box = g
        f = QtWidgets.QFormLayout(g)
        self.shadows = QtWidgets.QSpinBox()
        self.shadows.setRange(0, 100)
        self.shadows.setSuffix(" %")
        self.shadows.setValue(round(float(r.get("shadows", 1.0)) * 100))
        self.shadows.setToolTip("the sun's shadows' strength: 100 % as rendered; below that, SFM exports a "
                                "second pass without them and the two are blended (twice the export time)")
        f.addRow("Shadows", self.shadows)
        self.sun_box = QtWidgets.QCheckBox("Sun direction:")
        self.sun_box.setChecked(bool(sun.get("on", True)) and str(sun.get("pitch", "")).strip() != "")
        self.sun_pitch = QtWidgets.QDoubleSpinBox()
        self.sun_pitch.setRange(5, 90)
        self.sun_pitch.setSuffix("° down")
        self.sun_yaw = QtWidgets.QDoubleSpinBox()
        self.sun_yaw.setRange(-360, 360)
        self.sun_yaw.setSuffix("°")
        self.sun_pitch.setValue(float(sun.get("pitch") or 66))
        self.sun_yaw.setValue(float(sun.get("yaw") or 0))
        row = QtWidgets.QHBoxLayout()
        for w in (self.sun_box, QtWidgets.QLabel("pitch"), self.sun_pitch, QtWidgets.QLabel("yaw"), self.sun_yaw):
            row.addWidget(w)
        row.addStretch(1)
        f.addRow(row)
        self.sun_note = QtWidgets.QLabel()
        self.sun_note.setWordWrap(True)
        f.addRow(self.sun_note)
        self.sun_note2 = QtWidgets.QLabel()
        self.sun_note2.setWordWrap(True)
        f.addRow(self.sun_note2)
        v.addWidget(g)

        # Pre-run scripts
        g = QtWidgets.QGroupBox("Pre-run scripts")
        g.setCheckable(True)
        g.setChecked(r.get("prerun_enabled", True))
        self.prerun_box = g
        gl = QtWidgets.QGridLayout(g)
        self.prerun = QtWidgets.QListWidget()
        self.prerun.addItems(r.get("prerun", []))
        self.prerun.setMaximumHeight(90)
        gl.addWidget(self.prerun, 0, 0, 2, 1)
        b = QtWidgets.QPushButton("Add script…")
        b.clicked.connect(self.add_script)
        gl.addWidget(b, 0, 1)
        b = QtWidgets.QPushButton("Remove")
        b.clicked.connect(lambda: [self.prerun.takeItem(self.prerun.row(i)) for i in self.prerun.selectedItems()])
        gl.addWidget(b, 1, 1)
        v.addWidget(g)

        # More options: overrides
        more = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(more)
        c = mr.FULL
        self.ia_size = QtWidgets.QLineEdit(str(ia.get("size") or c["size"]))
        self.ia_x = [QtWidgets.QLineEdit(str(x)) for x in (ia.get("x") or c["x"])]
        self.ia_y = [QtWidgets.QLineEdit(str(y)) for y in (ia.get("y") or c["y"])]
        f.addRow("Full picture: size (px)", self.ia_size)
        f.addRow("x left, right", self.row(self.ia_x))
        f.addRow("y top, bottom", self.row(self.ia_y))
        self.bd_rect = [QtWidgets.QLineEdit(str(x)) for x in (bd.get("rect") or [0, 0, 0, 0])]
        f.addRow("Custom bounds x0 x1 y0 y1", self.row(self.bd_rect))
        self.bd_upp = QtWidgets.QLineEdit(str(bd.get("upp") or 0))
        f.addRow("Bounds: units per pixel (0: ~1.09)", self.bd_upp)
        cam = r.get("camera", {})
        self.cam_on = QtWidgets.QCheckBox("Camera area x0 x1 y0 y1")
        self.cam_on.setChecked(bool(cam.get("override")))
        self.cam = [QtWidgets.QLineEdit() for _ in range(4)]
        for e, x in zip(self.cam, cam.get("rect") or []):
            e.setText(f"{x:.0f}")
        f.addRow(self.cam_on, self.row(self.cam))
        self.grid_note = QtWidgets.QLabel()
        f.addRow("", self.grid_note)
        for e in self.cam:
            e.textChanged.connect(self.show_grid)
        self.cam_on.toggled.connect(self.cam_toggled)
        self.repeat = QtWidgets.QSpinBox()
        self.repeat.setRange(1, 4)
        self.repeat.setValue(int(r.get("repeat", 2)))
        self.repeat.setToolTip("frames exported per camera position, the sharpest kept: the texture streaming "
                               "falls behind now and then in a long export")
        self.repeat.valueChanged.connect(lambda _v: self.show_grid())
        f.addRow("Frames per position", self.repeat)
        self.preload = QtWidgets.QSpinBox()
        self.preload.setRange(0, 3)
        self.preload.setValue(int(r.get("preload", 1)))
        self.preload.setToolTip("times the shot is played in SFM before the export: the texture streaming "
                                "loads what each camera position needs while it plays")
        self.preload.valueChanged.connect(lambda _v: self.show_grid())
        f.addRow("Plays before export", self.preload)
        self.convars = QtWidgets.QPlainTextEdit(r.get("convars", ""))
        self.convars.setToolTip("console commands the tools start with when rendering")
        self.convars.setFixedHeight(self.convars.fontMetrics().lineSpacing() * 3 + 12)
        f.addRow(QtWidgets.QLabel("Tools' console commands"))
        f.addRow(self.convars)
        v.addWidget(fold("More options", more))

        g = QtWidgets.QGroupBox("Folders")
        f = QtWidgets.QFormLayout(g)
        rw, self.e_frames = path_row()
        f.addRow("Frames", rw)
        rw, self.e_out = path_row()
        f.addRow("Pictures", rw)
        v.addWidget(g)

        # Interactive map tiles
        tl = r.get("tiles", {})
        g = QtWidgets.QGroupBox("Split into tiles")
        f = QtWidgets.QFormLayout(g)
        rw, self.tl_pic = path_row(file=True)
        f.addRow("Picture", rw)
        rw, self.tl_out = path_row()
        f.addRow("Into", rw)
        self.tl_fmt = QtWidgets.QComboBox()
        self.tl_fmt.addItems(["jpg", "webp", "png"])
        self.tl_fmt.setCurrentText(tl.get("format", "jpg"))
        self.tl_q = self.quality_box(tl.get("quality", 0))
        self.tl_note = QtWidgets.QLabel()
        b = QtWidgets.QPushButton("Make tiles")
        b.clicked.connect(lambda _c=False: self.make_tiles())
        row = QtWidgets.QHBoxLayout()
        for w in (self.tl_fmt, QtWidgets.QLabel("quality"), self.tl_q, self.tl_note):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(b)
        f.addRow("Format", row)
        self.tl_pic.textChanged.connect(lambda _t: self.tile_note())
        self.tl_fmt.currentTextChanged.connect(lambda _t: self.tile_note())
        v.addWidget(g)
        v.addStretch(1)

        self.auto_ok = bool(shutil.which("gamescope") and shutil.which("xdotool"))
        bar = QtWidgets.QHBoxLayout()
        self.mode_auto = QtWidgets.QRadioButton("Automatic")
        self.mode_manual = QtWidgets.QRadioButton("Manual")
        mode = QtWidgets.QButtonGroup(self)
        for rb in (self.mode_auto, self.mode_manual):
            mode.addButton(rb)
            bar.addWidget(rb)
        bar.addSpacing(12)
        self.auto_buttons, self.manual_buttons = [], []
        for text, fn, tip, group in (
                ("Render automatically", self.render_auto,
                 "prepare, export in a hidden SFM (gamescope and xdotool), stitch", self.auto_buttons),
                ("1. Prepare", self.prepare, "the render addon and the SFM session", self.manual_buttons),
                ("2. Launch tools for SFM", self.launch, "export the movie in SFM yourself", self.manual_buttons),
                ("3. Stitch", self.stitch, "the exported frames into the pictures", self.manual_buttons)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _checked=False, fn=fn: fn())
            b.setToolTip(tip)
            group.append(b)
            bar.addWidget(b)
        if not self.auto_ok:
            self.auto_buttons[0].setEnabled(False)
            self.auto_buttons[0].setToolTip("needs gamescope and xdotool -- not found")
        bar.addStretch(1)
        b = QtWidgets.QPushButton("Open pictures")
        b.clicked.connect(lambda: subprocess.Popen(["xdg-open", self.e_out.text()]))
        bar.addWidget(b)
        self.mode_auto.toggled.connect(self.set_mode)
        auto = r.get("mode", "auto" if self.auto_ok else "manual") == "auto"
        (self.mode_auto if auto else self.mode_manual).setChecked(True)
        self.set_mode(auto)
        row = bar
        outer.addLayout(row)

        self.bd_src.currentTextChanged.connect(lambda _t: self.bounds_text())
        self.c_addon.currentTextChanged.connect(lambda _t: self.fill_maps())
        self.c_map.currentTextChanged.connect(lambda _t: self.map_changed())
        self.fill_maps()

    @staticmethod
    def row(widgets):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for e in widgets:
            h.addWidget(e)
        return w

    def quiet(self, fn, done):
        """fn() in a thread, done(result) after on the main thread; for the
        tab's own lookups, which shouldn't hold the window busy (switching
        maps quickly asked "Something is still running")."""
        import threading

        def body():
            try:
                result = fn()
            except Exception as e:      # noqa: BLE001 -- a lookup: logged
                self.app.call(self.app.log, f"{e}")
                return
            self.app.call(done, result)
        threading.Thread(target=body, daemon=True).start()

    def set_mode(self, auto):
        for b in self.auto_buttons:
            b.setVisible(auto)
        for b in self.manual_buttons:
            b.setVisible(not auto)

    def addon(self):
        a = self.c_addon.currentText()
        return "" if a == BASE else a

    def fill_maps(self):
        self.c_map.blockSignals(True)
        self.c_map.clear()
        self.c_map.addItems(mr.suggested_maps(self.app.tools, self.addon()))
        self.c_map.blockSignals(False)
        self.map_changed()

    @staticmethod
    def quality_box(value):
        q = QtWidgets.QSpinBox()
        q.setRange(0, 100)
        q.setSpecialValueText("auto")
        q.setToolTip("JPEG/WebP quality; auto: 25 for JPEG (the live interactive map's), 30 for WebP")
        q.setValue(int(value or 0))
        return q

    def map_changed(self):
        m = self.c_map.currentText()
        if not m:
            return
        base = Path.home() / "Pictures" / "dota2-maprender" / (self.addon() or "dota") / m
        self.e_frames.setText(str(base / "frames"))
        self.e_out.setText(str(base))
        self.tl_pic.setText(str(base / f"{m}_full.png"))
        self.tl_out.setText(str(base / f"{m}_tiles"))
        addon = self.addon()
        key = (addon, m)
        current = lambda: (self.addon(), self.c_map.currentText()) == key    # noqa: E731
        # Lookups answer late; one for a map no longer shown is dropped.
        self.quiet(lambda: __import__("dota2tools.mapinfo", fromlist=["x"]).bounds(self.app.tools, addon, m),
                   lambda b: current() and self.show_bounds(b))
        self.quiet(lambda: self.map_sun(addon, m), lambda res: current() and self.show_sun(res))
        self.show_full()

    def map_sun(self, addon, m):
        from . import mapinfo
        own = next((mapinfo._vec(e["angles"])[:2] for e in mapinfo.entities(self.app.tools, addon, m)
                    if e.get("classname") == "env_global_light" and "angles" in e), None)
        return own, mr.map_source(self.app.tools, addon, m) is not None

    def show_sun(self, res):
        own, has_source = res
        self.sun_note.setText((f"This map's own sun: pitch {own[0]:g}°, yaw {own[1]:g}°. " if own else "")
                              + "Dota's: 66°, 0°.")
        self.sun_note2.setText("A copy of the map is rebuilt with this sun for the render (a few minutes, once per "
                               "angle); the map itself stays as it is." if has_source else
                               "This map has no source in its addon to rebuild: its own sun is used.")

    def show_full(self):
        rect, size = mr.full_rect(self.app.tools)
        self.full_note.setText(f"{size} × {size} px, x {rect[0]:.0f} … {rect[1]:.0f}, "
                               f"y {rect[2]:.0f} … {rect[3]:.0f} (the interactive map's square)")

    def show_bounds(self, b):
        if b is None:
            return
        self.bounds = b
        self.bounds_text()
        self.area_from_pictures()

    def bounds_text(self):
        b = getattr(self, "bounds", {}) or {}
        src = self.bd_src.currentText()
        if src == "custom":
            r, used = [num(e.text()) for e in self.bd_rect], src
        else:
            # As maprender.products falls back.
            used = next((k for k in (src, "overview", "gridnav") if b.get(k)), None)
            r = b.get(used) if used else None
        text = f"x {r[0]:.0f} … {r[1]:.0f}, y {r[2]:.0f} … {r[3]:.0f}" if r else "the whole map"
        self.bounds_note.setText(text)

    def save(self):
        s = self.app.tools.settings
        r = s["render"]
        r["full"] = {"enabled": True, "size": int(num(self.ia_size.text(), 20480)),
                     "x": [num(e.text()) for e in self.ia_x], "y": [num(e.text()) for e in self.ia_y]}
        r["bounds"] = {"enabled": self.bd_on.isChecked(), "source": self.bd_src.currentText(),
                       "rect": [num(e.text()) for e in self.bd_rect], "upp": num(self.bd_upp.text())}
        r["small"] = {"enabled": self.sm_on.isChecked(), "size": self.sm_size.value(),
                      "format": self.sm_fmt.currentText(), "quality": self.sm_q.value()}
        r["tiles"] = dict(r.get("tiles", {}), format=self.tl_fmt.currentText(), quality=self.tl_q.value())
        r["match"] = {"mode": self.match_mode.currentData(), "trend": self.trend.value() / 100}
        r["lighting"] = self.light_box.isChecked()
        r["sun"] = {"pitch": self.sun_pitch.value(), "yaw": self.sun_yaw.value(), "on": self.sun_box.isChecked()}
        r["shadows"] = self.shadows.value() / 100
        r["prerun_enabled"] = self.prerun_box.isChecked()
        r["prerun"] = [self.prerun.item(i).text() for i in range(self.prerun.count())]
        r["camera"] = {"override": self.cam_on.isChecked(), "rect": [num(e.text()) for e in self.cam]}
        r["convars"] = " ".join(self.convars.toPlainText().split())
        r["mode"] = "auto" if self.mode_auto.isChecked() else "manual"
        r["scale"] = self.scale.currentData()
        r["repeat"] = self.repeat.value()
        r["preload"] = self.preload.value()
        self.app.tools.save_settings(s)

    def cam_toggled(self, on):
        for e in self.cam:
            e.setEnabled(on)
        if not on:
            self.area_from_pictures()

    def tile_note(self):
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(self.tl_pic.text()) as im:
                size = max(im.size)
            from . import tiles
            n = tiles.levels_for(size)
            self.tl_note.setText(f"{n} levels of 256 px ({size} px picture)")
        except Exception:      # noqa: BLE001 -- no picture yet
            self.tl_note.setText("")

    def make_tiles(self):
        from . import tiles
        self.save()
        pic, out, fmt, q = self.tl_pic.text(), self.tl_out.text(), self.tl_fmt.currentText(), self.tl_q.value()
        if not Path(pic).exists():
            QtWidgets.QMessageBox.warning(self, "Tiles", f"{pic} not found: render or stitch first")
            return
        pr = self.progress(0, (("Tiles", 1.0),))
        self.app.work(lambda: tiles.make(pic, out, fmt, q or None, log=self.log, progress=pr),
                      lambda r: r and self.app.log(f"done: tiles in {r}"))

    def area_from_pictures(self):
        self.save()
        if self.cam_on.isChecked():
            self.show_grid()
            return
        addon, m = self.addon(), self.c_map.currentText()
        self.quiet(lambda: mr.cover(mr.products(self.app.tools, addon, m)),
                   lambda r: (self.addon(), self.c_map.currentText()) == (addon, m) and self.set_area(r))

    def set_area(self, rect):
        if rect and not self.cam_on.isChecked():
            for e, x in zip(self.cam, rect):
                e.setText(f"{x:.0f}")
            for e in self.cam:
                e.setEnabled(False)
        self.show_grid()

    def area(self):
        """The camera area if overridden, else None (the pictures' own)."""
        if not self.cam_on.isChecked():
            return None
        r = tuple(num(e.text(), float("nan")) for e in self.cam)
        return r if all(x == x for x in r) and r[1] > r[0] and r[3] > r[2] else None

    def show_grid(self):
        r = tuple(num(e.text(), float("nan")) for e in self.cam)
        r = r if all(x == x for x in r) and r[1] > r[0] and r[3] > r[2] else None
        if r:
            _, nx, ny = mr.grid(r, self.app.tools.settings["render"].get("overlap", 0.15))
            rep = self.repeat.value() if hasattr(self, "repeat") else 1
            n = nx * ny * rep
            per = 4
            passes = 2 if self.light_box.isChecked() and self.shadows.value() < 100 else 1
            # Each play takes the shot's length: a second a frame.
            plays = (self.preload.value() if hasattr(self, "preload") else 0) * (n + 1) * passes
            self.grid_note.setText(f"{nx} × {ny} positions, {n * passes} frames, about "
                                   f"{int(n * passes * per + plays) // 60 + 1} min to export")

    def add_script(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Pre-run script", str(Path.home()), "Python (*.py)")
        if p:
            self.prerun.addItem(p)

    def log(self, text):
        self.app.call(self.app.log, text)

    # The stages of a render, by their share of its time.
    STAGES = (("Prepare", 0.05), ("Export in SFM", 0.6), ("Stitch", 0.35))

    def progress(self, index, stages=None):
        """A progress callback for stage `index` of `stages` (default: that
        stage alone): it shows the stage and the whole on the window."""
        stages = stages or (self.STAGES[index],)
        i = 0 if len(stages) == 1 else index
        name, share = stages[i]
        before = sum(w for _, w in stages[:i])
        total = sum(w for _, w in stages)

        def cb(text, fraction):
            fraction = max(0.0, min(1.0, fraction))
            self.app.call(self.app.stage, name, i + 1, len(stages), text, fraction,
                          (before + share * fraction) / total)
        return cb

    def prepare(self, then=None, stages=None):
        self.save()
        addon, m, frames, rect = self.addon(), self.c_map.currentText(), self.e_frames.text(), self.area()
        pr = self.progress(0, stages)
        self.app.work(lambda: mr.prepare(self.app.tools, addon, m, frames, rect, self.log, pr),
                      lambda r: r and (then() if then else self.app.log(mr.sfm_steps(self.app.tools, m))),
                      notify=f"Render of {m}")

    def launch(self):
        r = self.app.tools.settings["render"]
        self.app.launch(addon=r["render_addon"], map_name="",
                        extra=mr.render_convars(self.app.tools, self.addon(), self.c_map.currentText()))

    def render_auto(self):
        from . import sfmauto
        m, frames = self.c_map.currentText(), self.e_frames.text()
        pr = self.progress(1, self.STAGES)
        self.prepare(stages=self.STAGES, then=lambda: self.app.work(
            lambda: sfmauto.render_all(self.app.tools, m, frames, self.log, pr),
            lambda r: r and self.stitch(stages=self.STAGES), notify=f"Render of {m}"))

    def stitch(self, stages=None):
        self.save()
        addon, m, frames, out = self.addon(), self.c_map.currentText(), self.e_frames.text(), self.e_out.text()
        pr = self.progress(2, stages)
        def done(r):
            if r:
                self.app.log("done: " + ", ".join(str(p) for p in r))
                core.notify(f"Render of {m} done", "\n".join(Path(p).name for p in r) + f"\nin {out}")
        self.app.work(lambda: mr.stitch(self.app.tools, frames, out, addon, m, log=self.log, progress=pr),
                      done, notify=f"Render of {m}")
