"""The windows (Qt: PySide6, else PyQt6 -- the system's, so they look native
on KDE and elsewhere): the installer while nothing is installed, then the
launcher.

Work runs in a thread; whatever it needs from the user (DepotDownloader's
password, Steam Guard code, QR code) is asked on the main thread through
App.call.
"""
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        raise SystemExit("The windows need Qt for Python: PySide6 or PyQt6 (pacman -S pyside6, or python-pyqt6). "
                         "The commands work without it: dota2-tools --help")

from . import core, proton

Qt = QtCore.Qt


# --- DepotDownloader in a window ----------------------------------------------

class DDRunner:
    """core's UI for downloads: DepotDownloader's output into the log, its
    progress into the bar, its questions as dialogs. It reads answers from a
    pipe when its input is not a terminal, so a pipe is all it takes."""
    QR_START = "Use the Steam Mobile App to sign in with this QR code:"

    def __init__(self, app):
        self.app = app

    def say(self, text):
        self.app.call(self.app.log, text)

    def run(self, cmd):
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             bufsize=0)
        buf, qr, last = b"", None, -10.0
        while True:
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if qr is not None:
                    if line.strip() and set(line) <= {"█", " "}:
                        qr.append(line)
                        continue
                    if qr:
                        self.app.call(self.app.show_qr, qr)
                    qr = None
                if line.startswith(self.QR_START):
                    qr = []
                    self.say("Scan the QR code with the Steam mobile app")
                    continue
                m = re.match(r"\s*(\d{1,3}\.\d+)%", line)
                if m:
                    pct = float(m.group(1))
                    self.app.call(self.app.progress, pct)
                    if pct - last < 5 and pct < 100:
                        continue
                    last = pct
                m = re.search(r"login with -username (\S+) -remember-password", line)
                if m:
                    s = self.app.tools.settings
                    s["username"] = m.group(1)
                    self.app.tools.save_settings(s)
                if "Use the Steam Mobile App to confirm" in line:
                    self.app.call(self.app.note, "Approve the sign-in in the Steam mobile app")
                if line.startswith(("Got AppInfo", "Got depot key")):
                    self.app.call(self.app.hide_qr)
                self.say(line)
            tail = buf.decode("utf-8", "replace")
            if "Enter account password for" in tail:
                kind = ("Steam login", tail.strip(), True)
            elif re.search(r"2.factor auth code|auth code sent to the email|authentication code sent to your email",
                           tail):
                kind = ("Steam Guard", tail.replace("STEAM GUARD!", "").strip(), False)
            else:
                continue
            answer = self.app.call(self.app.ask, *kind)
            self.say(tail.strip())
            buf = b""
            if answer is None:          # cancelled
                p.kill()
                break
            p.stdin.write((answer + "\n").encode())
            p.stdin.flush()
        p.wait()
        self.app.call(self.app.hide_qr)
        return p.returncode


# --- shared window parts --------------------------------------------------------

class Bridge(QtCore.QObject):
    """Calls from worker threads, run on the main thread."""
    try:
        run = QtCore.Signal(object)
    except AttributeError:
        run = QtCore.pyqtSignal(object)


class QRWidget(QtWidgets.QWidget):
    """DepotDownloader's QR code (two characters a module) drawn as squares."""
    def __init__(self, rows):
        super().__init__()
        self.cells = []
        self.set_rows(rows)

    def set_rows(self, rows):
        self.cells = [[row[i:i + 2] == "██" for i in range(0, len(row) - 1, 2)] for row in rows]
        n = max((len(r) for r in self.cells), default=1)
        self.setFixedSize(n * 7 + 28, len(self.cells) * 7 + 28)
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("white"))
        for y, row in enumerate(self.cells):
            for x, dark in enumerate(row):
                if dark:
                    p.fillRect(14 + x * 7, 14 + y * 7, 7, 7, QtGui.QColor("black"))


def banner(tools):
    """The Workshop Tools banner, as Dota's own tools launcher shows it."""
    p = tools.root / "game" / "core" / "tools" / "images" / "common" / "workshop_banner.png"
    lab = QtWidgets.QLabel()
    if p.exists():
        lab.setPixmap(QtGui.QPixmap(str(p)))
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setStyleSheet("background: #1b1b1b;")
    else:
        lab.setText("<h2>Dota 2 Workshop Tools</h2>")
    return lab


