"""A terminal window running a command (the VConsole client): the one the
settings name, else the desktop's own, else the first one installed.

The setting is a terminal's program name (konsole, kitty, ...), or a whole
command line with {} where the command goes ("myterm --run {}").
"""
import os
import shlex
import shutil

# How each terminal is told to run a command after its options.
TERMINALS = {
    "konsole": ["konsole", "-e"],
    "gnome-terminal": ["gnome-terminal", "--"],
    "ptyxis": ["ptyxis", "--"],
    "kgx": ["kgx", "--"],
    "xfce4-terminal": ["xfce4-terminal", "-x"],
    "mate-terminal": ["mate-terminal", "-x"],
    "lxterminal": ["lxterminal", "-e"],
    "kitty": ["kitty"],
    "alacritty": ["alacritty", "-e"],
    "wezterm": ["wezterm", "start", "--"],
    "foot": ["foot"],
    "ghostty": ["ghostty", "-e"],
    "terminator": ["terminator", "-x"],
    "tilix": ["tilix", "-e"],
    "st": ["st", "-e"],
    "xterm": ["xterm", "-e"],
    "x-terminal-emulator": ["x-terminal-emulator", "-e"],
}
# The desktop's own first.
DESKTOP_TERMINALS = {"KDE": ["konsole"], "GNOME": ["ptyxis", "kgx", "gnome-terminal"],
                     "XFCE": ["xfce4-terminal"], "MATE": ["mate-terminal"], "LXQt": ["qterminal"],
                     "X-Cinnamon": ["gnome-terminal"]}


def installed():
    """The known terminals found on PATH."""
    return [name for name in TERMINALS if shutil.which(name)]


def detect():
    """The terminal to use when none is set: $TERMINAL, the desktop's own,
    else the first installed."""
    env = os.environ.get("TERMINAL", "")
    if env and shutil.which(shlex.split(env)[0]):
        return env
    for desktop in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":"):
        for name in DESKTOP_TERMINALS.get(desktop, []):
            if shutil.which(name):
                return name
    have = installed()
    return have[0] if have else ""


def command(setting, argv):
    """The command line opening a terminal that runs `argv`."""
    setting = (setting or "").strip() or detect()
    if not setting:
        raise SystemExit("no terminal found: name one in the settings")
    if "{}" in setting:
        head, _, tail = setting.partition("{}")
        return shlex.split(head) + list(argv) + shlex.split(tail)
    parts = shlex.split(setting)
    if len(parts) == 1 and parts[0] in TERMINALS:
        return TERMINALS[parts[0]] + list(argv)
    # An unknown terminal: most take -e.
    return parts + (["-e"] if len(parts) == 1 else []) + list(argv)
