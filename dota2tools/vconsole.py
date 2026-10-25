"""VConsole: the running game's console over TCP 29000 (the game started with
-vconsole), to send commands and read what it prints.

One connection, kept open for the whole session: under Wine the game takes
only one, and a client that connects and disconnects again while it loads
brought it down ("Cannot recover from failed socket shutdown"). Commands sent
before the map is in can crash it too: wait_loaded() first.

Packets: a 4-byte type ("CMND" out, "PRNT" and others in), version
0x00D40000, a 16-bit length counting the 12-byte header, a 16-bit handle;
PRNT's text starts 28 bytes into its body.
"""
import importlib
import re
import socket
import struct
import threading
import time


class VConsole:
    def __init__(self, port=29000):
        self.port = port
        self.sock = None
        self.lines = []
        self.lock = threading.Lock()
        self.reader = None

    def connect(self, timeout=600, alive=lambda: True):
        """Connect, retrying until the game listens (or `alive()` turns false)."""
        end = time.time() + timeout
        while time.time() < end and alive():
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                self.sock.settimeout(None)
                self.reader = threading.Thread(target=self._read, daemon=True)
                self.reader.start()
                return True
            except OSError:
                time.sleep(3)
        return False

    def _read(self):
        buf = b""
        while True:
            try:
                d = self.sock.recv(1 << 16)
            except OSError:
                break
            if not d:
                break
            buf += d
            while len(buf) >= 12:
                kind = buf[:4]
                _, length, _ = struct.unpack(">IHH", buf[4:12])
                if length < 12 or len(buf) < length:
                    break
                body, buf = buf[12:length], buf[length:]
                if kind == b"PRNT" and len(body) > 28:
                    text = body[28:].split(b"\0")[0].decode("utf-8", "replace")
                    with self.lock:
                        self.lines.extend(text.splitlines() or [""])

    def send(self, command):
        body = command.encode() + b"\0"
        self.sock.sendall(b"CMND" + struct.pack(">IHH", 0x00D40000, 12 + len(body), 0) + body)

    def mark(self):
        """A position in the output, for wait_for(since=...)."""
        with self.lock:
            return len(self.lines)

    def wait_for(self, pattern, timeout=300, since=0, alive=lambda: True):
        """The first line from `since` on matching `pattern` (None on timeout
        or when `alive()` turns false)."""
        rx = re.compile(pattern)
        end = time.time() + timeout
        i = since
        while time.time() < end and alive():
            with self.lock:
                new = self.lines[i:]
                i = len(self.lines)
            for ln in new:
                if rx.search(ln):
                    return ln
            time.sleep(0.25)
        return None

    def wait_loaded(self, timeout=900, alive=lambda: True):
        """Until the local player is fully in the map."""
        return self.wait_for(r"signon state \w+ -> SIGNONSTATE_FULL", timeout, alive=alive)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def shell(port=29000):
    """An interactive console in the terminal: waits for the game to listen,
    prints what it prints, sends each line typed; when the game closes it
    waits for the next one. One connection at a time, kept while the game
    runs (see above)."""
    try:
        importlib.import_module("readline")     # line editing and history for input()
    except ImportError:
        pass
    state = {"vc": None, "up": False}
    stop = threading.Event()

    def keep():
        while not stop.is_set():
            print(f"-- waiting for the game's VConsole on port {port} (the tools started with VConsole on)", flush=True)
            vc = VConsole(port)
            if not vc.connect(timeout=float("inf"), alive=lambda: not stop.is_set()):
                return
            state.update(vc=vc, up=True)
            print("-- connected: type console commands; Ctrl+D quits", flush=True)
            shown = 0
            while not stop.is_set() and vc.reader.is_alive():
                with vc.lock:
                    new = vc.lines[shown:]
                    shown = len(vc.lines)
                for line in new:
                    print(line, flush=True)
                time.sleep(0.1)
            state["up"] = False
            vc.close()
            if not stop.is_set():
                print("-- the game closed", flush=True)
                time.sleep(3)

    threading.Thread(target=keep, daemon=True).start()
    try:
        while True:
            line = input()
            if not line.strip():
                continue
            if state["up"]:
                try:
                    state["vc"].send(line)
                except OSError:
                    print("-- not sent: the connection dropped", flush=True)
            else:
                print("-- not connected yet", flush=True)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        stop.set()
        if state["vc"]:
            state["vc"].close()
