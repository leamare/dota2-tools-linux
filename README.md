# Dota 2 Workshop Tools for Linux

Runs the Windows-only Dota 2 Workshop Tools through Proton next to a native Linux Dota 2, without a second copy of the game.

The main game is kept as a native build, Windows binaries required for Workshop Tools are downloaded separately and linked to the main game's resources.

Aside from being a launcher for the workshop tools, it has additional tools for debugging and working with the game, like map renderer, vconsole or builds manager.

Important notes:
- It still runs the game and the tools via Proton. They work fine and I haven't noticed any significant performance losses, though there are occasional glitches sometimes.
- It is using DepotDownloader to download the game's builds, and it will ask you to log into your Steam account. Your credentials are not used for anything else. You can still download the updates manually if you want.
- While the initial version of the launcher/updater scripts was made by me for my own convenience, this more polished version has a lot of parts, generated or restructured by LLM (especially the native GUI part).

## How it works

- **Downloaded:** only the Windows-specific depots, about 2 GB: Windows binaries and shaders (373302, 373303) and the Workshop Tools DLC (381450). [DepotDownloader](https://github.com/SteamRE/DepotDownloader) fetches them at the exact manifests of your native Dota's build, which Steam's own app cache names.
- **Linked:** the ~76 GB of shared content comes from the native install. Files of 1 MB and over are symlinks, smaller ones copies, so the tools never write into Steam's files. `game/dota_addons` and `content/dota_addons` are the native install's own, so both clients see the same addons.
- **Updated:** when Steam updates Dota, only the changed files are downloaded and everything is relinked.
- **Run:** with Proton, inside the Steam Linux Runtime, in a Wine prefix of the tools' own. Steam must be running.

The Workshop Tools DLC is free but needs the account that owns it. Either log in once (password and Steam Guard, or a QR code; the login is remembered), or let your running Steam client download it with a `download_depot` command pasted into its console.

## Install

### 1. Before you start

- **Steam**, the native Linux client, running and logged in.
- **Dota 2** installed in Steam and up to date. It's supposed to be a Native client.
- **The Workshop Tools DLC** on your account.
- **Proton Experimental**, installed in Steam (Library → Tools), or later from the launcher's Proton tab. Other versions: 11.0 can't create the tools' prefix, and 9.0 lacks a Steam API Dota needs.

### 2. Get the program

**AppImage:**

1. Download `Dota2-Workshop-Tools-x86_64.AppImage` from the Releases page.
2. Put it where it will stay, e.g. `~/Applications`. The menu entry it makes points there.
3. Make it executable (`chmod +x`) and run it.

It brings its own Python, Qt and image libraries. Its windows use Qt's Fusion style rather than your desktop's.

**From source:**

```
git clone <this repository> && cd dota-workshop-linux
./dota2-tools
```

Install the dependencies below first. Running from source uses your system's Qt, so the windows look native.

### 3. Run the installer

The first start opens the installer:

1. Check the folders. Steam's and Dota's are found by themselves; the tools go to `~/.local/share/dota2-workshop-tools`.
2. Pick the Proton.
3. Pick how to get the Workshop Tools DLC:
   - **Steam account:** your password and Steam Guard code are asked once, then remembered for later updates. They are not used for anything except the depot downloads outside of steam. You can skip login and do the updates manually instead.
   - **QR code:** approve the login in the Steam mobile app.
   - **No login here:** the installer gives you a `download_depot` command to paste into Steam's console (`steam://open/console`); repeat it after Dota updates the tools.
4. Press **Install**. About 2 GB is downloaded; the rest of the game is linked from your Dota install.

Afterwards the menu has **Dota 2 Workshop Tools**, which opens the launcher. To have it in Steam too, close Steam and press **Add to Steam** (Tools tab).

From a terminal instead: `./dota2-tools install --username NAME` (`--qr` for a QR code, `--client` for no login).

### Dependencies

| What | Needed for | Arch | Debian / Ubuntu | Fedora |
|---|---|---|---|---|
| Python 3.10+ | everything | `python` | `python3` | `python3` |
| PySide6 or PyQt6 | the windows | `pyside6` | `python3-pyside6.qtwidgets` | `python3-pyside6` |
| Pillow, numpy | map renderer | `python-pillow python-numpy` | `python3-pil python3-numpy` | `python3-pillow python3-numpy` |
| OpenCV | map renderer | `python-opencv` | `python3-opencv` | `python3-opencv` |
| source2viewer-cli | map renderer (reads compiled maps) | `source2viewer-cli` (AUR) | the CLI from [ValveResourceFormat's releases](https://github.com/ValveResourceFormat/ValveResourceFormat/releases), on `PATH` as `source2viewer-cli` | same |
| gamescope, xdotool | rendering without clicks | `gamescope xdotool` | `gamescope xdotool` | `gamescope xdotool` |
| notify-send | notifications (optional) | `libnotify` | `libnotify-bin` | `libnotify` |

The AppImage bundles Python, Qt, Pillow, numpy and OpenCV, so only the last three rows apply to it. Installing, updating and launching the tools need only Python and Qt; the commands (`dota2-tools --help`) need only Python.

## Use

The launcher's tabs:

- **Addons:** pick an addon (and a map) and start the tools on it. Options: VConsole; opening the game's console in a terminal; hiding the launcher to the tray while the tools run. **New addon…** copies Dota's `addon_template` (or another addon) under a new name, the way Dota's own launcher does.
- **Map renderer:** see below.
- **Proton:** the installed versions, which one is used, and installing more:
  - Steam's own builds and the Steam Linux Runtime are installed by Steam;
  - GE-Proton releases are downloaded from GitHub (checked against their sha512 sum).
- **Builds:** old builds of Dota 2 (570) and Source 1 Dota (316570); see below.
- **Tools:** add to Steam, relink the native files, open folders, the Steam console.

**Settings** has Steam's folder, the native Dota folder, the Proton build, the Steam account, the runtime switch, auto-update, and the terminal for the console.

## Commands

```
dota2-tools status | update [--if-needed] | link | add-to-steam
dota2-tools launch [--addon NAME] [--map MAP] [--cfg] [--vconsole-terminal] [-- game arguments]
dota2-tools new-addon FOLDER [--title NAME] [--map MAP] [--from ADDON]
dota2-tools proton [list | use NAME | install [NAME|APPID] | install-ge [TAG] | remove NAME | runtime]
dota2-tools vconsole [--port 29000] [--terminal]
dota2-tools builds [list | refresh | import FILE | download BUILD | commands BUILD | use BUILD | reset | delete BUILD]
dota2-tools render maps | prepare | auto | stitch | tiles ...
```

## The game's console

`dota2-tools vconsole` is a console client in the terminal. It waits until the game listens on VConsole's port, prints everything the game prints, sends each line you type as a command, and waits for the next game when this one closes.

This was tested with Dota 2 `-dev -vconsole` under Proton and Artifact (Linux Native and Windows Proton). I generally used it as a debugging tool.

## Old builds

The **Builds** tab lists builds of Dota 2 (app 570) and of Source 1 Dota (app 316570) in one table. It uses pre-saved SteamDB pages and [D2-LRG-Metadata](https://github.com/leamare/D2-LRG-Metadata) to create a list of builds and remarks with patches.

This allows downloading older versions of the client and working with them if needed.

Switching builds:

- **Make active** makes the launcher start that build instead of the tools folder. A Source 1 build starts its game (`dota.exe -console`).
- **Reset to latest** goes back to the tools folder: the latest build, Steam's, linked from your native install.

## Map renderer

Renders Orthogonal top-down projection of the map, running an automated SFM process on a virtual screen. It does the following steps:

1. **Prepare:** builds a render addon that mirrors the chosen addon, runs the pre-run scripts, and writes an SFM session with the camera grid. The scripts fix some textures and restore materials.
2. **Rendering:** runs automated SFM process.
3. **Stitch:** does ICE-like panorama stitching with tone correction.

You can run run each step individually or run a fully automated version of it.

The outputs:
- the full square picture
- a picture cut to the map's bounds (based on overview/minimap bounds)
- a smaller web copy of the cut version
- optional tiles for [dota-interactive-map](https://github.com/devilesk/dota-interactive-map).

**Pre-run scripts**

- `flies` hides ambient swarms that turn into blots from far away;
- `tools_materials` hides tools-only materials, such as the walkable-area grid under bridges;
- `edge_trees` gives the black silhouette trees beyond the map's edge their leaves;
- `corners` puts ground on the black fields in the corners, baked from the map's own edge materials;
- `lava` restores the Dire lava's glow.

Custom lighting (sun direction, shadow strength) renders a relit copy of the map built from its source. Scale 2 or 0.5 changes only the export resolution.

## Building the AppImage

```
pip install python-appimage
packaging/appimage/build.sh          # Dota2-Workshop-Tools-x86_64.AppImage in the current folder
```

