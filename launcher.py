"""Multi-instance control and Roblox client launching.

Multi-instance works by pre-creating the named synchronization objects
(ROBLOX_singletonMutex / ROBLOX_singletonEvent) that RobloxPlayerBeta.exe
uses for its single-instance check. When they already exist, every launched
client runs as an independent instance instead of handing off to the first.

Launching uses the same roblox-player: protocol string the website produces:
a one-time auth ticket (minted per launch) plus a PlaceLauncher URL that
decides where the account lands. The account's cookie never appears on the
command line.
"""

import ctypes
import json
import os
import random
import time
import urllib.parse
from ctypes import wintypes

import api

kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
user32 = ctypes.windll.user32
ntdll = ctypes.windll.ntdll

WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102

_mutex: int = 0
_handles: list[int] = []

PLACE_LAUNCHER = "https://assetgame.roblox.com/game/PlaceLauncher.ashx"


def enable_multi_instance() -> str:
    """Claim Roblox's singleton objects.

    Mirrors what working tools do: *own* ROBLOX_singletonMutex (not just hold
    a handle), hold ROBLOX_singletonEvent open, and pre-create a mutex named
    ROBLOX_SingletonEvent so newer clients can't create that event either.

    Returns 'ok' (we own the mutex), 'busy' (a Roblox client owns it already —
    clients were running before we claimed, so new launches would merge), or
    'fail'.
    """
    global _mutex
    if _mutex:
        return "ok"

    mutex = kernel32.CreateMutexW(None, False, "ROBLOX_singletonMutex")
    if not mutex:
        return "fail"

    # Try to take ownership. If a Roblox client already holds it this times
    # out — that's the "must enable before first launch" case.
    res = kernel32.WaitForSingleObject(mutex, 0)
    if res == WAIT_TIMEOUT:
        kernel32.CloseHandle(mutex)
        return "busy"
    _mutex = mutex

    _handles.append(kernel32.CreateEventW(None, True, False, "ROBLOX_singletonEvent"))
    _handles.append(kernel32.CreateMutexW(None, True, "ROBLOX_SingletonEvent"))
    return "ok"


def disable_multi_instance() -> None:
    global _mutex
    for h in _handles:
        if h:
            kernel32.CloseHandle(h)
    _handles.clear()
    if _mutex:
        kernel32.ReleaseMutex(_mutex)
        kernel32.CloseHandle(_mutex)
        _mutex = 0


def multi_instance_enabled() -> bool:
    return bool(_mutex)


def running_roblox_count() -> int:
    """Count running RobloxPlayerBeta.exe processes.

    If any are alive when multi-instance is enabled, the first one has already
    claimed the singleton and new launches will merge into it.
    """
    import subprocess

    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe", "/NH"],
            text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.CalledProcessError):
        return 0
    return sum(1 for line in out.splitlines() if "RobloxPlayerBeta.exe" in line)


def kill_all_roblox() -> int:
    """Force-close all Roblox client processes (incl. tray/background)."""
    import subprocess

    n = running_roblox_count()
    for img in ("RobloxPlayerBeta.exe", "RobloxCrashHandler.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", img],
            capture_output=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    return n


def roblox_clients() -> list[dict]:
    """Running clients as [{pid, btid, mem_kb}]. btid from the -b cmdline arg."""
    import subprocess

    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='RobloxPlayerBeta.exe'\" "
                "| ForEach-Object { $b=([regex]'-b (\\d+)').Match($_.CommandLine); "
                "\"$($_.ProcessId)|$([math]::Round($_.WorkingSetSize/1KB))|$($b.Groups[1].Value)\" }",
            ],
            text=True,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    clients = []
    for line in out.splitlines():
        pid_s, _, rest = line.partition("|")
        mem_s, _, btid = rest.partition("|")
        if pid_s.strip().isdigit():
            clients.append(
                {
                    "pid": int(pid_s),
                    "mem_kb": int(float(mem_s or 0)),
                    "btid": btid.strip(),
                }
            )
    return clients


def find_window_for_pid(pid: int) -> int:
    """The real (titled, largest) visible top-level window for this process."""
    return windows_for_pids({pid}).get(pid, 0)


