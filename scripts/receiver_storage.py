"""Windows account protection for the receiver's certificate and device registry."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import ssl
import subprocess
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4


class StorageError(RuntimeError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


def private_directory(path: Path, *, existing_ok: bool = False) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    # Protected ACL with full control for the owner and SYSTEM, inherited by children.
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        ctypes.c_wchar_p("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"), 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if kernel.CreateDirectoryW(ctypes.c_wchar_p(str(path)), ctypes.byref(attributes)):
            return
        error = ctypes.get_last_error()
        if not (existing_ok and error == 183 and path.is_dir() and not path.is_symlink() and not path.is_junction()):
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def dpapi(data: bytes, *, encrypt: bool) -> bytes:
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    source_bytes = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(source_bytes))
    result = DataBlob()
    apply = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN, using the default current-user scope.
    if not apply(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.c_void_p(result.data))


def openssl_executable() -> Path:
    openssl = shutil.which("openssl")
    if openssl:
        return Path(openssl)
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parents[1] / "usr" / "bin" / "openssl.exe"
        if bundled.is_file():
            return bundled
    raise StorageError("OPENSSL_NOT_FOUND")


class ReceiverStorage:
    def __init__(self) -> None:
        if os.name != "nt":
            raise StorageError("WINDOWS_DPAPI_REQUIRED")
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise StorageError("LOCALAPPDATA_NOT_FOUND")
        self.root = Path(local) / "AndroidPluginAgentReceiver"

    @contextmanager
    def session(self):
        import msvcrt

        private_directory(self.root, existing_ok=True)
        with (self.root / "receiver.lock").open("a+b") as lock:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise StorageError("RECEIVER_ALREADY_RUNNING") from exc
            yield

    def write(self, name: str, data: bytes) -> None:
        path = self.root / name
        temporary = self.root / f".{name}-{uuid4().hex}"
        try:
            with temporary.open("xb") as stream:
                stream.write(dpapi(data, encrypt=True))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, name: str) -> bytes:
        return dpapi((self.root / name).read_bytes(), encrypt=False)

    def load_devices(self) -> dict:
        try:
            return json.loads(self.read("devices.dpapi"))
        except FileNotFoundError:
            return {}

    def save_devices(self, devices: dict) -> None:
        self.write("devices.dpapi", json.dumps(devices, ensure_ascii=True).encode("ascii"))

    def tls_identity(self) -> tuple[ssl.SSLContext, str]:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        working = self.root / f"tls-{uuid4().hex}"
        private_directory(working)
        try:
            pem = working / "identity.pem"
            try:
                bundle = self.read("tls.dpapi")
            except FileNotFoundError:
                key, certificate = working / "key.pem", working / "certificate.pem"
                password = secrets.token_urlsafe(32).encode("ascii")
                try:
                    subprocess.run(
                        [openssl_executable(), "req", "-x509", "-newkey", "ec",
                         "-pkeyopt", "ec_paramgen_curve:P-256", "-passout", "stdin", "-sha256", "-days", "3650",
                         "-subj", "/CN=Android Plugin Agent Receiver",
                         "-addext", "basicConstraints=critical,CA:FALSE",
                         "-addext", "extendedKeyUsage=serverAuth",
                         "-keyout", key, "-out", certificate],
                        input=password + b"\n", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=15, check=True,
                    )
                except subprocess.SubprocessError as exc:
                    raise StorageError("TLS_IDENTITY_CREATION_FAILED") from exc
                encrypted_key = key.read_bytes()
                if not encrypted_key.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----"):
                    raise StorageError("TLS_IDENTITY_CREATION_FAILED")
                bundle = password + b"\n" + encrypted_key + certificate.read_bytes()
                self.write("tls.dpapi", bundle)
            password, encrypted_pem = bundle.split(b"\n", 1)
            pem.write_bytes(encrypted_pem)
            context.load_cert_chain(pem, password=password)
            certificate_pem = encrypted_pem[encrypted_pem.index(b"-----BEGIN CERTIFICATE-----"):]
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate_pem.decode("ascii"))).hexdigest()
            return context, fingerprint
        finally:
            shutil.rmtree(working)