def path_row(text="", file=False):
    """An entry with a Browse button (a folder, or a picture file)."""
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    e = QtWidgets.QLineEdit(text)
    b = QtWidgets.QPushButton("Browse…")
    if file:
        b.clicked.connect(lambda: (lambda d: d and e.setText(d))(QtWidgets.QFileDialog.getOpenFileName(
            w, "Picture", e.text() or str(Path.home()), "Pictures (*.png *.jpg *.jpeg *.tif *.tiff)")[0]))
    else:
        b.clicked.connect(lambda: (lambda d: d and e.setText(d))(
            QtWidgets.QFileDialog.getExistingDirectory(w, "Folder", e.text() or str(Path.home()))))
    h.addWidget(e, 1)
    h.addWidget(b)
    return w, e


class App(QtWidgets.QMainWindow):
    def __init__(self, tools):
        super().__init__()
        self.tools = tools
        self.busy = False
        self.qr = None
        self.bridge = Bridge()
        self.bridge.run.connect(lambda f: f())
        self.setWindowTitle("Dota 2 Workshop Tools")
        icon = core.dota_icon(core.STEAM_ROOT)
        if icon:
            self.setWindowIcon(QtGui.QIcon(icon))
        self.logbox = QtWidgets.QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(5000)
        self.logbox.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(8)
        self.bar.hide()
        # A long job of stages (the map renderer): its stage and the stage's
        # progress, above the overall bar.
        self.stage_label = QtWidgets.QLabel()
        self.stage_bar = QtWidgets.QProgressBar()
        self.stage_bar.setRange(0, 1000)
        self.stage_label.hide()
        self.stage_bar.hide()
        # The log stays hidden: one line shows the latest message, and the
        # Log button (or an error) opens the rest.
        self.last = QtWidgets.QLabel()
        self.last.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.log_button = QtWidgets.QToolButton()
        self.log_button.setText("Log")
        self.log_button.setCheckable(True)
        self.log_button.toggled.connect(self.show_log)
        self.logbox.hide()

    # threads
    def call(self, fn, *args):
        """Run fn on the main thread; from a worker, wait for its result."""
        if threading.current_thread() is threading.main_thread():
            return fn(*args)
        box, done = [], threading.Event()

        def go():
            try:
                box.append(fn(*args))
            finally:
                done.set()
        self.bridge.run.emit(go)
        done.wait()
        return box[0] if box else None

    def work(self, fn, done=None, notify=None):
        """fn() in a thread; its errors shown; done(result) on the main thread.
        notify: what the job is, for a desktop notification if it fails."""
        if self.busy:
            QtWidgets.QMessageBox.information(self, "Busy", "Something is still running.")
            return
        self.busy = True
        self.bar.setValue(0)
        self.bar.show()

        def body():
            result = None
            try:
                result = fn()
            except core.NeedsClient as e:
                self.call(self.needs_client, e)
            except SystemExit as e:
                self.call(self.log, f"stopped: {e}")
                if notify:
                    core.notify(f"{notify}: stopped", str(e))
                self.call(QtWidgets.QMessageBox.warning, self, "Dota 2 Workshop Tools", str(e))
            except Exception as e:      # noqa: BLE001 -- shown, not swallowed
                self.call(self.log, f"error: {e!r}")
                if notify:
                    core.notify(f"{notify}: error", repr(e))
                self.call(QtWidgets.QMessageBox.critical, self, "Dota 2 Workshop Tools", repr(e))
            finally:
                self.busy = False
                self.call(self.progress, None)
                if done:
                    self.call(done, result)
        threading.Thread(target=body, daemon=True).start()

    def say(self, text):            # core's UI (link, renders)
        self.call(self.log, text)

    # main-thread helpers
    def log(self, text):
        text = str(text)
        self.logbox.appendPlainText(text)
        line = text.strip().splitlines()[-1] if text.strip() else ""
        if line:
            fm = self.last.fontMetrics()
            self.last.setText(fm.elidedText(line, Qt.TextElideMode.ElideMiddle, max(200, self.last.width())))
            self.last.setToolTip(line)
        if text.startswith(("error:", "stopped:")):
            self.log_button.setChecked(True)

    def show_log(self, on):
        self.logbox.setVisible(on)

    def log_strip(self):
        """The status line, the Log button, the progress bar and the log."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.last, 1)
        row.addWidget(self.log_button)
        v.addLayout(row)
        v.addWidget(self.stage_label)
        v.addWidget(self.stage_bar)
        v.addWidget(self.bar)
        v.addWidget(self.logbox, 1)
        return w

    def progress(self, pct):
        self.bar.setVisible(pct is not None or self.busy)
        self.bar.setValue(0 if pct is None else int(pct))
        if pct is None and not self.busy:
            self.stage_label.hide()
            self.stage_bar.hide()
            self.bar.setTextVisible(False)
            self.bar.setMaximumHeight(8)

    def stage(self, name, index, count, text, fraction, overall):
        """Stage `index` of `count` (`name`), at `fraction` of itself (`text`:
        what it is doing), the whole at `overall`."""
        self.stage_label.setText(f"<b>{name}</b> ({index} of {count}): {text}")
        self.stage_label.show()
        self.stage_bar.setFormat(f"{name}: {fraction * 100:.0f}%")
        self.stage_bar.setValue(int(fraction * 1000))
        self.stage_bar.show()
        self.bar.setMaximumHeight(16777215)
        self.bar.setTextVisible(True)
        self.bar.setFormat(f"overall: {overall * 100:.0f}%")
        self.bar.setValue(int(overall * 100))
        self.bar.show()

    def note(self, text):
        self.log(text)
        if self.qr is None:
            QtWidgets.QMessageBox.information(self, "Steam Guard", text)

    def ask(self, title, prompt, secret):
        text, ok = QtWidgets.QInputDialog.getText(
            self, title, prompt,
            QtWidgets.QLineEdit.EchoMode.Password if secret else QtWidgets.QLineEdit.EchoMode.Normal)
        return text if ok else None

    def show_qr(self, rows):
        if self.qr is None:
            self.qr = QtWidgets.QDialog(self)
            self.qr.setWindowTitle("Sign in with the Steam mobile app")
            v = QtWidgets.QVBoxLayout(self.qr)
            v.addWidget(QtWidgets.QLabel("Steam app → Steam Guard → scan this QR code"))
            self.qr_widget = QRWidget(rows)
            v.addWidget(self.qr_widget, 0, Qt.AlignmentFlag.AlignCenter)
            self.qr.show()
        else:
            self.qr_widget.set_rows(rows)
        self.qr.raise_()

    def hide_qr(self):
        if self.qr is not None:
            self.qr.close()
            self.qr = None

    def needs_client(self, e):
        """The tools DLC from the Steam client's console: the command to copy."""
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Download the Workshop Tools with Steam")
        v = QtWidgets.QVBoxLayout(d)
        lab = QtWidgets.QLabel(
            "The Workshop Tools DLC needs your Steam account. Without logging in here, your running Steam client "
            "can download it: open its console, paste this command, wait for “Depot download complete”, then "
            "press Continue.")
        lab.setWordWrap(True)
        v.addWidget(lab)
        cmd = QtWidgets.QLineEdit(e.command)
        cmd.setReadOnly(True)
        v.addWidget(cmd)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Copy", lambda: QtWidgets.QApplication.clipboard().setText(e.command)),
                         ("Open Steam console", lambda: subprocess.Popen(["xdg-open", "steam://open/console"])),
                         ("Continue", lambda: (d.accept(), self.update_tools(use_client=True)))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        d.show()

    def update_tools(self, use_client=False, qr=False, then=None):
        runner = DDRunner(self)
        self.work(lambda: core.update(self.tools, ui=runner, use_client=use_client, qr=qr),
                  lambda _r: (self.refresh(), then and then()))

    def refresh(self):
        pass