AFK_KEYS = {                     # key name -> vk (sent as scancodes)
    "TURN": 0x25,                # Left arrow, then Right — camera returns
    "NONE": 0x7C,                # F13 — a real keypress no game binds
    "W": 0x57,
    "A": 0x41,
    "S": 0x53,
    "D": 0x44,
    "SPACE": 0x20,
}
_AFK_REVERSE = {"W": "S", "A": "D", "S": "W", "D": "A"}
VK_RIGHT = 0x27

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_LMENU = 0xA4


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_U(ctypes.Union):
    # MOUSEINPUT is the largest member — without it sizeof(INPUT) is wrong
    # and SendInput rejects every call (ERROR_INVALID_PARAMETER).
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", wintypes.DWORD), ("_u", _INPUT_U)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def last_input_ms() -> int:
    """Milliseconds since the last real user input on this machine."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        return (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return 0


def _send_vk(vk: int, up: bool = False) -> None:
    i = _INPUT()
    i.type = INPUT_KEYBOARD
    i.ki.wVk = vk
    i.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i))


def _send_scan(vk: int, down: bool) -> None:
    """Scancode SendInput — what raw-input games actually listen for."""
    i = _INPUT()
    i.type = INPUT_KEYBOARD
    i.ki.wVk = 0
    i.ki.wScan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE
    if 0x21 <= vk <= 0x2E:  # arrows/pad keys are extended (E0 prefix)
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    i.ki.dwFlags = flags
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i))


def _scan_tap(vk: int) -> None:
    _send_scan(vk, True)
    time.sleep(0.06)
    _send_scan(vk, False)


def _take_foreground(hwnd: int) -> bool:
    """Grab foreground — Alt tap unlocks the foreground lock, then verify."""
    _send_vk(VK_LMENU)
    _send_vk(VK_LMENU, True)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.12)
    if user32.GetForegroundWindow() != hwnd:
        cur = kernel32.GetCurrentThreadId()
        tgt = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(cur, tgt, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(cur, tgt, False)
        time.sleep(0.12)
    return user32.GetForegroundWindow() == hwnd


def inject_activity(hwnd: int, key: str = "TURN", go_back: bool = False) -> bool:
    """Reset Roblox's idle timer: briefly take foreground, inject scancode
    input via SendInput, restore previous focus. Roblox only counts real
    input to the focused window — posted window messages don't register."""
    if not hwnd:
        return False
    if key not in AFK_KEYS:
        key = "TURN"
    fg = user32.GetForegroundWindow()
    was_iconic = is_iconic(hwnd)
    if hwnd != fg:
        if was_iconic:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.18)
        if not _take_foreground(hwnd):
            return False  # never inject keys into the wrong window
        time.sleep(0.1)
    _scan_tap(AFK_KEYS[key])
    rev = _AFK_REVERSE.get(key)
    if key == "TURN":
        time.sleep(0.1)
        _scan_tap(VK_RIGHT)
    elif go_back and rev:
        time.sleep(0.25)
        _scan_tap(AFK_KEYS[rev])
    time.sleep(0.06)
    if hwnd != fg:
        if fg and user32.IsWindow(fg):
            _take_foreground(fg)  # hand focus back
        if was_iconic:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    return True


def foreground_hwnd() -> int:
    return ctypes.windll.user32.GetForegroundWindow()


def window_rect(hwnd: int):
    """(x, y, w, h) of the window, or None if the handle is invalid."""
    from ctypes import wintypes

    r = wintypes.RECT()
    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return None


def is_iconic(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def windows_for_pids(pids) -> dict[int, int]:
    """{pid: hwnd} for visible top-level windows — prefers the real game
    window (titled + largest rect) over helper windows Roblox also creates."""
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    want = set(pids)
    cands: dict[int, list] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value in want and user32.IsWindowVisible(hwnd):
            cands.setdefault(p.value, []).append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    out = {}
    for pid, hs in cands.items():
        def score(h):
            r = window_rect(h)
            area = (r[2] * r[3]) if r else 0
            return (1 if user32.GetWindowTextLengthW(h) else 0, area)
        out[pid] = max(hs, key=score)
    return out


def set_foreground(hwnd: int) -> bool:
    """Best-effort SetForegroundWindow (AttachThreadInput trick for the
    foreground-lock restriction)."""
    if not hwnd:
        return False
    if is_iconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    src_tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(cur, fg_tid, True)
    user32.AttachThreadInput(cur, src_tid, True)
    ok = user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur, fg_tid, False)
    user32.AttachThreadInput(cur, src_tid, False)
    return bool(ok)


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000


