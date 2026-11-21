# AltGrid

Multi-instance Roblox account manager for Windows. One taskbar app that stores
multiple accounts, launches them side-by-side in a tiled grid, and keeps them
alive on less RAM/CPU than stock clients.

## Features

- **Multi-account storage** — cookies DPAPI-encrypted per Windows user, browser
  sign-in (Chrome/Edge) or paste a `.ROBLOSECURITY` cookie
- **Multi-instance** — runs several `RobloxPlayerBeta.exe` clients at once via
  named-mutex handling; per-account launch or launch-all into the same
  place/server (Place ID + Job ID, persisted)
- **Tiled grid** — 17-slot layout: one big center window + mini live DWM
  thumbnails around it (bypasses Roblox's minimum window size). Click a mini to
  promote it to center. Game windows park off-screen and stay composited
- **Keep-alive** — per-account or per-external-client. Real scancode input via
  focus-flicker + `SendInput` (Roblox ignores posted messages). Selectable keys
  (camera Turn, W/A/S/D, Jump, F13-none), optional return-move, skips nudges
  while you're typing, restores your foreground window
- **Low RAM** — safe FFlags + working-set trims every 30s
- **CPU throttle** — non-center tiled clients get suspend/resume duty-cycling
  (~20fps equivalent) + EcoQoS efficiency mode + Below-Normal priority,
  auto-restored when promoted
- **Auto-update** — checks GitHub Releases on startup, one-click install

## Download

Grab `AltGrid.exe` from [Releases](../../releases/latest). Windows 10/11, no
install — put it in a folder and run it. `data.json` and avatars are stored
next to the exe.

## Build from source

```
pip install -r requirements.txt
python main.py
```

or build the exe yourself:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name AltGrid --collect-all customtkinter main.py
```

## Notes

- Keep-alive briefly focuses each client (~300ms) every few minutes — real input
  is the only way Roblox's idle timer resets on background windows.
- External Roblox clients (not launched via the manager) appear in the Clients
  tab with the same Active/Trim controls.
