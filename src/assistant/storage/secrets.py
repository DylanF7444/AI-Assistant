from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


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


class SecretVault:
    """Persist named DPAPI-encrypted secrets without exposing them in settings."""

    def __init__(self, root: Path, cipher: SecretStore | None = None) -> None:
        self.root = root
        self.cipher = cipher or SecretStore()

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return self.cipher.decrypt(path.read_bytes())

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            raise ValueError("Secret value cannot be empty")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self.cipher.encrypt(value))
        os.replace(temporary, path)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def has(self, name: str) -> bool:
        return self._path(name).is_file()

    def _path(self, name: str) -> Path:
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name):
            raise ValueError("Invalid secret name")
        return self.root / f"{name}.bin"