def set_tool_window(hwnd: int, tool: bool = True) -> bool:
    """Add/remove WS_EX_TOOLWINDOW — controls the taskbar + Alt-Tab entry."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if tool:
        new = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    else:
        new = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
    if new == ex:
        return False
    vis = user32.IsWindowVisible(hwnd)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
    if vis:
        # the taskbar only re-evaluates the button after a hide/show
        user32.ShowWindow(hwnd, 0)  # SW_HIDE
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
    else:
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    return True


def raise_above_others(hwnd: int) -> None:
    """Z-raise above normal windows without stealing focus."""
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOP|NOMOVE|NOSIZE|NOACTIVATE


def show_minimized(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


def show_restored(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE


def move_window(hwnd: int, x: int, y: int, w: int, h: int) -> bool:
    """Move/resize a window without stealing focus or changing z-order."""
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    return bool(user32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE))


def work_area() -> tuple[int, int, int, int]:
    """Usable screen area minus the taskbar: (x, y, w, h)."""
    from ctypes import wintypes

    r = wintypes.RECT()
    SPI_GETWORKAREA = 0x30
    if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return 0, 0, ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)


def tile_layout(x0: int, y0: int, w: int, h: int) -> list[tuple[int, int, int, int]]:
    """17 slots as (x, y, w, h) rects — index 0 is the big center window.

    Grid: left/right mini columns are 18% of screen width; 5 equal rows.
    Corners share the columns' top/bottom cells; top/bottom rows fill between.
    """
    mw = int(w * 0.18)
    rh = h // 5
    iw = (w - 2 * mw) // 3
    inner_w = w - 2 * mw
    slots = [(x0 + mw, y0 + rh, inner_w, h - 2 * rh)]                      # center
    slots += [(x0, y0 + k * rh, mw, rh) for k in (1, 2, 3)]                # left L1-3
    slots += [(x0 + w - mw, y0 + k * rh, mw, rh) for k in (1, 2, 3)]       # right R1-3
    slots += [(x0 + mw + k * iw, y0, iw if k < 2 else inner_w - 2 * iw, rh) for k in range(3)]            # top T1-3
    slots += [(x0 + mw + k * iw, y0 + 4 * rh, iw if k < 2 else inner_w - 2 * iw, rh) for k in range(3)]   # bottom B1-3
    slots += [(x0, y0, mw, rh), (x0 + w - mw, y0, mw, rh),                 # corners TL TR
             (x0, y0 + 4 * rh, mw, rh), (x0 + w - mw, y0 + 4 * rh, mw, rh)]  # BL BR
    return slots


# ---- DWM thumbnails ---------------------------------------------------------
# Roblox enforces a minimum window size (WM_GETMINMAXINFO), so minis can't be
# real windows. Instead the real client is parked off-screen and we render a
# live scaled copy into our own window via DwmRegisterThumbnail.

DWM_TNP_RECTDESTINATION = 0x1
DWM_TNP_RECTSOURCE = 0x2
DWM_TNP_OPACITY = 0x4
DWM_TNP_VISIBLE = 0x8


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


def top_hwnd(hwnd: int) -> int:
    """Frame/top-level hwnd for a window (winfo_id gives a child)."""
    return user32.GetAncestor(hwnd, 2)  # GA_ROOT


def register_thumbnail(dest_hwnd: int, src_hwnd: int):
    tid = wintypes.HANDLE()
    hr = dwmapi.DwmRegisterThumbnail(top_hwnd(dest_hwnd), top_hwnd(src_hwnd), ctypes.byref(tid))
    return tid.value if hr == 0 else None


def update_thumbnail(tid, dest: tuple, src: tuple) -> None:
    """dest/src are (left, top, right, bottom). dest is in OUR window's client
    coords; src is in the game window's coords (full window incl. chrome)."""
    p = DWM_THUMBNAIL_PROPERTIES()
    p.dwFlags = DWM_TNP_RECTDESTINATION | DWM_TNP_RECTSOURCE | DWM_TNP_OPACITY | DWM_TNP_VISIBLE
    p.rcDestination = wintypes.RECT(*dest)
    p.rcSource = wintypes.RECT(*src)
    p.opacity = 255
    p.fVisible = True
    dwmapi.DwmUpdateThumbnailProperties(tid, ctypes.byref(p))


def unregister_thumbnail(tid) -> None:
    if tid:
        dwmapi.DwmUnregisterThumbnail(tid)


def window_size(hwnd: int) -> tuple[int, int]:
    r = window_rect(hwnd)
    return (r[2], r[3]) if r else (0, 0)


