"""Roblox Account Manager — multi-instance client launcher.

Run: python main.py
"""

import io
import os
import sys
import tempfile
import threading
import time
import traceback

import customtkinter as ctk
from PIL import Image

import api
import browser_login
import launcher
import store
import updater
import version

# A shell can inject SSLKEYLOGFILE pointing at an unwritable/virtual path —
# Python then fails EVERY HTTPS call with EACCES. Drop it if its dir is gone.
_kl = os.environ.get("SSLKEYLOGFILE")
if _kl and not os.path.isdir(os.path.dirname(_kl.lstrip("\\?"))):
    os.environ.pop("SSLKEYLOGFILE")


def _log_exc(e: BaseException) -> None:
    try:
        with open(os.path.join(store.DATA_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write("".join(traceback.format_exception(e)))
    except OSError:
        pass


def _validate_cookie(cookie: str) -> dict | None:
    try:
        user = api.get_authenticated_user(cookie)
    except api.ApiError:
        return None
    return user if user.get("id") else None

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ACCENT = "#2fa84f"


def _avatar_image(path: str | None, size: int = 44) -> ctk.CTkImage:
    if path and os.path.exists(path):
        img = Image.open(path)
    else:
        img = Image.new("RGB", (size, size), "#3a3d45")
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


class AddDialog(ctk.CTkToplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self._login: browser_login.BrowserLogin | None = None
        self._cancelled = False
        self.title("Add Account")
        self.geometry("460x360")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.browser_btn = ctk.CTkButton(
            self,
            text="Sign in with Browser",
            fg_color=ACCENT,
            hover_color="#27853f",
            command=self._browser_login,
        )
        self.browser_btn.pack(padx=16, pady=(18, 4))
        ctk.CTkLabel(
            self,
            text="Opens a browser on the Roblox sign-in page.\nOnce you log in, the account is captured automatically.",
            text_color="#8a8f9a",
            font=ctk.CTkFont(size=11),
        ).pack(padx=16)

        ctk.CTkLabel(self, text="— or paste a .ROBLOSECURITY cookie —", text_color="#8a8f9a").pack(
            padx=16, pady=(14, 4)
        )
        self.box = ctk.CTkTextbox(self, height=70)
        self.box.pack(padx=16, fill="x")
        self.add_btn = ctk.CTkButton(
            self, text="Validate & Add", fg_color="#3a3d45", command=self._add
        )
        self.add_btn.pack(padx=16, pady=(8, 0), anchor="e")

        self.status = ctk.CTkLabel(self, text="", text_color="#9ab")
        self.status.pack(padx=16, pady=(6, 10), anchor="w")

    # ---------- shared ----------

    def _finish(self, user: dict, cookie: str):
        """Store a validated account. Runs off the UI thread."""
        try:
            name = user.get("name") or user.get("displayName") or str(user["id"])
            store.add_account(
                user["id"], name, user.get("displayName") or name, cookie
            )
            png = api.get_avatar_headshot(user["id"])
            if png:
                try:
                    with open(store.avatar_path(user["id"]), "wb") as f:
                        f.write(png)
                except OSError:
                    pass
        except Exception as e:
            _log_exc(e)
            self.after(0, lambda: self._fail(str(e) or repr(e)))
            return
        self.after(0, lambda: self._done(name))

    def _fail(self, msg: str):
        self._close_login()
        self.add_btn.configure(state="normal")
        self.browser_btn.configure(state="normal")
        self.status.configure(text=msg or "Unknown error", text_color="#e06c6c")

    def _done(self, username: str):
        self._close_login()
        self.app.set_status(f"Added account {username}")
        self.app.refresh_accounts()
        self.destroy()

    def _close_login(self):
        self._cancelled = True
        if self._login:
            login, self._login = self._login, None
            threading.Thread(target=login.close, daemon=True).start()

    def _on_close(self):
        self._close_login()
        self.destroy()

    # ---------- browser sign-in ----------

    def _browser_login(self):
        if self._login:
            return
        self._cancelled = False
        self.browser_btn.configure(state="disabled")
        self.status.configure(text="Opening browser...", text_color="#9ab")

        def work():
            login = browser_login.BrowserLogin()
            self._login = login
            try:
                login.start()
            except Exception as e:
                self.after(0, lambda: self._fail(f"Couldn't open browser: {e}"))
                return
            self.after(0, lambda: self.status.configure(
                text="Sign in to Roblox in the browser window..."))
            result = login.wait_for_login(_validate_cookie, cancelled=lambda: self._cancelled)
            if not result:
                self.after(0, self._browser_closed)
                return
            user, cookie = result
            self._finish(user, cookie)

        def guarded():
            try:
                work()
            except Exception as e:
                _log_exc(e)
                self.after(0, lambda: self._fail(str(e) or repr(e)))

        threading.Thread(target=guarded, daemon=True).start()

    def _browser_closed(self):
        self._close_login()
        self.browser_btn.configure(state="normal")
        self.status.configure(text="Browser closed before sign-in finished.", text_color="#e0a06c")

    # ---------- manual paste ----------

    def _add(self):
        raw = self.box.get("1.0", "end")
        cookie = api.normalize_cookie(raw)
        if not cookie:
            self.status.configure(text="Paste a cookie first.")
            return
        self.add_btn.configure(state="disabled")
        self.status.configure(text="Validating...", text_color="#9ab")

        def work():
            user = _validate_cookie(cookie)
            if not user:
                self.after(0, lambda: self._fail("Cookie is invalid or expired"))
                return
            self._finish(user, cookie)

        threading.Thread(target=work, daemon=True).start()


class AccountRow(ctk.CTkFrame):
    def __init__(self, master, app: "App", account: dict):
        super().__init__(
            master, fg_color="#26282e", corner_radius=8,
            border_width=0, border_color=ACCENT,
        )
        self.app = app
        self.account = account
        uid = account["user_id"]
        self.running_pid: int | None = None

        self.selected = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self, text="", variable=self.selected, width=24, checkbox_width=20, checkbox_height=20
        ).pack(side="left", padx=(10, 4), pady=10)

        img = app.get_avatar(uid)
        ctk.CTkLabel(self, image=img, text="").pack(side="left", padx=4, pady=6)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", padx=8, pady=6, fill="x", expand=True)
        self._real_name = account.get("display_name") or account["username"]
        self._real_user = account["username"]
        self.name_lbl = ctk.CTkLabel(info, text="", font=ctk.CTkFont(weight="bold"))
        self.name_lbl.pack(anchor="w")
        self.sub_lbl = ctk.CTkLabel(info, text="", text_color="#8a8f9a",
                                    font=ctk.CTkFont(size=11))
        self.sub_lbl.pack(anchor="w")
        self._paint_names()
        self._last_mem: float | None = None

        # per-client toggles — apply to this account's running client
        toggles = ctk.CTkFrame(self, fg_color="transparent")
        toggles.pack(side="right", padx=4, pady=6)
        self.trim_var = ctk.BooleanVar(value=not account.get("no_trim"))
        ctk.CTkCheckBox(
            toggles, text="Trim", variable=self.trim_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=lambda: store.set_account_flag(uid, "no_trim", not self.trim_var.get()),
        ).pack(anchor="w")
        self.keep_var = ctk.BooleanVar(value=bool(account.get("keep_active")))
        ctk.CTkCheckBox(
            toggles, text="Active", variable=self.keep_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=lambda: store.set_account_flag(uid, "keep_active", self.keep_var.get()),
        ).pack(anchor="w")
        self.key_menu = ctk.CTkOptionMenu(
            toggles, values=["Turn", "None", "W", "A", "S", "D", "Jump"], width=58, height=18,
            font=ctk.CTkFont(size=10), fg_color="#3a3d45", button_color="#4a4d55",
            command=lambda v: store.set_account_flag(
                uid, "afk_key", {"Jump": "SPACE", "None": "NONE", "Turn": "TURN"}.get(v, v)),
        )
        self.key_menu.set(
            {"SPACE": "Jump", "NONE": "None", "TURN": "Turn"}.get(
                account.get("afk_key", "TURN"), account.get("afk_key", "Turn"))
        )
        self.key_menu.pack(anchor="w", pady=(2, 0))
        self.back_var = ctk.BooleanVar(value=bool(account.get("afk_return")))
        ctk.CTkCheckBox(
            toggles, text="Return", variable=self.back_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=lambda: store.set_account_flag(uid, "afk_return", self.back_var.get()),
        ).pack(anchor="w")

        self.action_btn = ctk.CTkButton(
            self, text="✕", width=42, fg_color="#3a3d45", hover_color="#a33",
            command=lambda: app.remove_account(account),
        )
        self.action_btn.pack(side="right", padx=(4, 10), pady=10)
        ctk.CTkButton(
            self, text="Launch", width=80, fg_color=ACCENT, hover_color="#27853f",
            command=lambda: app.launch_accounts([account]),
        ).pack(side="right", padx=4, pady=10)

    def _shown_name(self) -> str:
        return "Alt" if self.app.hide_names else self._real_name

    def _shown_user(self) -> str:
        return "hidden" if self.app.hide_names else f"@{self._real_user}  •  {self.account['user_id']}"

    def _paint_names(self, running_mem: float | None = None):
        self.name_lbl.configure(text=self._shown_name())
        if running_mem is not None:
            self.sub_lbl.configure(
                text=f"{self._shown_user()}  •  ●{running_mem:.0f} MB",
                text_color=ACCENT,
            )
        else:
            self.sub_lbl.configure(text=self._shown_user(), text_color="#8a8f9a")

    def apply_name_mask(self):
        self._paint_names(self._last_mem)

    def set_running(self, info: dict | None, pid: int | None):
        self.running_pid = pid
        self._last_mem = info["mem_mb"] if info else None
        self._paint_names(self._last_mem)
        if info:
            self.configure(border_width=2)
            if self.action_btn.cget("text") != "Close":
                self.action_btn.configure(
                    text="Close",
                    command=lambda: launcher.kill_by_btid(self.account.get("btid") or ""),
                )
        else:
            self.configure(border_width=0)
            if self.action_btn.cget("text") != "✕":
                self.action_btn.configure(
                    text="✕", command=lambda: self.app.remove_account(self.account)
                )


class ExtClientRow(ctk.CTkFrame):
    """Row for a Roblox client NOT launched through the manager — external
    clients get the same Trim/Active treatment but aren't tied to an account."""

    def __init__(self, master, app: "App", client: dict):
        super().__init__(master, fg_color="#26282e", corner_radius=8)
        self.app = app
        self.pid = client["pid"]

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", padx=10, pady=8, fill="x", expand=True)
        ctk.CTkLabel(
            info, text="Roblox Client", font=ctk.CTkFont(weight="bold")
        ).pack(anchor="w")
        self.sub_lbl = ctk.CTkLabel(
            info, text=f"PID {self.pid}  •  external", text_color="#8a8f9a",
            font=ctk.CTkFont(size=11),
        )
        self.sub_lbl.pack(anchor="w")

        toggles = ctk.CTkFrame(self, fg_color="transparent")
        toggles.pack(side="right", padx=4, pady=6)
        self.trim_var = ctk.BooleanVar(value=self.pid not in app._ext_notrim)
        ctk.CTkCheckBox(
            toggles, text="Trim", variable=self.trim_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=self._toggle_trim,
        ).pack(anchor="w")
        self.act_var = ctk.BooleanVar(value=self.pid in app._ext_active)
        ctk.CTkCheckBox(
            toggles, text="Active", variable=self.act_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=self._toggle_active,
        ).pack(anchor="w")
        self.key_menu = ctk.CTkOptionMenu(
            toggles, values=["Turn", "None", "W", "A", "S", "D", "Jump"], width=58, height=18,
            font=ctk.CTkFont(size=10), fg_color="#3a3d45", button_color="#4a4d55",
            command=lambda v: app._ext_afk_key.__setitem__(
                self.pid, {"Jump": "SPACE", "None": "NONE", "Turn": "TURN"}.get(v, v)),
        )
        self.key_menu.set(
            {"SPACE": "Jump", "NONE": "None", "TURN": "Turn"}.get(
                app._ext_afk_key.get(self.pid, "TURN"),
                app._ext_afk_key.get(self.pid, "Turn"))
        )
        self.key_menu.pack(anchor="w", pady=(2, 0))
        self.back_var = ctk.BooleanVar(value=self.pid in app._ext_afk_back)
        ctk.CTkCheckBox(
            toggles, text="Return", variable=self.back_var, width=54,
            checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=11),
            command=self._toggle_back,
        ).pack(anchor="w")

        ctk.CTkButton(
            self, text="Close", width=60, fg_color="#3a3d45", hover_color="#a33",
            command=lambda: launcher.kill_pid(self.pid),
        ).pack(side="right", padx=(4, 10), pady=8)

    def _toggle_trim(self):
        if self.trim_var.get():
            self.app._ext_notrim.discard(self.pid)
        else:
            self.app._ext_notrim.add(self.pid)

    def _toggle_active(self):
        if self.act_var.get():
            self.app._ext_active.add(self.pid)
        else:
            self.app._ext_active.discard(self.pid)

    def _toggle_back(self):
        if self.back_var.get():
            self.app._ext_afk_back.add(self.pid)
        else:
            self.app._ext_afk_back.discard(self.pid)

    def set_mem(self, mb: float):
        self.sub_lbl.configure(
            text=f"PID {self.pid}  •  external  •  ●{mb:.0f} MB",
            text_color=ACCENT,
        )


class GameOverlay(ctk.CTkToplevel):
    """Slim bar docked just below a running client's window — follows it,
    hides when minimized, never covers the game."""

    H, GAP = 38, 4
    MIN_W, MAX_W = 330, 460

    def __init__(self, app: "App", account: dict, hwnd: int, pid: int):
        super().__init__(app)
        self.app = app
        self.account = account
        self.hwnd = hwnd
        self.pid = pid
        self.overrideredirect(True)
        self.attributes("-topmost", True, "-alpha", 0.97)
        self.configure(fg_color="#0d0f13")
        self.resizable(False, False)

        body = ctk.CTkFrame(
            self, fg_color="#0d0f13", corner_radius=8,
            border_width=1, border_color="#2e323b",
        )
        body.pack(fill="both", expand=True, padx=0, pady=0)

        ctk.CTkLabel(
            body, text="●", text_color=ACCENT, font=ctk.CTkFont(size=10), width=16
        ).pack(side="left", padx=(8, 0))
        self.name_lbl = ctk.CTkLabel(
            body,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e6e9ee",
        )
        self.name_lbl.pack(side="left", padx=(2, 8))
        self.mem_lbl = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(size=11), text_color="#9aa0ab"
        )
        self.mem_lbl.pack(side="left")

        self.act_btn = ctk.CTkButton(
            body, text="ACTIVE", width=86, height=24,
            font=ctk.CTkFont(size=11, weight="bold"), command=self._toggle_active,
        )
        self.act_btn.pack(side="right", padx=(4, 7), pady=6)
        self.trim_btn = ctk.CTkButton(
            body, text="TRIM", width=86, height=24,
            font=ctk.CTkFont(size=11, weight="bold"), command=self._toggle_trim,
        )
        self.trim_btn.pack(side="right", padx=4, pady=6)

        self._sync()
        self.tick()

    def _paint(self, btn, on: bool):
        btn.configure(
            fg_color=ACCENT if on else "#23262d",
            text_color="#0c0f0c" if on else "#c9ced8",
            hover_color="#27853f" if on else "#33363f",
            border_width=0 if on else 1,
            border_color="#3a3e48",
        )

    def _sync(self):
        self.name_lbl.configure(
            text="Alt" if self.app.hide_names
            else (self.account.get("display_name") or self.account["username"])
        )
        self._paint(self.trim_btn, not self.account.get("no_trim"))
        self._paint(self.act_btn, bool(self.account.get("keep_active")))
        row = self.app._row_for(self.account["user_id"])
        if row:
            row.trim_var.set(not self.account.get("no_trim"))
            row.keep_var.set(bool(self.account.get("keep_active")))

    def _toggle_trim(self):
        cur = not self.account.get("no_trim")
        self.account["no_trim"] = cur
        store.set_account_flag(self.account["user_id"], "no_trim", cur)
        self._sync()

    def _toggle_active(self):
        cur = not bool(self.account.get("keep_active"))
        self.account["keep_active"] = cur
        store.set_account_flag(self.account["user_id"], "keep_active", cur)
        self._sync()

    def set_mem(self, mb: float):
        self.mem_lbl.configure(text=f"{mb:.0f} MB")

    def tick(self):
        if not store.get_setting("overlay", True) or store.get_setting("tile", True):
            self.withdraw()
            return
        rect = launcher.window_rect(self.hwnd)
        if not rect or launcher.is_iconic(self.hwnd):
            self.withdraw()
            return
        x, y, w, h = rect
        bar_w = max(self.MIN_W, min(w, self.MAX_W))
        bar_x = x + (w - bar_w) // 2
        bar_y = y + h + self.GAP
        # flip above the window if the bar would run off the screen bottom
        if bar_y + self.H > self.winfo_screenheight():
            bar_y = y - self.H - self.GAP
        self.deiconify()
        self.geometry(f"{bar_w}x{self.H}+{bar_x}+{bar_y}")