# --- the installer ------------------------------------------------------------------

class Installer(App):
    def __init__(self, tools):
        super().__init__(tools)
        steam = core.STEAM_ROOT
        try:
            native = str(core.find_native(steam)[0])
        except SystemExit:
            native = ""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(banner(tools))
        intro = QtWidgets.QLabel(
            "Native Dota 2 stays as Steam installs it. The Windows Workshop Tools go in a folder of their own: "
            "only the Windows-specific files are downloaded (about 2 GB); the rest of the game is linked from the "
            "native install.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        g = QtWidgets.QGroupBox("Folders")
        f = QtWidgets.QFormLayout(g)
        r, self.e_steam = path_row(str(steam))
        f.addRow("Steam", r)
        r, self.e_native = path_row(native)
        f.addRow("Dota 2 (native)", r)
        r, self.e_dir = path_row(str(tools.root))
        f.addRow("Workshop Tools", r)
        self.protons = {p['name']: p['path'] for p in proton.installed(steam)}
        self.c_proton = QtWidgets.QComboBox()
        self.c_proton.addItems(list(self.protons))
        if "Proton - Experimental" in self.protons:
            self.c_proton.setCurrentText("Proton - Experimental")
        if self.protons:
            f.addRow("Proton", self.c_proton)
        else:
            b = QtWidgets.QPushButton("Install Proton Experimental in Steam")
            b.clicked.connect(lambda: proton.steam_install(proton.RECOMMENDED))
            f.addRow("Proton", b)
        v.addWidget(g)
        g = QtWidgets.QGroupBox("The Workshop Tools DLC (free, but needs the account that has it)")
        gl = QtWidgets.QVBoxLayout(g)
        self.r_login = {}
        for key, text in (("account", "Steam account: password and Steam Guard asked once, then remembered"),
                          ("qr", "QR code: approve in the Steam mobile app, then remembered"),
                          ("client", "No login here: the Steam client downloads it (a command to paste in its "
                                     "console; again after Dota updates the tools)")):
            rb = QtWidgets.QRadioButton(text)
            gl.addWidget(rb)
            self.r_login[key] = rb
        self.r_login["account"].setChecked(True)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Account name"))
        self.e_user = QtWidgets.QLineEdit(tools.settings.get("username", ""))
        row.addWidget(self.e_user)
        gl.addLayout(row)
        v.addWidget(g)
        row = QtWidgets.QHBoxLayout()
        self.b_steam = QtWidgets.QPushButton("Add to Steam")
        self.b_steam.setEnabled(False)
        self.b_steam.clicked.connect(self.add_steam)
        self.b_open = QtWidgets.QPushButton("Open the launcher")
        self.b_open.setEnabled(False)
        self.b_open.clicked.connect(self.open_launcher)
        self.b_install = QtWidgets.QPushButton("Install")
        self.b_install.setDefault(True)
        self.b_install.clicked.connect(self.install)
        row.addWidget(self.b_steam)
        row.addWidget(self.b_open)
        row.addStretch(1)
        row.addWidget(self.b_install)
        v.addLayout(row)
        v.addWidget(self.log_strip(), 1)
        self.log_button.setChecked(True)          # the installer's log is its progress
        self.setCentralWidget(w)
        self.resize(760, 760)

    def install(self):
        login = next(k for k, rb in self.r_login.items() if rb.isChecked())
        if login != "client" and not self.e_user.text().strip():
            QtWidgets.QMessageBox.warning(self, "Account", "The account name is needed (the login is remembered "
                                                           "under it).")
            return
        self.tools = core.Tools(self.e_dir.text())
        s = self.tools.settings
        s["steam"] = self.e_steam.text()
        try:
            detected = str(core.find_native(self.e_steam.text())[0])
        except SystemExit:
            detected = ""
        s["native"] = "" if self.e_native.text() == detected else self.e_native.text()
        s["proton"] = self.protons.get(self.c_proton.currentText(), s["proton"])
        if login != "client":
            s["username"] = self.e_user.text().strip()
        self.tools.save_settings(s)
        self.b_install.setEnabled(False)
        self.update_tools(use_client=login == "client", qr=login == "qr", then=self.finished)

    def finished(self):
        self.b_install.setEnabled(True)
        if self.tools.installed:
            self.log("Installed. The launcher is in the menu as “Dota 2 Workshop Tools”.")
            self.b_steam.setEnabled(True)
            self.b_open.setEnabled(True)

    def add_steam(self):
        try:
            QtWidgets.QMessageBox.information(self, "Steam", core.add_to_steam(self.tools))
        except SystemExit as e:
            QtWidgets.QMessageBox.warning(self, "Steam", str(e))

    def open_launcher(self):
        self.launcher = Launcher(self.tools)
        self.launcher.show()
        self.close()


# --- the launcher ----------------------------------------------------------------------

class Launcher(App):
    def __init__(self, tools):
        super().__init__(tools)
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(banner(tools))
        top = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("checking the builds…")
        top.addWidget(self.status, 1)
        for text, fn in (("Check", self.refresh), ("Update", lambda: self.update_tools()),
                         ("Settings", lambda: SettingsDialog(self).exec())):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        v.addLayout(top)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.launch_tab(), "Addons")
        from .rendertab import RenderTab
        self.render_tab = RenderTab(self)
        tabs.addTab(self.render_tab, "Map renderer")
        tabs.addTab(self.proton_tab(), "Proton")
        tabs.addTab(self.tools_tab(), "Tools")
        v.addWidget(tabs, 3)
        v.addWidget(self.log_strip(), 0)
        self.setCentralWidget(w)
        self.resize(820, 860)
        QtCore.QTimer.singleShot(100, lambda: self.refresh(auto=True))

    # builds
    def refresh(self, auto=False):
        try:
            tgt, state, current = self.tools.status()
        except Exception as e:      # noqa: BLE001
            self.status.setText(f"Cannot read Steam's data: {e}")
            return
        text = f"Dota 2 build {tgt['buildid']}  ·  tools build {state.get('buildid', 'none')}  ·  "
        if not tgt["installed"] or tgt["buildid"] != tgt["current"]:
            text += "<b>Dota has an update pending: update it in Steam first</b>"
        elif current:
            text += "up to date"
        else:
            text += "<b>update needed</b>"
            if auto and self.tools.settings.get("auto_update", True):
                self.log("The native build moved on: updating the tools.")
                QtCore.QTimer.singleShot(10, lambda: self.update_tools())
        self.status.setText(text)

    # Addons tab: a list as in Dota's own tools launcher
    def launch_tab(self):
        s = self.tools.settings["launch"]
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        self.addons = QtWidgets.QListWidget()
        render = self.tools.settings["render"]["render_addon"]
        self.addons.addItems([a for a in self.tools.addons() if a != render])
        self.addons.itemDoubleClicked.connect(lambda _i: self.launch())
        self.addons.currentTextChanged.connect(self.fill_maps)
        h.addWidget(self.addons, 1)
        side = QtWidgets.QVBoxLayout()
        b = QtWidgets.QPushButton("Launch Tools")
        b.setDefault(True)
        b.setMinimumHeight(40)
        b.clicked.connect(lambda: self.launch())
        side.addWidget(b)
        f = QtWidgets.QFormLayout()
        self.c_map = QtWidgets.QComboBox()
        f.addRow("Start on map", self.c_map)
        self.vcon = QtWidgets.QCheckBox("VConsole")
        self.vcon.setChecked(s.get("vconsole", True))
        f.addRow("", self.vcon)
        self.extra = QtWidgets.QLineEdit(s.get("extra", ""))
        f.addRow("Arguments", self.extra)
        self.vcon_term = QtWidgets.QCheckBox("Open the console in a terminal")
        self.vcon_term.setChecked(s.get("vconsole_terminal", False))
        self.vcon_term.setEnabled(self.vcon.isChecked())
        self.vcon.toggled.connect(self.vcon_term.setEnabled)
        f.addRow("", self.vcon_term)
        self.tray_cb = QtWidgets.QCheckBox("Hide to the tray while they run")
        self.tray_cb.setChecked(s.get("tray", True))
        f.addRow("", self.tray_cb)
        side.addLayout(f)
        for text, fn in (("New addon…", self.new_addon),
                         ("Open content folder", lambda: self.open_addon("content")),
                         ("Open game folder", lambda: self.open_addon("game")),
                         ("Dota's tools launcher", lambda: self.launch(cfg=True)),
                         ("Console in a terminal", self.open_console)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        h.addLayout(side)
        items = self.addons.findItems(s.get("addon", ""), Qt.MatchFlag.MatchExactly)
        self.addons.setCurrentItem(items[0] if items else self.addons.item(0))
        if s.get("map"):
            self.c_map.setCurrentText(s["map"])
        return w

    def open_console(self):
        try:
            core.open_vconsole(self.tools)
        except (SystemExit, OSError) as e:
            QtWidgets.QMessageBox.warning(self, "Terminal", f"{e}\n\nName a terminal in the settings.")

    def addon(self):
        it = self.addons.currentItem()
        return it.text() if it else ""

    def fill_maps(self, addon):
        self.c_map.clear()
        self.c_map.addItems([""] + self.tools.maps(addon))

    def new_addon(self):
        """A new addon from a base (Dota's template by default)."""
        render = self.tools.settings["render"]["render_addon"]
        d = NewAddonDialog(self, self.tools, [a for a in self.tools.addons() if a != render])
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        v = d.values()

        def made(_r):
            self.addons.clear()
            self.addons.addItems([a for a in self.tools.addons() if a != render])
            items = self.addons.findItems(v["folder"], Qt.MatchFlag.MatchExactly)
            if items:
                self.addons.setCurrentItem(items[0])
            self.log(f"New addon {v['folder']} (from {v['base']}), map {v['map_name']}")
        self.work(lambda: core.create_addon(self.tools, **v), made)

    def open_addon(self, kind):
        a = self.addon()
        subprocess.Popen(["xdg-open", str(self.tools.root / kind / "dota_addons" / a if a else self.tools.root / kind)])

    def launch(self, cfg=False, addon=None, map_name=None, extra=None):
        """The tools, through Proton. Returns the process."""
        s = self.tools.settings
        if addon is None:
            s["launch"] = {"addon": self.addon(), "map": self.c_map.currentText(), "vconsole": self.vcon.isChecked(),
                           "extra": self.extra.text(), "tray": self.tray_cb.isChecked(),
                           "vconsole_terminal": self.vcon_term.isChecked()}
            self.tools.save_settings(s)
        if not core.steam_running():
            QtWidgets.QMessageBox.warning(self, "Steam", "Steam is not running: the tools need it.")
            return None
        if not self.proton_ready():
            return None
        if cfg:
            exe, args = "dota2cfg.exe", ["-steam", "-dx11"]
        else:
            exe = "dota2.exe"
            args = core.game_args(self.addon() if addon is None else addon,
                                  self.c_map.currentText() if map_name is None else map_name,
                                  self.vcon.isChecked() if addon is None else True,
                                  self.extra.text() if extra is None else extra)
        cmd, env, cwd = core.launch_command(self.tools, exe, args)
        logf = open(self.tools.meta / "game.log", "w")
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        self.log(f"started {exe} {' '.join(args)} (output: {self.tools.meta / 'game.log'})")
        if addon is None and not cfg and self.vcon.isChecked() and self.vcon_term.isChecked():
            self.open_console()
        if addon is None and not cfg and self.tray_cb.isChecked():
            self.to_tray(p)
        return p

    def proton_ready(self):
        """Proton and the runtime there, or the user told what to install."""
        found = proton.problems(self.tools)
        if not found:
            return True
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Proton")
        box.setText("\n".join(text for text, _ in found))
        installs = [appid for _, appid in found if appid]
        if installs:
            box.setInformativeText("Steam can install what's missing; launch again when it's done.")
            install = box.addButton("Install in Steam", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if installs and box.clickedButton() is install:
            for appid in installs:
                proton.steam_install(appid)
        return False

    def to_tray(self, p):
        """Out of the way while the tools run (the tray, else minimised);
        back when they exit."""
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            if not getattr(self, "tray", None):
                icon = self.windowIcon()
                if icon.isNull():
                    icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon)
                self.tray = QtWidgets.QSystemTrayIcon(icon, self)
                self.tray.setToolTip("Dota 2 Workshop Tools")
                menu = QtWidgets.QMenu(self)
                menu.addAction("Show the launcher", self.from_tray)
                menu.addAction("Quit", QtWidgets.QApplication.quit)
                self.tray.setContextMenu(menu)
                self.tray.activated.connect(lambda _r: self.from_tray())
            self.tray.show()
            self.hide()
        else:
            self.showMinimized()
        timer = QtCore.QTimer(self)

        def check():
            if p.poll() is not None:
                timer.stop()
                self.from_tray()
        timer.timeout.connect(check)
        timer.start(3000)

    def from_tray(self):
        if getattr(self, "tray", None):
            self.tray.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # Proton tab: the installed versions, the one in use, installing others
    def proton_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.p_list = QtWidgets.QTreeWidget()
        self.p_list.setHeaderLabels(["Proton", "Version", "From"])
        self.p_list.setRootIsDecorated(False)
        self.p_list.itemDoubleClicked.connect(lambda _i, _c: self.proton_use())
        v.addWidget(self.p_list, 1)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Use", self.proton_use), ("Remove", self.proton_remove), ("Refresh", self.proton_fill)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        g = QtWidgets.QGroupBox("Install")
        f = QtWidgets.QFormLayout(g)
        self.p_steam = QtWidgets.QComboBox()
        b = QtWidgets.QPushButton("Install in Steam")
        b.clicked.connect(lambda: self.p_steam.currentData() and proton.steam_install(self.p_steam.currentData()))
        f.addRow("Steam's", self.row(self.p_steam, b))
        self.p_ge = QtWidgets.QComboBox()
        self.p_ge.addItem("latest", None)
        fetch = QtWidgets.QPushButton("List releases")
        fetch.clicked.connect(self.proton_ge_list)
        b = QtWidgets.QPushButton("Install")
        b.clicked.connect(self.proton_ge_install)
        f.addRow("GE-Proton", self.row(self.p_ge, fetch, b))
        self.p_runtime = QtWidgets.QLabel()
        self.p_runtime_b = QtWidgets.QPushButton("Install in Steam")
        self.p_runtime_b.clicked.connect(lambda: proton.steam_install(proton.SNIPER))
        f.addRow("Runtime", self.row(self.p_runtime, self.p_runtime_b))
        v.addWidget(g)
        self.proton_fill()
        return w

    @staticmethod
    def row(*widgets):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for i, x in enumerate(widgets):
            h.addWidget(x, 1 if i == 0 else 0)
        return w

    def proton_fill(self):
        steam, use = self.tools.steam, core.proton_script(self.tools)
        self.p_list.clear()
        for p in proton.installed(steam):
            src = "custom" if p["appid"] is None else "Steam"
            it = QtWidgets.QTreeWidgetItem([p["name"] + ("  (in use)" if p["path"] == use else ""), p["version"], src])
            it.setData(0, Qt.ItemDataRole.UserRole, p)
            if p["path"] == use:
                f = it.font(0)
                f.setBold(True)
                it.setFont(0, f)
            self.p_list.addTopLevelItem(it)
        for c in range(3):
            self.p_list.resizeColumnToContents(c)
        self.p_steam.clear()
        for b in proton.steam_builds(steam):
            if not b["installed"]:
                self.p_steam.addItem(b["name"] + (" (recommended)" if b["appid"] == proton.RECOMMENDED else ""),
                                     b["appid"])
        has = proton.runtime_installed(steam)
        self.p_runtime.setText("Steam Linux Runtime 3.0 (sniper): " + ("installed" if has else "missing"))
        self.p_runtime_b.setVisible(not has)

    def proton_selected(self):
        it = self.p_list.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def proton_use(self):
        p = self.proton_selected()
        if p:
            s = self.tools.settings
            s["proton"] = p["path"]
            self.tools.save_settings(s)
            self.log(f"Proton: {p['name']}")
            self.proton_fill()

    def proton_remove(self):
        p = self.proton_selected()
        if not p:
            return
        if p["appid"] is not None:
            proton.steam_uninstall(p["appid"])
            return
        if not p["custom"]:
            QtWidgets.QMessageBox.information(self, "Proton", f"{p['name']} is installed for the whole system.")
            return
        if QtWidgets.QMessageBox.question(self, "Proton", f"Delete {p['name']}?") \
                == QtWidgets.QMessageBox.StandardButton.Yes:
            proton.remove_custom(self.tools.steam, p["name"])
            self.proton_fill()

    def proton_ge_list(self):
        def done(releases):
            if releases:
                self.p_ge.clear()
                for r in releases:
                    self.p_ge.addItem(r["tag"], r["tag"])
        self.work(lambda: proton.ge_releases(), done)

    def proton_ge_install(self):
        tag = self.p_ge.currentData()
        self.work(lambda: proton.install_ge(self.tools.steam, tag, say=self.say,
                                            progress=lambda f: self.call(self.progress, f * 100)),
                  lambda path: (path and self.log(f"installed: {path}"), self.proton_fill()))

    # Tools tab
    def tools_tab(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        items = [
            ("Add to Steam", "the launcher as a non-Steam game (Steam must be closed)", self.add_steam),
            ("Relink native files", "link the native install's files again (after verifying Dota in Steam)",
             lambda: self.work(lambda: core.link(self.tools, self.tools.native, self))),
            ("Reinstall menu entry", "the desktop menu entry and the launcher in the tools folder",
             lambda: (core.install_app(self.tools), self.log("menu entry written"))),
            ("Open tools folder", str(self.tools.root), lambda: subprocess.Popen(["xdg-open", str(self.tools.root)])),
            ("Open Dota folder", "the native install", lambda: subprocess.Popen(["xdg-open", str(self.tools.native)])),
            ("Steam console", "where download_depot commands go",
             lambda: subprocess.Popen(["xdg-open", "steam://open/console"])),
        ]
        for i, (text, note, fn) in enumerate(items):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            g.addWidget(b, i, 0)
            g.addWidget(QtWidgets.QLabel(note), i, 1)
        g.setRowStretch(len(items), 1)
        g.setColumnStretch(1, 1)
        return w

    def add_steam(self):
        try:
            QtWidgets.QMessageBox.information(self, "Steam", core.add_to_steam(self.tools))
        except SystemExit as e:
            QtWidgets.QMessageBox.warning(self, "Steam", str(e))


class NewAddonDialog(QtWidgets.QDialog):
    """Folder, display name, map name, and the addon (and its map) to start
    from -- Dota's addon_template unless another is picked."""

    def __init__(self, parent, tools, addons):
        super().__init__(parent)
        self.tools = tools
        self.setWindowTitle("New addon")
        f = QtWidgets.QFormLayout(self)
        self.base = QtWidgets.QComboBox()
        self.base.addItems(addons)
        self.base.setCurrentText(core.TEMPLATE_ADDON if core.TEMPLATE_ADDON in addons else (addons[0] if addons else ""))
        f.addRow("Start from addon", self.base)
        self.base_map = QtWidgets.QComboBox()
        f.addRow("Its map", self.base_map)
        self.base.currentTextChanged.connect(self.fill)
        self.fill(self.base.currentText())
        self.folder = QtWidgets.QLineEdit()
        self.folder.setPlaceholderText("my_addon")
        self.folder.setToolTip("game/ and content/dota_addons/<folder>: lowercase letters, digits and _")
        f.addRow("Folder name", self.folder)
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText("My Addon")
        self.title.setToolTip("the addon's name in the game (resource/addon_english.txt)")
        f.addRow("Addon name", self.title)
        self.map_name = QtWidgets.QLineEdit()
        self.map_name.setPlaceholderText("(the folder name)")
        f.addRow("Map name", self.map_name)
        self.note = QtWidgets.QLabel()
        self.note.setStyleSheet("color: palette(placeholder-text)")
        f.addRow(self.note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.check)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.folder.textChanged.connect(self.update_note)
        self.map_name.textChanged.connect(self.update_note)
        self.update_note()

    def fill(self, addon):
        self.base_map.clear()
        self.base_map.addItems(self.tools.source_maps(addon))

    def values(self):
        folder = self.folder.text().strip()
        return {"folder": folder, "title": self.title.text().strip(),
                "map_name": self.map_name.text().strip() or folder,
                "base": self.base.currentText(), "base_map": self.base_map.currentText()}

    def problem(self):
        v = self.values()
        if not v["folder"]:
            return "a folder name"
        if not core.ADDON_NAME.match(v["folder"]):
            return "folder name: lowercase letters, digits and _ only"
        if not core.ADDON_NAME.match(v["map_name"]):
            return "map name: lowercase letters, digits and _ only"
        if v["folder"] in self.tools.addons():
            return f"{v['folder']} exists already"
        return ""

    def update_note(self):
        v = self.values()
        self.note.setText(self.problem() or
                          f"game/ and content/dota_addons/{v['folder']}, map {v['map_name']}")

    def check(self):
        p = self.problem()
        if p:
            QtWidgets.QMessageBox.warning(self, "New addon", p)
            return
        self.accept()


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        s = app.tools.settings
        self.setWindowTitle("Settings")
        f = QtWidgets.QFormLayout(self)
        r, self.e_steam = path_row(s["steam"])
        f.addRow("Steam", r)
        r, self.e_native = path_row(s.get("native", ""))
        f.addRow("Dota 2 (empty: found)", r)
        self.protons = {p['name']: p['path'] for p in proton.installed(s['steam'])}
        self.c_proton = QtWidgets.QComboBox()
        self.c_proton.setEditable(True)
        self.c_proton.addItems(list(self.protons))
        self.c_proton.setCurrentText(next((k for k, v in self.protons.items() if v == s["proton"]), s["proton"]))
        f.addRow("Proton", self.c_proton)
        self.e_user = QtWidgets.QLineEdit(s.get("username", ""))
        f.addRow("Steam account", self.e_user)
        self.container = QtWidgets.QCheckBox("Run Proton in the Steam Linux Runtime (as Steam does)")
        self.container.setChecked(s.get("container", True))
        f.addRow("", self.container)
        self.auto = QtWidgets.QCheckBox("Update the tools when Dota updates")
        self.auto.setChecked(s.get("auto_update", True))
        f.addRow("", self.auto)
        from . import terminal
        self.c_term = QtWidgets.QComboBox()
        self.c_term.setEditable(True)
        self.c_term.addItem(f"automatic ({terminal.detect() or 'none found'})", "")
        for name in terminal.installed():
            self.c_term.addItem(name, name)
        cur = s.get("terminal", "")
        i = self.c_term.findData(cur)
        if i >= 0:
            self.c_term.setCurrentIndex(i)
        else:
            self.c_term.setCurrentText(cur)
        self.c_term.setToolTip("The terminal for the game's console: a program name, or a command with {} "
                               "where the console goes")
        f.addRow("Terminal", self.c_term)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def term_value(self):
        i = self.c_term.currentIndex()
        if i >= 0 and self.c_term.itemText(i) == self.c_term.currentText():
            return self.c_term.itemData(i)
        return self.c_term.currentText().strip()

    def save(self):
        s = self.app.tools.settings
        s.update(steam=self.e_steam.text(), native=self.e_native.text(), username=self.e_user.text(),
                 proton=self.protons.get(self.c_proton.currentText(), self.c_proton.currentText()),
                 container=self.container.isChecked(), auto_update=self.auto.isChecked(),
                 terminal=self.term_value())
        self.app.tools.save_settings(s)
        self.accept()
        self.app.refresh()


def main(tools):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("dota2-tools")
    app.setDesktopFileName("dota2-workshop-tools")
    win = Launcher(tools) if tools.installed else Installer(tools)
    win.show()
    return app.exec()