def _roblox_pids() -> list[int]:
    import subprocess

    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe", "/NH", "/FO", "CSV"],
            text=True,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) > 1 and "RobloxPlayerBeta" in parts[0]:
            pid_s = parts[1].strip().strip('"')
            if pid_s.isdigit():
                pids.append(int(pid_s))
    return pids


def trim_working_set(skip_pids: set[int] | None = None) -> int:
    """EmptyWorkingSet on each Roblox client — the 'RAM cleaner' trick.

    Forces each background client's inactive pages out of the working set.
    The foreground client is skipped so the active game doesn't hitch, plus
    any pids in skip_pids (accounts with Trim toggled off)."""
    pids = _roblox_pids()
    if not pids:
        return 0

    skip = set(skip_pids or ())
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        skip.add(pid.value)
    except Exception:
        pass

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400
    psapi = ctypes.windll.psapi
    trimmed = 0
    for pid in pids:
        if pid in skip:
            continue
        h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
        if h:
            psapi.EmptyWorkingSet(h)
            kernel32.CloseHandle(h)
            trimmed += 1
    return trimmed


def find_player_exe() -> str | None:
    """Locate the newest installed RobloxPlayerBeta.exe."""
    roots = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "Versions"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Roblox", "Versions"),
    ]
    best = None
    best_mtime = 0.0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            exe = os.path.join(root, name, "RobloxPlayerBeta.exe")
            if os.path.isfile(exe):
                mtime = os.path.getmtime(exe)
                if mtime > best_mtime:
                    best, best_mtime = exe, mtime
    return best


def extract_place_id(text: str) -> str | None:
    """Accept a raw place id or a roblox.com/games/<id>/... URL."""
    text = text.strip()
    if text.isdigit():
        return text
    import re

    m = re.search(r"/games/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"placeId=(\d+)", text)
    return m.group(1) if m else None


def place_launcher_url(place_id: str, job_id: str | None = None, btid: str | None = None) -> str:
    """PlaceLauncher.ashx URL — same parameter layout RAM uses."""
    req = f"RequestGame{'Job' if job_id else ''}"
    url = f"{PLACE_LAUNCHER}?request={req}"
    if btid:
        url += f"&browserTrackerId={btid}"
    url += f"&placeId={place_id}"
    if job_id:
        url += f"&gameId={job_id}"
    return url + "&isPlayTogetherGame=false"


def build_protocol_url(
    ticket: str, place_id: str | None = None, job_id: str | None = None, btid: str = ""
) -> str:
    """Assemble the roblox-player: launch string the website would produce."""
    fields = [
        "roblox-player:1",
        f"launchmode:{'play' if place_id else 'app'}",
        f"gameinfo:{urllib.parse.quote(ticket, safe='')}",
        f"launchtime:{int(time.time() * 1000)}",
    ]
    if place_id:
        pl = urllib.parse.quote(place_launcher_url(place_id, job_id, btid), safe="")
        fields.append(f"placelauncherurl:{pl}")
    fields += [
        f"browsertrackerid:{btid or random.randint(10**11, 10**12 - 1)}",
        "robloxLocale:en_us",
        "gameLocale:en_us",
        "channel:",
        "LaunchExp:InApp",
    ]
    return "+".join(fields)


# ---- Low-RAM mode -----------------------------------------------------------
# Same mechanism "RAM reducer" apps use: FFlags via ClientAppSettings.json,
# which Roblox reads at client startup. Biggest real win is forcing the lowest
# render quality; telemetry-off flags cut background memory + CPU churn.

LOW_RAM_FFLAGS = {
    "DFIntDebugFRMQualityLevelOverride": "1",
    "FFlagDebugDisableTelemetryEphemeralCounter": "True",
    "FFlagDebugDisableTelemetryEphemeralStat": "True",
    "FFlagDebugDisableTelemetryPoint": "True",
    "FFlagDebugDisableTelemetryV2Counter": "True",
    "FFlagDebugDisableTelemetryV2Event": "True",
    "FFlagDebugDisableTelemetryV2Stat": "True",
    "FFlagDebugSkyGray": "True",
}

_MARKER = ".ram-managed"


def apply_low_ram_flags(enabled: bool) -> bool:
    """Write/remove our FFlags in the version dir's ClientSettings.

    Merges with any existing flags so we don't clobber the user's own config.
    Re-applied before launches since Roblox updates can wipe the folder.
    """
    exe = find_player_exe()
    if not exe:
        return False
    csdir = os.path.join(os.path.dirname(exe), "ClientSettings")
    path = os.path.join(csdir, "ClientAppSettings.json")
    marker = os.path.join(csdir, _MARKER)
    try:
        if enabled:
            os.makedirs(csdir, exist_ok=True)
            flags = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        flags = json.load(f)
                except (json.JSONDecodeError, OSError):
                    flags = {}
            flags.update(LOW_RAM_FFLAGS)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flags, f, indent=4)
            with open(marker, "w") as f:
                f.write("1")
        elif os.path.exists(marker):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        flags = json.load(f)
                    for k in LOW_RAM_FFLAGS:
                        flags.pop(k, None)
                    if flags:
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(flags, f, indent=4)
                    else:
                        os.remove(path)
                except (json.JSONDecodeError, OSError):
                    pass
            os.remove(marker)
    except OSError:
        return False
    return True


