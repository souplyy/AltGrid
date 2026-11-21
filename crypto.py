"""DPAPI encryption via ctypes (no pywin32 dependency).

Cookies are encrypted with CryptProtectData so they can only be decrypted
by the same Windows user account on this machine.
"""

import ctypes
from ctypes import wintypes

crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    # keep a reference so the buffer isn't freed while in use
    blob._buf = buf  # type: ignore[attr-defined]
    return blob


def _read_blob(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def protect(data: bytes, entropy: bytes = b"rbxmanager") -> bytes:
    blob_in = _make_blob(data)
    blob_ent = _make_blob(entropy)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    return _read_blob(blob_out)


def unprotect(data: bytes, entropy: bytes = b"rbxmanager") -> bytes:
    blob_in = _make_blob(data)
    blob_ent = _make_blob(entropy)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    return _read_blob(blob_out)
