"""Trusted guest PID 1 supervisor. Loaded from the coordinator's checkout."""

import ctypes
import json
import os
import subprocess
import time
from pathlib import Path


CGROUP = Path('/sys/fs/cgroup')
WORKER = CGROUP / 'worker'
COMMANDS = {
    'build': ['/bin/sh', '-eu', '/input/build.sh'],
    'test': ['/bin/sh', '-eu', '/input/test.sh'],
    'preview': ['/usr/bin/chromium-browser', '--headless', '--no-sandbox', '--disable-gpu',
                '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update',
                '--disable-extensions', '--no-first-run', '--no-zygote', '--renderer-process-limit=1',
                '--log-level=3', '--user-data-dir=/work/browser', '--dump-dom', 'file:///input/index.html'],
}


def command(*args):
    subprocess.run(('/bin/busybox', *args), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare(limits):
    command('mount', '-t', 'proc', 'proc', '/proc')
    command('mount', '-t', 'sysfs', 'sysfs', '/sys')
    if os.listdir('/sys/class/net') != ['lo']:
        raise RuntimeError('Unexpected network interface')
    command('ip', 'link', 'set', 'lo', 'up')
    command('mount', '-t', 'cgroup2', 'cgroup2', str(CGROUP))
    (CGROUP / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
    WORKER.mkdir()
    for name, value in {
        'cpu.max': '75000 100000', 'pids.max': limits['processes'],
        'memory.max': limits['memoryMiB'] * 1024**2, 'memory.swap.max': 0,
        'memory.oom.group': 1,
    }.items():
        setting = WORKER / name
        setting.write_text(str(value))
        if setting.read_text().strip() != str(value):
            raise RuntimeError(f'Cgroup limit did not take effect: {name}')
    if not (WORKER / 'cgroup.kill').exists():
        raise RuntimeError('Cgroup termination is unavailable')

    command('mount', '--bind', '/', '/sandbox')
    command('mount', '-o', 'remount,bind,ro,nosuid,nodev', '/sandbox')
    command('mount', '--bind', '/input', '/sandbox/input')
    command('mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec', '/sandbox/input')
    command('mount', '-t', 'proc', '-o', 'ro,nosuid,nodev,noexec,hidepid=2', 'proc', '/sandbox/proc')
    command('mount', '-t', 'tmpfs', '-o', 'size=64k,nr_inodes=32,nosuid,noexec', 'tmpfs', '/sandbox/dev')
    for name, minor in {'null': 3, 'zero': 5, 'random': 8, 'urandom': 9}.items():
        path = '/sandbox/dev/' + name
        os.mknod(path, 0o20666, os.makedev(1, minor))
        os.chmod(path, 0o666)
    os.symlink('/work/.tmp', '/sandbox/dev/shm')
    command('mount', '-t', 'tmpfs', '-o',
            f"size={limits['diskMiB']}m,nr_inodes=4096,nosuid,nodev,mode=0700", 'tmpfs', '/scratch')
    Path('/scratch/data/.tmp').mkdir(parents=True)
    for path in ('/scratch/data', '/scratch/data/.tmp'):
        os.chown(path, 65534, 65534)
        os.chmod(path, 0o700)
    command('mount', '--bind', '/scratch/data', '/sandbox/work')
    if not all(os.statvfs(path).f_flag & os.ST_RDONLY for path in ('/sandbox', '/sandbox/input')):
        raise RuntimeError('Guest image or input is writable')


def drop_privileges():
    (WORKER / 'cgroup.procs').write_text(str(os.getpid()))
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS')
    os.chroot('/sandbox')
    os.chdir('/work')
    os.umask(0o077)
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)


def counter(file, key):
    return int(dict(line.split() for line in (WORKER / file).read_text().splitlines())[key])


def resource_error(limits):
    if counter('memory.events', 'oom'):
        return 'ISOLATION_MEMORY_LIMIT'
    if counter('pids.events', 'max'):
        return 'ISOLATION_PROCESS_LIMIT'
    if counter('cpu.stat', 'usage_usec') >= limits['cpuSeconds'] * 1_000_000:
        return 'ISOLATION_CPU_LIMIT'
    disk = os.statvfs('/scratch')
    if disk.f_bavail == 0 or disk.f_favail == 0:
        return 'ISOLATION_DISK_LIMIT'
    return None


def reap_children():
    # PID 1 adopts orphaned Chromium processes and any detached workers.
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return


def run(profile, limits):
    prepare(limits)
    with open('/scratch/capture', 'w+b') as capture:
        worker = subprocess.Popen(COMMANDS[profile], stdin=subprocess.DEVNULL, stdout=capture, stderr=capture,
                                  env={'PATH': '/usr/bin:/bin', 'HOME': '/work', 'TMPDIR': '/work/.tmp',
                                       'LANG': 'C.UTF-8', 'TZ': 'UTC'}, preexec_fn=drop_privileges)
        try:
            print('C4_READY', flush=True)
            code = resource_error(limits)
            while code is None and worker.poll() is None:
                time.sleep(0.05)
                code = resource_error(limits)
        finally:
            (WORKER / 'cgroup.kill').write_text('1')
            worker.wait(timeout=3)
            deadline = time.monotonic() + 3
            while counter('cgroup.events', 'populated'):
                reap_children()
                if time.monotonic() >= deadline:
                    raise RuntimeError('Cgroup remained populated')
                time.sleep(0.05)
            reap_children()
        capture.seek(0)
        output = capture.read(8193)
    return {'code': code or ('OK' if worker.returncode == 0 else 'ISOLATION_WORKLOAD_FAILED'),
            'exitCode': worker.returncode, 'output': output[:8192].decode('utf-8', errors='replace'),
            'truncated': len(output) > 8192, 'guestProcesses': 0}


def main():
    try:
        run_config = json.loads(Path('/c4-config.json').read_text())
        report = run(run_config['profile'], run_config['limits'])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        report = {'code': 'ISOLATION_UNAVAILABLE', 'detail': str(error)[:256]}
    print('C4_RESULT ' + json.dumps(report), flush=True)
    os.sync()
    command('poweroff', '-f')


if __name__ == '__main__':
    main()