def launch_account(
    cookie: str,
    place_id: str | None = None,
    job_id: str | None = None,
    btid: str = "",
    join_mode: str = "app",
) -> None:
    """Mint a ticket and launch a Roblox client logged into this account."""
    ticket = api.get_auth_ticket(cookie)
    exe = find_player_exe()
    if exe:
        _launch_exe(exe, ticket, place_id, job_id, btid, join_mode)
        return
    os.startfile(build_protocol_url(ticket, place_id, job_id, btid))  # type: ignore[attr-defined]


def _launch_exe(
    exe: str,
    ticket: str,
    place_id: str | None,
    job_id: str | None,
    btid: str,
    join_mode: str,
) -> None:
    """Launch RobloxPlayerBeta.exe with explicit args.

    RAM's proven format uses --app -t ticket -j url. --play skips the app-shell
    UI entirely (lighter per client) — selectable via join_mode."""
    import subprocess

    if place_id:
        args = [
            exe, f"--{join_mode}", "-t", ticket,
            "-j", place_launcher_url(place_id, job_id, btid or None),
            "-b", btid,
        ]
    else:
        args = [exe, "--app", "-t", ticket]
    subprocess.Popen(args)


# ---- per-client CPU throttling ----------------------------------------------
# Suspend/resume duty-cycling is the practical runtime "FPS cap" for already-
# running clients: while suspended the process can't render or simulate.
# EcoQoS (Task Manager's "Efficiency mode") + Below-Normal priority stack on
# top. All are handle-based and fully reversible.

PROCESS_SET_INFORMATION = 0x0200
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
POWER_THROTTLING_EXECUTION_SPEED = 1
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("ControlMask", wintypes.ULONG),
        ("StateMask", wintypes.ULONG),
    ]


def open_throttle_handle(pid: int):
    return kernel32.OpenProcess(
        PROCESS_SET_INFORMATION | PROCESS_SUSPEND_RESUME
        | PROCESS_QUERY_LIMITED_INFORMATION,
        False, pid,
    )


def suspend_process(hproc) -> None:
    ntdll.NtSuspendProcess(hproc)


def resume_process(hproc) -> None:
    ntdll.NtResumeProcess(hproc)


def set_efficiency(hproc, on: bool) -> None:
    st = PROCESS_POWER_THROTTLING_STATE()
    st.Version = 1
    st.ControlMask = POWER_THROTTLING_EXECUTION_SPEED
    st.StateMask = POWER_THROTTLING_EXECUTION_SPEED if on else 0
    kernel32.SetProcessInformation(hproc, 4, ctypes.byref(st), ctypes.sizeof(st))


def set_low_priority(hproc, low: bool) -> None:
    kernel32.SetPriorityClass(
        hproc, BELOW_NORMAL_PRIORITY_CLASS if low else NORMAL_PRIORITY_CLASS
    )


def kill_pid(pid: int) -> bool:
    r = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True, creationflags=0x08000000,
    )
    return r.returncode == 0


def kill_by_btid(btid: str) -> int:
    """Kill running Roblox clients whose command line carries this tracker id —
    prevents 'same account launched' kicks when relaunching an account."""
    import subprocess

    if not btid:
        return 0
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='RobloxPlayerBeta.exe'\" "
                "| ForEach-Object { \"$($_.ProcessId):$($_.CommandLine)\" }",
            ],
            text=True,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0
    killed = 0
    for line in out.splitlines():
        pid_s, _, cmdline = line.partition(":")
        if f"-b {btid}" in cmdline and pid_s.strip().isdigit():
            subprocess.run(
                ["taskkill", "/F", "/PID", pid_s.strip()],
                capture_output=True,
                creationflags=0x08000000,
            )
            killed += 1
    return killed
