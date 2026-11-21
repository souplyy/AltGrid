"""Self-update against GitHub Releases.

The running exe can't overwrite itself on Windows, so apply_update() drops a
detached .bat that waits for our PID to exit, swaps in the new exe, relaunches
it, then deletes itself.
"""

import os
import subprocess
import sys
import tempfile

import requests

import version

RELEASES_API = f"https://api.github.com/repos/{version.GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{version.GITHUB_REPO}/releases/latest"


def _parse_tag(tag: str) -> tuple[int, ...]:
    parts = []
    for p in tag.strip().lstrip("vV").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_update() -> tuple[str, str] | None:
    """Return (new_version, exe_download_url) if a newer release exists."""
    try:
        r = requests.get(
            RELEASES_API,
            timeout=15,
            headers={"Accept": "application/vnd.github+json"},
        )
        data = r.json()
    except Exception:
        return None
    tag = data.get("tag_name", "")
    if not tag or _parse_tag(tag) <= _parse_tag(version.APP_VERSION):
        return None
    for asset in data.get("assets", []):
        url = asset.get("browser_download_url", "")
        if url.lower().endswith(".exe"):
            return tag.lstrip("vV"), url
    return None


def download(url: str, dest: str) -> None:
    """Download the release asset to dest."""
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)


def apply_update(new_exe_path: str) -> None:
    """Swap in the downloaded exe and relaunch. Never returns."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("in-place update only works in the packaged build")
    exe = os.path.abspath(sys.executable)
    pid = os.getpid()
    bat = os.path.join(tempfile.gettempdir(), "altgrid_update.bat")
    with open(bat, "w", encoding="ascii") as f:
        f.write(
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" | findstr /C:"{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "    timeout /t 1 /nobreak >nul\r\n"
            "    goto wait\r\n"
            ")\r\n"
            f'move /y "{new_exe_path}" "{exe}"\r\n'
            f'start "" "{exe}"\r\n'
            'del "%~f0"\r\n'
        )
    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
    )
    os._exit(0)
