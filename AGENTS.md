# Roblox Account Manager

Python + CustomTkinter multi-instance Roblox account manager.

## Run

```
python main.py
```

## Gotchas

- `SSLKEYLOGFILE` env var on this machine points to an unwritable path and
  breaks pip/requests TLS setup. Prefix commands with `SSLKEYLOGFILE=""`.
- `.ROBLOSECURITY` cookies are stored DPAPI-encrypted in `data.json`
  (per-Windows-user). Avatars cache to `avatars/`.

## How it works

- `launcher.enable_multi_instance()` claims `ROBLOX_singletonMutex` +
  `ROBLOX_singletonEvent` before any client launches.
- Each launch mints a single-use auth ticket (`api.get_auth_ticket`) and opens
  a `roblox-player:` protocol URL — cookie never touches the command line.
- Place ID + optional Job ID (`RequestGameJob`) targets a specific server.

## Keep-active notes
- Roblox only counts real input (SendInput, scancodes) delivered to the focused window; posted WM_KEYDOWN messages are ignored.
- ctypes `INPUT` must include the MOUSEINPUT union member so `sizeof(INPUT) == 40` on x64; otherwise SendInput fails with error 87 and silently sends nothing.
