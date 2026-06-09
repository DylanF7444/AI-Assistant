from __future__ import annotations

import ctypes
from ctypes import wintypes


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


class SecretStore:
    """Windows DPAPI encryption bound to the current user account."""

    def encrypt(self, plaintext: str) -> bytes:
        if not hasattr(ctypes, "windll"):
            raise RuntimeError("DPAPI secret storage is available only on Windows")
        source, source_buffer = _blob(plaintext.encode("utf-8"))
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source),
            "LocalAssistant",
            None,
            None,
            None,
            0x1,
            ctypes.byref(output),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
            del source_buffer

    def decrypt(self, ciphertext: bytes) -> str:
        if not hasattr(ctypes, "windll"):
            raise RuntimeError("DPAPI secret storage is available only on Windows")
        source, source_buffer = _blob(ciphertext)
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
            del source_buffer

