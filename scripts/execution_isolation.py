"""Run one build, test, or browser preview in an offline disposable VM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


HERE = Path(__file__).resolve().parent
ENTRIES = {'build': 'build.sh', 'test': 'test.sh', 'preview': 'index.html'}
MAX_INPUT = 8 * 1024**2
MAX_SERIAL = 128 * 1024
READY = b'C4_READY'
RESULT = b'C4_RESULT '
GUEST_CODES = {'OK', 'ISOLATION_WORKLOAD_FAILED', 'ISOLATION_MEMORY_LIMIT',
               'ISOLATION_PROCESS_LIMIT', 'ISOLATION_CPU_LIMIT', 'ISOLATION_DISK_LIMIT'}
VM_LAUNCHER = """
import subprocess
import sys

if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(2)
raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW))
"""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 90
    cpu_seconds: int = 20
    memory_mib: int = 768
    disk_mib: int = 64
    processes: int = 128

    def guest(self):
        if not 0 < self.timeout_seconds <= 180:
            raise ValueError('Timeout outside sandbox limits')
        bounds = {'cpuSeconds': (self.cpu_seconds, 60), 'memoryMiB': (self.memory_mib, 768),
                  'diskMiB': (self.disk_mib, 64), 'processes': (self.processes, 128)}
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in bounds.values()):
            raise ValueError('Resource outside sandbox limits')
        return {name: value for name, (value, _) in bounds.items()}


@dataclass(frozen=True)
class SandboxConfig:
    qemu: Path
    image: Path
    kernel_sha256: str
    rootfs_sha256: str

    @classmethod
    def load(cls, qemu: Path, image: Path):
        metadata = json.loads((image / 'image.json').read_text(encoding='utf-8'))
        return cls(qemu, image, metadata['vmlinuz'], metadata['rootfs.cpio.gz'])


def input_archive(files: Mapping[str, bytes], profile: str, limits: Limits) -> bytes:
    if profile not in ENTRIES or ENTRIES[profile] not in files or not 1 <= len(files) <= 64:
        raise ValueError('Missing or unsupported workload')
    directories = {'input'}
    for name, data in files.items():
        parts = name.split('/')
        if len(name) > 160 or len(parts) > 8 or not all(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', p)
                                                       for p in parts) or not isinstance(data, bytes):
            raise ValueError('Invalid input name or content')
        directories.update('input/' + '/'.join(parts[:i]) for i in range(1, len(parts)))
    if sum(map(len, files.values())) > MAX_INPUT or any('input/' + name in directories for name in files):
        raise ValueError('Input size or path conflict')

    archive = bytearray()

    def entry(name, mode, data=b''):
        name = name.encode('ascii') + b'\0'
        fields = (0, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)
        archive.extend(b'070701' + ''.join(f'{number:08x}' for number in fields).encode('ascii') + name)
        archive.extend(b'\0' * (-len(archive) % 4))
        archive.extend(data)
        archive.extend(b'\0' * (-len(archive) % 4))

    for name in sorted(directories):
        entry(name, 0o40555)
    for name, data in sorted(files.items()):
        entry('input/' + name, 0o100444, data)
    entry('c4-init', 0o100755, b'#!/bin/sh\nexec /usr/bin/python3 -I -S /c4-guest.py\n')
    entry('c4-guest.py', 0o100444, (HERE / 'sandbox_guest.py').read_bytes())
    entry('c4-config.json', 0o100444, json.dumps({'profile': profile, 'limits': limits.guest()}).encode('ascii'))
    entry('TRAILER!!!', 0)
    return bytes(archive)


class Stopped(Exception):
    pass


def copy_verified_image(path, expected, maximum, check_stop, output):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('Invalid image digest')
    digest = hashlib.sha256()
    total = 0
    with path.open('rb') as source:
        while chunk := source.read(1024**2):
            check_stop()
            total += len(chunk)
            if total > maximum:
                raise ValueError('Image size exceeds sandbox limit')
            digest.update(chunk)
            output.write(chunk)
    if not total or digest.hexdigest() != expected:
        raise ValueError('Invalid image digest')


class SerialOutput:
    def __init__(self):
        self.data = bytearray()
        self.overflow = threading.Event()
        self.ready = threading.Event()

    def read(self, stream):
        with stream:
            while chunk := stream.read1(4096):
                if len(self.data) + len(chunk) > MAX_SERIAL:
                    self.overflow.set()
                    return
                self.data.extend(chunk)
                if not self.ready.is_set() and READY in self.data.splitlines():
                    self.ready.set()

    def report(self):
        replies = [line[len(RESULT):] for line in self.data.splitlines() if line.startswith(RESULT)]
        if len(replies) != 1:
            raise ValueError('Missing VM report')
        report = json.loads(replies[0])
        if report['code'] == 'ISOLATION_UNAVAILABLE':
            return report
        if not self.ready.is_set() or report['code'] not in GUEST_CODES or report['guestProcesses'] != 0:
            raise ValueError('Invalid VM report')
        return report


def qemu_command(executable, directory):
    return [str(executable), '-no-user-config', '-nodefaults', '-machine', 'q35,usb=off',
            '-accel', 'tcg,tb-size=128',
            '-cpu', 'max', '-smp', '1', '-m', '2048', '-nic', 'none', '-display', 'none', '-monitor', 'none',
            '-serial', 'stdio', '-no-reboot', '-kernel', str(directory / 'vmlinuz'),
            '-initrd', str(directory / 'initrd'), '-append',
            'rdinit=/c4-init console=ttyS0,115200 quiet loglevel=3 panic=1 cgroup_no_v1=all']


def run_vm(command, directory, check_stop, ready):
    from sandbox_job import WindowsJob

    serial = SerialOutput()
    system_root = os.environ['SystemRoot']
    environment = {'SystemRoot': system_root, 'PATH': os.pathsep.join((str(Path(command[0]).parent),
                   str(Path(system_root) / 'System32'))), 'TEMP': str(directory), 'TMP': str(directory)}
    total = active = 0
    code = None
    with WindowsJob() as job:
        # The trusted launcher waits for a byte before spawning QEMU. Job assignment is mandatory.
        with subprocess.Popen([sys.executable, '-I', '-S', '-c', VM_LAUNCHER, *command], cwd=directory, env=environment,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW) as launcher:
            reader = threading.Thread(target=serial.read, args=(launcher.stdout,), daemon=True)
            assigned = False
            try:
                job.assign(launcher.pid)
                assigned = True
                reader.start()
                check_stop()
                launcher.stdin.write(b'1')
                launcher.stdin.close()
                while launcher.poll() is None:
                    check_stop()
                    if ready is not None and serial.ready.is_set():
                        ready.set()
                    if serial.overflow.is_set():
                        raise Stopped('ISOLATION_OUTPUT_LIMIT')
                    time.sleep(0.03)
                check_stop()
            except Stopped as error:
                code = str(error)
            finally:
                if assigned:
                    try:
                        job.stop()
                    except OSError:
                        job.close()  # KILL_ON_JOB_CLOSE also covers a failed explicit termination.
                        assigned = False
                        active = None
                        code = 'ISOLATION_CLEANUP_FAILED'
                else:
                    launcher.kill()
                launcher.wait(timeout=5)
                if reader.ident is not None:
                    reader.join(timeout=5)
                deadline = time.monotonic() + 5
                while assigned:
                    total, active = job.counts()
                    if not active or time.monotonic() >= deadline:
                        break
                    time.sleep(0.03)
    if active or reader.is_alive() or code == 'ISOLATION_CLEANUP_FAILED':
        return {'code': 'ISOLATION_CLEANUP_FAILED', 'hostProcesses': active, 'hostProcessesStarted': total}
    if ready is not None and serial.ready.is_set():
        ready.set()
    report = {'code': code or 'ISOLATION_UNAVAILABLE'}
    if code is None and launcher.returncode == 0 and not serial.overflow.is_set():
        try:
            report = serial.report()
        except (ValueError, KeyError, TypeError):
            pass
    return {**report, 'hostProcesses': active, 'hostProcessesStarted': total}


def run_isolated(config: SandboxConfig, profile: str, files: Mapping[str, bytes], *, limits: Limits | None = None,
                 cancel: threading.Event | None = None, ready: threading.Event | None = None,
                 scratch: Path | None = None):
    """Copy approved inputs, run once, and return bounded diagnostics after cleanup."""
    if limits is None:
        limits = Limits(timeout_seconds=120, cpu_seconds=60) if profile == 'preview' else Limits()
    try:
        payload = input_archive(files, profile, limits)
    except (AttributeError, ValueError, TypeError):
        return {'code': 'ISOLATION_INPUT_INVALID'}
    deadline = time.monotonic() + limits.timeout_seconds

    def check_stop():
        if cancel is not None and cancel.is_set():
            raise Stopped('ISOLATION_CANCELLED')
        if time.monotonic() >= deadline:
            raise Stopped('ISOLATION_TIMEOUT')

    if sys.platform != 'win32' or not config.qemu.is_absolute() or not config.qemu.is_file():
        return {'code': 'ISOLATION_UNAVAILABLE'}
    try:
        check_stop()
        with tempfile.TemporaryDirectory(prefix='plugin-isolation-', dir=scratch) as temp:
            directory = Path(temp)
            for name, expected, destination, mib in (
                ('vmlinuz', config.kernel_sha256, 'vmlinuz', 32),
                ('rootfs.cpio.gz', config.rootfs_sha256, 'initrd', 384),
            ):
                with (directory / destination).open('wb') as output:
                    copy_verified_image(config.image / name, expected, mib * 1024**2, check_stop, output)
                    if destination == 'initrd':
                        # Linux requires the uncompressed newc header to start on a four-byte boundary.
                        output.write(b'\0' * (-output.tell() % 4))
                        output.write(payload)
            return run_vm(qemu_command(config.qemu, directory), directory, check_stop, ready)
    except Stopped as error:
        return {'code': str(error), 'hostProcesses': 0, 'hostProcessesStarted': 0}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'code': 'ISOLATION_UNAVAILABLE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('profile', choices=ENTRIES)
    parser.add_argument('--input', action='append', nargs=2, required=True, metavar=('NAME', 'FILE'))
    args = parser.parse_args()
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        config = SandboxConfig.load(args.qemu.resolve(), args.image.resolve())
        files = {}
        remaining = MAX_INPUT
        for name, file in args.input:
            with Path(file).open('rb') as stream:
                data = stream.read(remaining + 1)
            remaining -= len(data)
            if remaining < 0 or name in files:
                raise ValueError('Too many inputs or duplicate path')
            files[name] = data
        report = run_isolated(config, args.profile, files, cancel=cancel)
    except (OSError, ValueError, KeyError, TypeError):
        report = {'code': 'ISOLATION_UNAVAILABLE'}
    print(json.dumps(report))
    return 0 if report['code'] == 'OK' else 2


if __name__ == '__main__':
    raise SystemExit(main())
