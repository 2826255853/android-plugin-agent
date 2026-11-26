"""Verify isolation on the target Windows machine using fixed, synthetic inputs."""

import argparse
import concurrent.futures
import dataclasses
import os
import shlex
import tempfile
import threading
import time
import unittest
from pathlib import Path

from execution_isolation import Limits, SandboxConfig, run_isolated


class IsolationAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = self.temp / 'runs'
        self.scratch.mkdir()

    def run_case(self, script, *, code='OK', profile='test', extra=None, **kwargs):
        entry = {'test': 'test.sh', 'build': 'build.sh', 'preview': 'index.html'}[profile]
        report = run_isolated(self.config, profile, {entry: script.encode(), **(extra or {})},
                              scratch=self.scratch, **kwargs)
        self.assertEqual(report['code'], code, report)
        self.assertGreaterEqual(report['hostProcessesStarted'], 2, report)
        self.assertEqual(report['hostProcesses'], 0, report)
        self.assertEqual(list(self.scratch.iterdir()), [])
        if code not in {'ISOLATION_TIMEOUT', 'ISOLATION_CANCELLED'}:
            self.assertEqual(report['guestProcesses'], 0, report)
        return report

    def test_build_io_and_cross_run_isolation(self):
        sentinel = self.temp / 'synthetic-host-credential'
        sentinel.write_text('FAKE_C4_HOST_VALUE', encoding='utf-8')
        drive, relative = sentinel.as_posix().split(':/', 1)
        host_path = shlex.quote(f'/mnt/{drive.lower()}/{relative}')
        credential = 'C4_SYNTHETIC_HOST_CREDENTIAL'
        os.environ[credential] = 'FAKE_C4_HOST_VALUE'
        self.addCleanup(os.environ.pop, credential)
        self.run_case(f'''
test "$(cat /input/assets/source.txt)" = approved
test "$(id -u)" = 65534
test -z "${{{credential}:-}}"
test ! -e {host_path}
test ! -e /dev/kvm
test ! -e /dev/sda
test ! -e /dev/vda
test ! -e /dev/ttyS0
test ! -e /proc/1/root
if (echo changed > /input/assets/source.txt) 2>/dev/null; then exit 7; fi
if (mkdir /etc/c4-write) 2>/dev/null; then exit 8; fi
echo private > /work/another-task.txt
test "$(cat /work/another-task.txt)" = private
node -e 'process.exit(0)'
''', profile='build', extra={'assets/source.txt': b'approved'})
        self.run_case('test ! -e /work/another-task.txt\ntest ! -e /input/assets/source.txt\n')
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'FAKE_C4_HOST_VALUE')

    def test_network_blocks_external_routes_and_preserves_guest_loopback(self):
        self.run_case('''python3 - <<'PY'
import errno, socket
assert socket.if_nameindex() == [(1, 'lo')]
for address in [('192.0.2.10', 443), ('10.0.2.2', 443), ('2001:db8::1', 443)]:
    try:
        socket.create_connection(address, timeout=1)
    except OSError as error:
        assert error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH), repr(error)
    else:
        raise AssertionError('Unexpected external route')
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
    with socket.create_connection(server.getsockname(), timeout=1) as client:
        conn, _ = server.accept()
        with conn:
            client.sendall(b'guest-only')
            assert conn.recv(80) == b'guest-only'
PY
''')

    def test_preview_runs_javascript_in_fresh_browser(self):
        report = self.run_case('''<!doctype html><p id="output">initial</p>
<script src="assets/page.js"></script>''', profile='preview', extra={'assets/page.js': b"""
document.querySelector('#output').textContent = 'fixture rendered ' + (6 * 7);
"""})
        self.assertIn('<p id="output">fixture rendered 42</p>', report['output'])

    def test_disk_limit(self):
        self.run_case('dd if=/dev/zero of=/work/full bs=1M count=32', code='ISOLATION_DISK_LIMIT',
                      limits=Limits(disk_mib=8))

    def test_memory_limit(self):
        self.run_case('python3 -c "a = bytearray(192 * 1024**2)"', code='ISOLATION_MEMORY_LIMIT',
                      limits=Limits(memory_mib=64))

    def test_process_limit(self):
        self.run_case('''python3 - <<'PY'
import os, time
for i in range(64):
    if os.fork() == 0:
        time.sleep(20)
        os._exit(0)
PY''', code='ISOLATION_PROCESS_LIMIT', limits=Limits(processes=8))

    def test_cpu_limit(self):
        self.run_case('while true; do :; done', code='ISOLATION_CPU_LIMIT', limits=Limits(cpu_seconds=1))

    def test_timeout_kills_descendants(self):
        ready = threading.Event()
        self.run_case('sleep 180 & wait', code='ISOLATION_TIMEOUT', limits=Limits(timeout_seconds=35), ready=ready)
        self.assertTrue(ready.is_set(), 'Timeout must occur after workload starts')

    def test_cancellation_kills_descendants(self):
        ready, cancel = threading.Event(), threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(self.run_case, 'sleep 180 & wait', code='ISOLATION_CANCELLED',
                               cancel=cancel, ready=ready)
            try:
                self.assertTrue(ready.wait(35), 'Guest workload did not start')
                time.sleep(1)
            finally:
                cancel.set()
            task.result(timeout=10)

    def test_fail_closed_before_execution(self):
        for config in (dataclasses.replace(self.config, qemu=self.temp / 'absent.exe'),
                       dataclasses.replace(self.config, rootfs_sha256='0' * 64)):
            report = run_isolated(config, 'test', {'test.sh': b'exit 0'}, scratch=self.scratch)
            self.assertEqual(report, {'code': 'ISOLATION_UNAVAILABLE'})
            self.assertEqual(list(self.scratch.iterdir()), [])
        report = run_isolated(self.config, 'test', {'test.sh': b'exit 0', '../escape': b'bad'})
        self.assertEqual(report, {'code': 'ISOLATION_INPUT_INVALID'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    args, unittest_args = parser.parse_known_args()
    IsolationAcceptance.config = SandboxConfig.load(args.qemu.resolve(), args.image.resolve())
    unittest.main(argv=[__file__, *unittest_args], verbosity=2)