class MiniTile(ctk.CTkToplevel):
    """Borderless topmost window hosting a live DWM thumbnail of a parked
    (off-screen) Roblox client. Click it to promote that client to center."""

    LABEL_H = 18

    def __init__(self, app: "App", rect):
        super().__init__(app)
        self.app = app
        self.uid = None
        self.tid = None
        self.src_hwnd = 0
        self.overrideredirect(True)
        try:
            self.wm_attributes("-toolwindow", True)  # hide from alt-tab
        except Exception:
            pass
        self.configure(fg_color="#0d0f13")
        x, y, w, h = rect
        self.geometry(f"{w}x{h}+{x}+{y}")

        bar = ctk.CTkFrame(self, fg_color="#0d0f13", height=self.LABEL_H, corner_radius=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.name_lbl = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=10), text_color="#c9ced8"
        )
        self.name_lbl.pack(side="left", padx=5)
        self.mem_lbl = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=10), text_color="#6d727c"
        )
        self.mem_lbl.pack(side="right", padx=5)

        for wdg in (self, bar, self.name_lbl, self.mem_lbl):
            wdg.bind("<Button-1>", lambda _e: self._click())

    def _click(self):
        if self.uid is not None:
            self.app._tile_promote(self.uid)

    def assign(self, uid: int, src_hwnd: int, name: str, rect):
        self.uid = uid
        x, y, w, h = rect
        if getattr(self, "_rect", None) != rect:
            self._rect = rect
            self.geometry(f"{w}x{h}+{x}+{y}")
        if src_hwnd != self.src_hwnd:
            launcher.unregister_thumbnail(self.tid)
            self.tid = None
            self.src_hwnd = src_hwnd
            if src_hwnd:
                self.tid = launcher.register_thumbnail(self.winfo_id(), src_hwnd)
        self.name_lbl.configure(text=name)
        self._update_thumb(rect)

    def _update_thumb(self, rect):
        if not self.tid or not self.src_hwnd:
            return
        _, _, w, h = rect
        sw, sh = launcher.window_size(self.src_hwnd)
        if sw and sh:
            launcher.update_thumbnail(self.tid, (0, self.LABEL_H, w, h), (0, 0, sw, sh))

    def set_mem(self, mb: float):
        self.mem_lbl.configure(text=f"{mb:.0f}M")

    def close(self):
        launcher.unregister_thumbnail(self.tid)
        self.tid = None
        self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    """One gear-menu for every settings toggle — keeps the main window slim."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self._syncing = False
        self.title("Settings")
        self.configure(fg_color="#14161a")
        self.resizable(False, False)
        self.transient(app)
        self.geometry("400x460")
        self.protocol("WM_DELETE_WINDOW", self._close)

        ctk.CTkLabel(
            self, text="Settings", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=16, pady=(14, 8))

        self.sw_multi = self._row(
            "Multi-instance",
            "Run several Roblox clients at once (claims the singleton objects)",
            launcher.multi_instance_enabled(),
            app._set_multi,
        )
        self.sw_lowram = self._row(
            "Low RAM",
            "Low-quality FFlags + trims background clients every 30s",
            bool(store.get_setting("low_ram", False)),
            app._set_lowram,
        )
        self.sw_tile = self._row(
            "Tiling",
            "Grid layout — click a mini preview to make it the main client",
            bool(store.get_setting("tile", True)),
            app._set_tile,
        )
        self.sw_ovl = self._row(
            "Overlay bars",
            "Per-client controls docked under each game window (when untiled)",
            bool(store.get_setting("overlay", True)),
            app._set_overlay,
        )
        self.sw_hide = self._row(
            "Hide names",
            "Mask usernames everywhere — streaming / privacy",
            app.hide_names,
            app._set_hide,
        )

        # join mode picker
        f = ctk.CTkFrame(self, fg_color="#20232a", corner_radius=8)
        f.pack(fill="x", padx=14, pady=4)
        t = ctk.CTkFrame(f, fg_color="transparent")
        t.pack(side="left", padx=10, pady=8, fill="x", expand=True)
        ctk.CTkLabel(t, text="Join mode", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            t, text="--app is the proven path; --play skips the app shell",
            font=ctk.CTkFont(size=11), text_color="#8a8f9a", wraplength=230, justify="left",
        ).pack(anchor="w")
        self.join_menu = ctk.CTkOptionMenu(
            f, values=["APP", "PLAY"], width=84, height=26,
            fg_color="#3a3d45", button_color="#4a4d55", font=ctk.CTkFont(size=11),
            command=lambda v: app._set_joinmode(v.lower()),
        )
        self.join_menu.set(store.get_setting("join_mode", "app").upper())
        self.join_menu.pack(side="right", padx=10)

        # version / update footer
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=16, pady=(8, 12))
        ctk.CTkLabel(
            foot, text=f"{version.APP_NAME} v{version.APP_VERSION}",
            font=ctk.CTkFont(size=11), text_color="#6a6f7a",
        ).pack(side="left")
        ctk.CTkButton(
            foot, text="Check for updates", width=130, height=26,
            fg_color="#3a3d45", hover_color="#4a4d55", font=ctk.CTkFont(size=11),
            command=lambda: threading.Thread(
                target=lambda: app._update_check(manual=True), daemon=True
            ).start(),
        ).pack(side="right")

    def _row(self, title: str, desc: str, on: bool, cmd) -> ctk.CTkSwitch:
        f = ctk.CTkFrame(self, fg_color="#20232a", corner_radius=8)
        f.pack(fill="x", padx=14, pady=4)
        t = ctk.CTkFrame(f, fg_color="transparent")
        t.pack(side="left", padx=10, pady=8, fill="x", expand=True)
        ctk.CTkLabel(t, text=title, font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            t, text=desc, font=ctk.CTkFont(size=11), text_color="#8a8f9a",
            wraplength=230, justify="left",
        ).pack(anchor="w")
        sw = ctk.CTkSwitch(f, text="", width=44, progress_color=ACCENT)
        sw.configure(command=lambda: self._syncing or cmd(bool(sw.get())))
        sw.pack(side="right", padx=10)
        return sw

    def _set(self, sw: ctk.CTkSwitch, on: bool):
        self._syncing = True
        try:
            (sw.select if on else sw.deselect)()
        finally:
            self._syncing = False

    def sync(self):
        a = self.app
        self._set(self.sw_multi, launcher.multi_instance_enabled())
        self._set(self.sw_lowram, bool(store.get_setting("low_ram", False)))
        self._set(self.sw_tile, bool(store.get_setting("tile", True)))
        self._set(self.sw_ovl, bool(store.get_setting("overlay", True)))
        self._set(self.sw_hide, a.hide_names)
        self.join_menu.set(store.get_setting("join_mode", "app").upper())

    def _close(self):
        self.app._settings = None
        self.destroy()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(version.APP_NAME)
        self.geometry("720x620")
        self.minsize(560, 480)
        self._avatars: dict[int, ctk.CTkImage] = {}
        self._rows: list[AccountRow] = []
        self._launching = False
        self._trim_stop = threading.Event()
        self._watch_stop = threading.Event()
        self._last_ping = time.time()
        self._running_pids: dict[int, int] = {}
        self._overlays: dict[int, GameOverlay] = {}
        self._tile_order: list[int] = []
        self._tile_rects: dict[int, tuple] = {}
        self._tiles: dict[int, MiniTile] = {}
        self._last_promote = 0.0
        self._tooled: set[int] = set()   # hwnds given WS_EX_TOOLWINDOW
        self._last_bring = 0.0
        self._ext_rows: dict[int, ExtClientRow] = {}
        self._ext_active: set[int] = set()   # external pids to keep alive
        self._ext_notrim: set[int] = set()   # external pids excluded from trims
        self._ext_afk_key: dict[int, str] = {}
        self._ext_afk_back: set[int] = set()
        self._throttled: dict[int, int] = {}   # pid -> process handle
        self._throttle_stop = threading.Event()
        self._afk_running = False
        self.hide_names = bool(store.get_setting("hide_names", False))
        self._settings: SettingsDialog | None = None

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            header, text=version.APP_NAME, font=ctk.CTkFont(size=20, weight="bold")
        ).pack(side="left")

        ctk.CTkButton(header, text="⚙ Settings", width=100, fg_color="#3a3d45",
                      hover_color="#4a4d55", command=self._open_settings).pack(
            side="right", padx=(4, 0)
        )
        ctk.CTkButton(header, text="+ Add Account", width=120, command=self._open_add).pack(
            side="right", padx=4
        )

        # Toolbar — actions only; settings live in the ⚙ dialog
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkButton(
            toolbar, text="Kill", width=60, fg_color="#3a3d45", hover_color="#a33",
            command=self._kill_clients,
        ).pack(side="left", padx=(0, 6))
        self.tile_btn = ctk.CTkButton(toolbar, text="Tile: ...", width=70, command=self._toggle_tile)
        self.tile_btn.pack(side="left", padx=6)
        ctk.CTkButton(
            toolbar, text="Raise All", width=80, fg_color="#3a3d45", hover_color="#4a4d55",
            command=self._restore_all,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            toolbar, text="Nudge Now", width=86, fg_color="#3a3d45", hover_color="#4a4d55",
            command=self._nudge_now,
        ).pack(side="left", padx=6)
        # Packed on the right only when a newer release is found
        self.update_btn = ctk.CTkButton(
            toolbar, text="Update", width=110, fg_color="#b8860b",
            hover_color="#d4a017", command=self._install_update,
        )

        # Tabs: Accounts (managed) | Clients (external Roblox processes)
        self.tabview = ctk.CTkTabview(
            self, fg_color="transparent", anchor="nw",
            segmented_button_fg_color="#1b1d22",
            segmented_button_unselected_color="#26282e",
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color="#27853f",
        )
        self.tabview.pack(fill="both", expand=True, padx=14, pady=6)
        acc_tab = self.tabview.add("Accounts")
        ext_tab = self.tabview.add("Clients")

        # Account list
        self.list_frame = ctk.CTkScrollableFrame(acc_tab, fg_color="#1b1d22")
        self.list_frame.pack(fill="both", expand=True)

        self.empty_label = ctk.CTkLabel(
            self.list_frame, text="No accounts yet — click '+ Add Account'.", text_color="#8a8f9a"
        )

        # Footer: join controls
        footer = ctk.CTkFrame(acc_tab, fg_color="transparent")
        footer.pack(fill="x", pady=(6, 0))

        ctk.CTkLabel(footer, text="Place:").pack(side="left")
        self.place_entry = ctk.CTkEntry(footer, width=180, placeholder_text="Place ID or game URL")
        self.place_entry.pack(side="left", padx=(4, 10))
        self.place_entry.insert(0, store.get_setting("last_place", ""))
        ctk.CTkLabel(footer, text="Job ID:").pack(side="left")
        self.job_entry = ctk.CTkEntry(footer, width=160, placeholder_text="optional")
        self.job_entry.pack(side="left", padx=(4, 10))
        self.job_entry.insert(0, store.get_setting("last_job", ""))

        btns = ctk.CTkFrame(acc_tab, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        self.launch_sel_btn = ctk.CTkButton(
            btns, text="Launch Selected", fg_color=ACCENT, hover_color="#27853f",
            command=self.launch_selected,
        )
        self.launch_sel_btn.pack(side="left")
        self.launch_all_btn = ctk.CTkButton(
            btns, text="Launch All", command=self.launch_all
        )
        self.launch_all_btn.pack(side="left", padx=8)

        # Clients tab — external Roblox processes not launched via the manager
        self.ext_frame = ctk.CTkScrollableFrame(ext_tab, fg_color="#1b1d22")
        self.ext_frame.pack(fill="both", expand=True)
        self.ext_empty = ctk.CTkLabel(
            self.ext_frame,
            text="No external Roblox clients detected.\nClients launched outside the manager appear here.",
            text_color="#8a8f9a",
        )
        self.ext_empty.pack(pady=30)

        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color="#9ab")
        self.status.pack(fill="x", padx=16, pady=(0, 10))

        self._update_tile_btn()
        self._overlay_tick()
        self._tile_tick()
        if store.get_setting("low_ram", False):
            self._start_trimmer()
        # Enable multi-instance up front so launches are never blocked.
        self._apply_multi_result(launcher.enable_multi_instance())
        self.refresh_accounts()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._throttle_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pending_update: tuple[str, str] | None = None
        threading.Thread(target=self._update_check, daemon=True).start()

    # ---------- auto-update ----------

    def _update_check(self, manual: bool = False):
        found = updater.check_for_update()
        if found:
            self._pending_update = found
            ver = found[0]
            self.after(0, lambda: self._offer_update(ver))
        elif manual:
            v = version.APP_VERSION
            self.after(0, lambda: self.set_status(f"{version.APP_NAME} v{v} — you're up to date."))

    def _offer_update(self, ver: str):
        self.set_status(f"Update v{ver} available — click 'Update' to install.")
        self.update_btn.configure(text=f"Update → v{ver}")
        self.update_btn.pack(side="right", padx=(0, 4))

    def _install_update(self):
        if not self._pending_update:
            return
        ver, url = self._pending_update
        if not getattr(sys, "frozen", False):
            import webbrowser
            webbrowser.open(updater.RELEASES_PAGE)
            return
        self.update_btn.configure(state="disabled", text="Downloading...")

        def work():
            try:
                tmp = os.path.join(tempfile.gettempdir(), f"{version.APP_NAME}-{ver}.exe")
                updater.download(url, tmp)
            except Exception as e:
                self.after(0, lambda: self._update_failed(e))
                return
            self.after(0, lambda: self._update_ready(tmp, ver))

        threading.Thread(target=work, daemon=True).start()

    def _update_ready(self, tmp: str, ver: str):
        from tkinter import messagebox
        if messagebox.askyesno(
            "Update ready", f"{version.APP_NAME} v{ver} downloaded.\nRestart now to finish installing?"
        ):
            updater.apply_update(tmp)  # never returns
        self.update_btn.configure(state="normal", text=f"Update → v{ver}")

    def _update_failed(self, e: Exception):
        self.set_status(f"Update download failed: {e}")
        self.update_btn.configure(state="normal", text="Retry update")

    def _restore_all(self):
        for pid in self._running_pids.values():
            hwnd = launcher.find_window_for_pid(pid)
            if hwnd and launcher.is_iconic(hwnd):
                launcher.show_restored(hwnd)
        for t in list(self._tiles.values()):
            if self._alive(t):
                t.deiconify()
        self._bring_all_front()

    def _bring_all_front(self):
        wins = launcher.windows_for_pids(set(self._running_pids.values()))
        for hwnd in wins.values():
            launcher.raise_above_others(hwnd)
        for t in list(self._tiles.values()):
            if self._alive(t):
                try:
                    t.attributes("-topmost", True)
                    t.attributes("-topmost", False)
                except Exception:
                    pass
        self.lift()

    # ---------- client watchdog (running state + keep-active) ----------

    def _watchdog(self):
        while not self._watch_stop.wait(4):
            clients = launcher.roblox_clients()
            self.after(0, lambda c=clients: self._update_running(c))
            now = time.time()
            if now - self._last_ping >= 300 and self._ping_keep_active(clients):
                self._last_ping = now

    def _update_running(self, clients: list[dict]):
        btid_map = {c["btid"]: c for c in clients if c["btid"]}
        running: dict[int, int] = {}
        overlay_on = bool(store.get_setting("overlay", True)) and not store.get_setting("tile", True)
        for row in self._rows:
            uid = row.account["user_id"]
            btid = row.account.get("btid") or store.ensure_btid(uid)
            row.account["btid"] = btid
            c = btid_map.get(btid)
            if c:
                running[uid] = c["pid"]
                mem_mb = c["mem_kb"] / 1024
                row.set_running({"mem_mb": mem_mb}, c["pid"])
                if overlay_on:
                    ov = self._overlays.get(uid)
                    if ov is not None and not self._alive(ov):
                        ov = None
                    if ov is None:
                        hwnd = launcher.find_window_for_pid(c["pid"])
                        ov = GameOverlay(self, row.account, hwnd, c["pid"]) if hwnd else None
                        self._overlays[uid] = ov
                    elif ov.pid != c["pid"]:
                        ov.pid = c["pid"]
                        ov.hwnd = launcher.find_window_for_pid(c["pid"])
                    if ov:
                        ov.set_mem(mem_mb)
                t = self._tiles.get(uid)
                if t and self._alive(t):
                    t.set_mem(mem_mb)
            else:
                row.set_running(None, None)
                ov = self._overlays.pop(uid, None)
                if ov:
                    ov.destroy()
        self._running_pids = running

        # external clients — running but matching no account's tracker id
        acc_btids = {r.account.get("btid") for r in self._rows}
        ext = [c for c in clients if c["btid"] not in acc_btids]
        seen = {c["pid"] for c in ext}
        for pid in list(self._ext_rows):
            if pid not in seen:
                row = self._ext_rows.pop(pid)
                self._ext_active.discard(pid)
                self._ext_notrim.discard(pid)
                self._ext_afk_key.pop(pid, None)
                self._ext_afk_back.discard(pid)
                if self._alive(row):
                    row.destroy()
        for c in ext:
            row = self._ext_rows.get(c["pid"])
            if row is None or not self._alive(row):
                row = ExtClientRow(self.ext_frame, self, c)
                row.pack(fill="x", pady=3, padx=4)
                self._ext_rows[c["pid"]] = row
            row.set_mem(c["mem_kb"] / 1024)
        if ext:
            self.ext_empty.pack_forget()
        else:
            self.ext_empty.pack(pady=30)

    @staticmethod
    def _alive(w) -> bool:
        try:
            return bool(w.winfo_exists())
        except Exception:
            return False

    def _row_for(self, uid: int):
        for row in self._rows:
            if row.account["user_id"] == uid:
                return row
        return None

    def _overlay_tick(self):
        for ov in list(self._overlays.values()):
            if self._alive(ov):
                ov.tick()
        self.after(400, self._overlay_tick)

    def _open_settings(self):
        if self._settings is not None and self._alive(self._settings):
            self._settings.focus()
            return
        self._settings = SettingsDialog(self)
        self._settings.sync()

    def _sync_settings(self):
        if self._settings is not None and self._alive(self._settings):
            self._settings.sync()

    def _set_overlay(self, on: bool):
        store.set_setting("overlay", on)
        if not on:
            for ov in self._overlays.values():
                if self._alive(ov):
                    ov.destroy()
            self._overlays.clear()
        self._sync_settings()

    def _toggle_overlay(self):
        self._set_overlay(not store.get_setting("overlay", True))

    # ---------- tiling ----------

    def _set_tile(self, on: bool):
        store.set_setting("tile", on)
        if not on:
            for t in list(self._tiles.values()):
                if self._alive(t):
                    t.close()
            self._tiles.clear()
            self._tile_rects.clear()
            self._throttle_set(set())
            for hwnd in list(self._tooled):
                launcher.set_tool_window(hwnd, False)
            self._tooled.clear()
            # un-park windows back into a visible cascade
            for i, (uid, pid) in enumerate(self._running_pids.items()):
                hwnd = launcher.find_window_for_pid(pid)
                if hwnd:
                    launcher.move_window(hwnd, 160 + 40 * i, 60 + 30 * i, 1150, 680)
            self.set_status("Tiling off — windows restored.")
        else:
            self.set_status("Tiling on — click any mini window to make it the main one.")
        self._update_tile_btn()
        self._sync_settings()

    def _toggle_tile(self):
        self._set_tile(not store.get_setting("tile", True))

    def _update_tile_btn(self):
        on = bool(store.get_setting("tile", True))
        self.tile_btn.configure(
            text=f"Tile: {'ON' if on else 'OFF'}",
            fg_color=ACCENT if on else "#3a3d45",
            hover_color="#27853f" if on else "#4a4d55",
        )

    def _set_hide(self, on: bool):
        self.hide_names = on
        store.set_setting("hide_names", on)
        for r in self._rows:
            r.apply_name_mask()
        for ov in self._overlays.values():
            if self._alive(ov):
                ov._sync()
        self._sync_settings()

    def _toggle_hide(self):
        self._set_hide(not self.hide_names)

    def _tile_tick(self):
        if store.get_setting("tile", True) and self._running_pids:
            self._arrange_tiles()
        self.after(500, self._tile_tick)

    def _throttle_set(self, pids: set[int]):
        """Apply/remove CPU throttle on a set of pids."""
        for pid in list(self._throttled):
            if pid not in pids:
                h = self._throttled.pop(pid)
                launcher.resume_process(h)
                launcher.set_efficiency(h, False)
                launcher.set_low_priority(h, False)
                launcher.kernel32.CloseHandle(h)
        for pid in pids:
            if pid not in self._throttled:
                h = launcher.open_throttle_handle(pid)
                if not h:
                    continue
                launcher.set_efficiency(h, True)
                launcher.set_low_priority(h, True)
                self._throttled[pid] = h

    def _throttle_loop(self):
        # ~66% of each 90ms cycle suspended ≈ ~20fps effective rate on parked
        # clients — real CPU cut, and millisecond gaps don't trip disconnects
        while not self._throttle_stop.wait(0.03):
            if self._afk_running:
                continue
            hs = list(self._throttled.values())
            if not hs:
                continue
            for h in hs:
                launcher.suspend_process(h)
            time.sleep(0.06)
            for h in hs:
                launcher.resume_process(h)

    def _arrange_tiles(self):
        pids = set(self._running_pids.values())
        wins = launcher.windows_for_pids(pids)  # pid -> hwnd
        if not wins:
            self._throttle_set(set())
            return

        # keep a stable order: running uids first, new ones appended
        order = [u for u in self._tile_order if u in self._running_pids]
        for uid in self._running_pids:
            if uid not in order:
                order.append(uid)

        # whichever client window is foreground gets the center slot
        fg = launcher.foreground_hwnd()
        # honor a recent explicit tile-click / AFK nudge before fg-promoting
        if not self._afk_running and time.time() - self._last_promote > 1.0:
            for i in range(1, len(order)):
                if wins.get(self._running_pids[order[i]]) == fg:
                    order.insert(0, order.pop(i))
                    break

        self._tile_order = order
        wa = launcher.work_area()
        slots = launcher.tile_layout(*wa)
        park = (wa[0] + wa[2] + 200, wa[1], 800, 500)  # parked just off-screen
        names = {
            r.account["user_id"]: (
                "Alt" if self.hide_names
                else (r.account.get("display_name") or r.account["username"])
            )
            for r in self._rows
        }
        used: set[int] = set()

        for i, uid in enumerate(order):
            hwnd = wins.get(self._running_pids[uid])
            if not hwnd:
                continue
            if hwnd not in self._tooled:
                launcher.set_tool_window(hwnd, True)  # one combined taskbar app
                self._tooled.add(hwnd)
            if i == 0:
                target = slots[0]
                if self._tile_rects.get(uid) != target:
                    launcher.move_window(hwnd, *target)
                    self._tile_rects[uid] = target
            elif i < len(slots):
                # real window parked off-screen; its slot shows a live thumbnail
                if self._tile_rects.get(uid) != park:
                    launcher.move_window(hwnd, *park)
                    self._tile_rects[uid] = park
                tile = self._tiles.get(uid)
                if tile is None or not self._alive(tile):
                    tile = MiniTile(self, slots[i])
                    self._tiles[uid] = tile
                tile.assign(uid, hwnd, names.get(uid, str(uid)), slots[i])
                used.add(uid)
            else:
                if self._tile_rects.get(uid) != park:
                    launcher.move_window(hwnd, *park)
                    self._tile_rects[uid] = park

        for uid in list(self._tiles):
            if uid not in used:
                t = self._tiles.pop(uid)
                if self._alive(t):
                    t.close()

        # every non-center client gets CPU-throttled (~20fps feel + eco mode)
        self._throttle_set({self._running_pids[u] for u in order[1:]})

    def _tile_promote(self, uid: int):
        if uid in self._tile_order:
            self._tile_order.remove(uid)
            self._tile_order.insert(0, uid)
            self._last_promote = time.time()
            self._arrange_tiles()
            pid = self._running_pids.get(uid)
            if pid:
                hwnd = launcher.find_window_for_pid(pid)
                if hwnd:
                    launcher.set_foreground(hwnd)

    def _nudge_now(self):
        self.set_status("Keep-alive: nudging Active clients...")
        threading.Thread(
            target=lambda: self._ping_keep_active(launcher.roblox_clients(), force=True)
            or True, daemon=True,
        ).start()

    def _ping_keep_active(self, clients: list[dict], force: bool = False) -> bool:
        """Nudge every AFK-enabled client via focus + real SendInput.
        Returns False while the user is mid-input so the cycle retries soon."""
        if not force and launcher.last_input_ms() < 1500:
            return False
        self._afk_running = True
        nudged, missed = 0, 0
        try:
            btid_map = {c["btid"]: c for c in clients if c["btid"]}
            fg = launcher.foreground_hwnd()
            for acc in store.list_accounts():
                if not acc.get("keep_active"):
                    continue
                c = btid_map.get(acc.get("btid") or "")
                if not c:
                    continue
                hwnd = launcher.find_window_for_pid(c["pid"])
                if not hwnd:
                    continue
                ok = launcher.inject_activity(
                    hwnd, key=acc.get("afk_key") or "TURN",
                    go_back=bool(acc.get("afk_return")),
                )
                nudged += ok
                missed += not ok
                time.sleep(0.15)
            # external clients opted into keep-active
            for pid in set(self._ext_active):
                hwnd = launcher.find_window_for_pid(pid)
                if not hwnd:
                    continue
                ok = launcher.inject_activity(
                    hwnd, key=self._ext_afk_key.get(pid, "TURN"),
                    go_back=pid in self._ext_afk_back,
                )
                nudged += ok
                missed += not ok
                time.sleep(0.15)
            if nudged or missed:
                msg = f"Keep-alive: nudged {nudged} client(s)"
                if missed:
                    msg += f" — {missed} failed to take focus"
                self.after(0, lambda m=msg: self.set_status(m))
            return True
        finally:
            self._afk_running = False
            # extra grace so a just-nudged client doesn't get fg-promoted
            self._last_promote = time.time()

    def _warn_if_roblox_running(self):
        n = launcher.running_roblox_count()
        if n:
            self.set_status(
                f"⚠ {n} Roblox client(s) running in background — check the system tray "
                "or hit 'Kill Clients', or launches will merge into it."
            )

    # ---------- multi-instance ----------

    def _kill_clients(self):
        n = launcher.kill_all_roblox()
        self.set_status(f"Killed {n} Roblox client(s)." if n else "No Roblox clients running.")

    # ---------- low RAM ----------

    def _set_lowram(self, on: bool):
        ok = launcher.apply_low_ram_flags(on)
        if ok or not on:
            store.set_setting("low_ram", on)
            if on:
                self._start_trimmer()
                n = launcher.trim_working_set()
                self.set_status(
                    f"Low-RAM on — flags applied, trimmed {n} running client(s). "
                    "Trims background clients every 30s."
                )
            else:
                self._stop_trimmer()
        else:
            self.set_status("Couldn't write ClientAppSettings.json — Roblox not found?")
        self._sync_settings()

    def _toggle_lowram(self):
        self._set_lowram(not store.get_setting("low_ram", False))

    def _start_trimmer(self):
        self._trim_stop.clear()

        def loop():
            while not self._trim_stop.wait(30):
                no_trim = {a["user_id"] for a in store.list_accounts() if a.get("no_trim")}
                skip = ({pid for uid, pid in self._running_pids.items() if uid in no_trim}
                        | set(self._ext_notrim))
                launcher.trim_working_set(skip)

        threading.Thread(target=loop, daemon=True).start()

    def _stop_trimmer(self):
        self._trim_stop.set()

    # ---------- join mode ----------

    def _set_joinmode(self, mode: str):
        store.set_setting("join_mode", mode)
        self.set_status(f"Join mode: --{mode} (applies to new launches)")
        self._sync_settings()

    def _toggle_joinmode(self):
        self._set_joinmode("play" if store.get_setting("join_mode", "app") == "app" else "app")

    def _set_multi(self, on: bool):
        if on and not launcher.multi_instance_enabled():
            self._apply_multi_result(launcher.enable_multi_instance())
        elif not on and launcher.multi_instance_enabled():
            launcher.disable_multi_instance()
        self._sync_settings()

    def _toggle_multi(self):
        self._set_multi(not launcher.multi_instance_enabled())

    def _apply_multi_result(self, res: str):
        if res == "ok":
            self._warn_if_roblox_running()
        elif res == "busy":
            self.set_status(
                "⚠ Multi-instance BLOCKED — a Roblox client already holds the "
                "singleton. Hit 'Kill' then re-enable Multi-instance."
            )
        else:
            self.set_status("Failed to enable multi-instance (couldn't create mutex).")
        self._sync_settings()

    # ---------- accounts ----------

    def get_avatar(self, uid: int) -> ctk.CTkImage:
        if uid not in self._avatars:
            self._avatars[uid] = _avatar_image(store.avatar_path(uid))
        return self._avatars[uid]

    def refresh_accounts(self):
        for row in self._rows:
            row.destroy()
        self._rows.clear()

        accounts = store.list_accounts()
        if not accounts:
            self.empty_label.pack(pady=30)
        else:
            self.empty_label.pack_forget()
            for acc in accounts:
                row = AccountRow(self.list_frame, self, acc)
                row.pack(fill="x", pady=4)
                self._rows.append(row)
                if not os.path.exists(store.avatar_path(acc["user_id"])):
                    threading.Thread(
                        target=self._fetch_avatar, args=(acc["user_id"],), daemon=True
                    ).start()

    def _fetch_avatar(self, uid: int):
        png = api.get_avatar_headshot(uid)
        if png:
            path = store.avatar_path(uid)
            try:
                with open(path, "wb") as f:
                    f.write(png)
            except OSError:
                return
            self.after(0, lambda: self._reload_avatar(uid))

    def _reload_avatar(self, uid: int):
        self._avatars.pop(uid, None)
        self.refresh_accounts()

    def remove_account(self, account: dict):
        uid = account["user_id"]
        store.remove_account(uid)
        ov = self._overlays.pop(uid, None)
        if ov and self._alive(ov):
            ov.destroy()
        self.set_status(f"Removed @{account['username']}")
        self.refresh_accounts()

    # ---------- launching ----------

    def _open_add(self):
        AddDialog(self)

    def _dest(self):
        place = launcher.extract_place_id(self.place_entry.get())
        job = self.job_entry.get().strip() or None
        return place, job

    def launch_accounts(self, accounts: list[dict]):
        if self._launching or not accounts:
            return
        place_raw = self.place_entry.get().strip()
        place, job = self._dest()
        if place_raw and not place:
            self.set_status("Couldn't parse Place ID — use a numeric ID or game URL.")
            return
        store.set_setting("last_place", place_raw)
        store.set_setting("last_job", self.job_entry.get().strip())
        self._launching = True
        self._set_launch_btns("disabled")
        if not launcher.multi_instance_enabled() and len(accounts) > 1:
            self._apply_multi_result(launcher.enable_multi_instance())
        running = launcher.running_roblox_count()
        if running:
            self.set_status(
                f"⚠ {running} Roblox client(s) already running — launches may merge. "
                "Consider 'Kill Clients' first."
            )

        def work():
            if store.get_setting("low_ram", False):
                launcher.apply_low_ram_flags(True)
            join_mode = store.get_setting("join_mode", "app")
            ok, fail = 0, 0
            for acc in accounts:
                try:
                    btid = store.ensure_btid(acc["user_id"])
                    launcher.kill_by_btid(btid)
                    launcher.launch_account(acc["cookie_value"], place, job, btid, join_mode)
                    ok += 1
                    self.after(0, lambda n=acc["username"], k=ok, f=fail:
                               self.set_status(
                                   f"Launched {'Alt' if self.hide_names else '@'+n}"
                                   f"  ({k} ok, {f} failed)"))
                except Exception as e:
                    if not isinstance(e, api.ApiError):
                        _log_exc(e)
                    fail += 1
                    m = str(e) or repr(e)
                    self.after(0, lambda n=acc["username"], m=m:
                               self.set_status(f"@{n}: {m}"))
                # RAM uses an 8s stagger: each client's ticket must finish
                # redeeming before the next launch or sessions collide on the
                # shared cookie file -> "two of the same account".
                time.sleep(6)
            self.after(0, self._launch_done)

        threading.Thread(target=work, daemon=True).start()

    def launch_selected(self):
        self.launch_accounts([r.account for r in self._rows if r.selected.get()])

    def launch_all(self):
        self.launch_accounts([r.account for r in self._rows])

    def _launch_done(self):
        self._launching = False
        self._set_launch_btns("normal")

    def _set_launch_btns(self, state: str):
        self.launch_sel_btn.configure(state=state)
        self.launch_all_btn.configure(state=state)
        for r in self._rows:
            for child in r.winfo_children():
                if isinstance(child, ctk.CTkButton):
                    child.configure(state=state)

    def set_status(self, text: str):
        self.status.configure(text=text)

    def _on_close(self):
        self._stop_trimmer()
        self._watch_stop.set()
        for hwnd in list(self._tooled):
            launcher.set_tool_window(hwnd, False)
        self._tooled.clear()
        self._throttle_set(set())
        launcher.disable_multi_instance()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
