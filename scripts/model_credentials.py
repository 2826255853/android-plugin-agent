"""OpenAI credential storage in the current Windows user's Credential Manager."""

import ctypes
import os
import re
from ctypes import wintypes

from model_boundary import ModelError


TARGET = "AndroidPluginAgent/OpenAI"
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{16,500}")


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _windows():
    if os.name != "nt":
        raise ModelError("MODEL_CONFIG_INVALID")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential)))
    api.CredWriteW.argtypes = (ctypes.POINTER(Credential), wintypes.DWORD)
    api.CredFree.argtypes = (ctypes.c_void_p,)
    api.CredFree.restype = None
    return api


def save_openai_key(key: str):
    save_key(TARGET, key)


def save_key(target: str, key: str):
    if not KEY_PATTERN.fullmatch(key):
        raise ModelError("MODEL_CONFIG_INVALID")
    api = _windows()
    raw = ctypes.create_string_buffer(key.encode("ascii"))
    item = Credential(Type=1, TargetName=target, CredentialBlobSize=len(key),
                      CredentialBlob=ctypes.addressof(raw), Persist=2, UserName="AndroidPluginAgent")
    try:
        if not api.CredWriteW(ctypes.byref(item), 0):
            raise ModelError("MODEL_CONFIG_INVALID")
    finally:
        ctypes.memset(raw, 0, ctypes.sizeof(raw))


def read_openai_key() -> str:
    return read_key(TARGET)


def read_key(target: str) -> str:
    api = _windows()
    item = ctypes.POINTER(Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(item)):
        raise ModelError("MODEL_CONFIG_INVALID")
    try:
        try:
            key = ctypes.string_at(item.contents.CredentialBlob, item.contents.CredentialBlobSize).decode("ascii")
        except (ValueError, UnicodeError):
            raise ModelError("MODEL_CONFIG_INVALID") from None
        if not KEY_PATTERN.fullmatch(key):
            raise ModelError("MODEL_CONFIG_INVALID")
        return key
    finally:
        api.CredFree(item)
