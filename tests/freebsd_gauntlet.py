'''Run the purecdp test suite on FreeBSD under QEMU/KVM, WITH a real Chromium —
no root required. This is the full-e2e counterpart to the CI FreeBSD leg (which
skips the browser); here Chrome is installed in the guest so goto/snapshot/
stealth/OOPIF/download tests actually run on FreeBSD.

Modelled on checkdisk's freebsd_gauntlet.py; the same hard-won bits apply:

  * Use the **BASIC-CLOUDINIT** UFS image, not the plain one — it logs to the
    serial console and lets root log in with no password (headless-friendly).
    We drive the serial console with pexpect, not cloud-init.
  * Give the overlay a **20 GB** virtual size — the base UFS is small and nearly
    full; growfs + pkg installs (Chromium is large) need the headroom.
  * Provision once (firstboot growfs + a possible freebsd-update reboot) to a
    quiescent login prompt, power down cleanly, then reuse that overlay.
  * Ship the repo via a **FAT disk built with mtools** (no root/loopback):
    src/ + tests/ + spec/, labelled PURECDP, mounted at /dev/msdosfs/PURECDP.

purecdp-specific vs checkdisk:
  * The guest installs **python314 AND chromium**; Chromium persists in the
    overlay so only the first run pays the download.
  * FreeBSD Chromium needs **procfs** and **fdescfs** mounted, and runs headless
    with --no-sandbox --disable-dev-shm-usage --disable-gpu (set via
    $PURECDP_*_ARGS, which the tests honour). No display is needed.
  * The suite bootstraps sys.path to ./src itself, so we just
    `python3.14 -m unittest discover tests` — nothing to pip install.

Host deps: qemu-system-x86_64, qemu-img, /dev/kvm, mkfs.fat, mtools, xz, curl,
and the `pexpect` Python package.

Usage:
    python freebsd_gauntlet.py            # FreeBSD 15.1 (default)
    python freebsd_gauntlet.py 14.3       # a specific release
    python freebsd_gauntlet.py --fresh    # re-provision from scratch
'''
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get('FREEBSD_GAUNTLET_DIR',
                       os.path.expanduser('~/.cache/freebsd-gauntlet'))
DEFAULT_VERSION = '15.1'
MIRROR = 'https://download.freebsd.org/releases/VM-IMAGES'
PROMPT = 'RDY> '
LABEL = 'PURECDP'
# headless Chromium flags for FreeBSD in a VM (no user namespaces, tiny shm,
# no GPU); the tests read these and pass them to every browser launch.
BROWSER_ARGS = '--no-sandbox --disable-dev-shm-usage --disable-gpu'


def _sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _paths(version):
    base = f'{CACHE}/fbsd-{version}.raw'
    return base, f'{CACHE}/fbsd-{version}-purecdp-overlay.qcow2'


# ── host disks (FAT repo image, mtools, no root) ─────────────────────────────

def build_repo_disk():
    '''Stage src/ + tests/ + spec/ (minus __pycache__) onto a labelled FAT disk.
       spec/ is included so the codegen byte-identity test can run.'''
    disk = f'{CACHE}/purecdp-repo.fat'
    stage = f'{CACHE}/purecdp-stage'
    _sh(f'rm -rf {stage} {disk}')
    os.makedirs(stage)
    for d in ('src', 'tests', 'spec'):
        _sh(f'cp -r {REPO}/{d} {stage}/{d}')
    _sh(f'find {stage} -name __pycache__ -type d -prune -exec rm -rf {{}} +')
    _sh(f'truncate -s 128M {disk}')
    _sh(f'mkfs.fat -n {LABEL} {disk}')
    for d in ('src', 'tests', 'spec'):
        _sh(f'mcopy -s -o -i {disk} {stage}/{d} ::{d}')
    return disk


def fetch_image(version):
    base, _ = _paths(version)
    if os.path.exists(base):
        return base
    os.makedirs(CACHE, exist_ok=True)
    name = f'FreeBSD-{version}-RELEASE-amd64-BASIC-CLOUDINIT-ufs.raw.xz'
    url = f'{MIRROR}/{version}-RELEASE/amd64/Latest/{name}'
    print(f'downloading {name} (~700 MB) …')
    _sh(f'curl -s -o {base}.xz "{url}"')
    print('decompressing …')
    _sh(f'xz -dk {base}.xz')
    return base


# ── QEMU ─────────────────────────────────────────────────────────────────────

def _base_args(overlay, repo_disk):
    return [
        'qemu-system-x86_64', '-enable-kvm', '-machine', 'q35,accel=kvm',
        '-cpu', 'host', '-m', '4096', '-smp', '4',
        '-drive', f'file={overlay},format=qcow2,if=virtio',
        '-drive', f'file={repo_disk},format=raw,if=virtio',
        '-netdev', 'user,id=n0', '-device', 'virtio-net-pci,netdev=n0',
        '-display', 'none',
    ]


def provision(version, repo_disk):
    '''Boot the fresh 20 GB overlay and let firstboot (growfs + a possible
       freebsd-update reboot) settle to a quiescent login prompt, then power
       down cleanly. Idempotent: skipped if the overlay already exists.'''
    base, overlay = _paths(version)
    if os.path.exists(overlay):
        return overlay
    _sh(f'qemu-img create -f qcow2 -F raw -b {base} {overlay} 20G')
    con, mon = f'{CACHE}/prov.log', f'{CACHE}/prov.sock'
    for p in (con, mon):
        if os.path.exists(p):
            os.unlink(p)
    q = subprocess.Popen(_base_args(overlay, repo_disk)
                         + ['-serial', f'file:{con}',
                            '-monitor', f'unix:{mon},server,nowait'])
    print('provisioning: waiting for firstboot to settle …')
    quiet_since, last_len, deadline = None, -1, time.time() + 1200
    while time.time() < deadline and q.poll() is None:
        time.sleep(6)
        log = open(con, errors='replace').read() if os.path.exists(con) else ''
        if 'login:' in log and len(log) == last_len:      # console gone quiet
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= 72:
                break
        else:
            quiet_since = None
        last_len = len(log)
    try:
        s = socket.socket(socket.AF_UNIX)
        s.connect(mon)
        time.sleep(0.5)
        s.recv(65536)
        s.sendall(b'system_powerdown\n')
        time.sleep(2)
        s.close()
        q.wait(120)
    except Exception:
        pass
    finally:
        if q.poll() is None:
            q.terminate()
            try:
                q.wait(20)
            except subprocess.TimeoutExpired:
                q.kill()
    return overlay


def run_suite(version, overlay, repo_disk):
    '''pexpect the serial console: root login (no password), install python314 +
       chromium, mount procfs/fdescfs + the repo, run the full suite with a real
       browser.'''
    import pexpect
    c = pexpect.spawn(' '.join(_base_args(overlay, repo_disk) + ['-serial', 'stdio']),
                      timeout=240, encoding='utf-8', codec_errors='replace')
    c.logfile_read = sys.stdout
    c.expect('login:', timeout=240)
    c.sendline('root')
    c.expect(r'# ', timeout=30)
    c.sendline("export ASSUME_ALWAYS_YES=yes; PS1='%s'" % PROMPT)
    c.expect_exact(PROMPT, timeout=15)

    def run(command, timeout):
        c.sendline(command + ' ; echo RC=$?')
        c.expect(r'RC=(\d+)', timeout=timeout)
        rc = int(c.match.group(1))
        c.expect_exact(PROMPT, timeout=30)
        return rc

    print(f'\n########## FreeBSD {version}: provisioning (python + chromium) ##########')
    run('dhclient vtnet0 2>/dev/null; true', 60)
    run('pkg bootstrap -y 2>&1 | tail -1', 180)
    # chromium is large (X libs even for headless); first run downloads it, then
    # it lives in the overlay. python314 gives us the >=3.14 purecdp requires.
    print(f'[pkg install rc={run("pkg install -y python314 chromium 2>&1 | tail -3", 2400)}]')
    # FreeBSD Chromium needs /proc and /dev/fd; mounts are harmless if present.
    run('mount -t procfs proc /proc 2>/dev/null; true', 20)
    run('mount -t fdescfs fdesc /dev/fd 2>/dev/null; true', 20)
    # FreeBSD only configures 127.0.0.1 on lo0; Linux treats all of 127/8 as
    # loopback. The cross-origin OOPIF tests use 127.0.0.2/.3 as distinct sites,
    # so alias them or those iframes never load (and the tests fail, not skip).
    run('ifconfig lo0 alias 127.0.0.2/32 2>/dev/null; true', 20)
    run('ifconfig lo0 alias 127.0.0.3/32 2>/dev/null; true', 20)
    run(f'mkdir -p /mnt/repo && mount -t msdosfs /dev/msdosfs/{LABEL} /mnt/repo', 30)
    run('echo TOOLS python=$(which python3.14) chrome=$(command -v chrome || command -v chromium)', 20)

    print(f'\n########## purecdp suite (FreeBSD {version}, real Chromium) ##########')
    rc = run(
        'cd /mnt/repo && env PYTHONDONTWRITEBYTECODE=1 '
        'CDP_BROWSER="$(command -v chrome || command -v chromium)" '
        f'PURECDP_E2E_ARGS="{BROWSER_ARGS}" PURECDP_LAUNCH_ARGS="{BROWSER_ARGS}" '
        'python3.14 -X faulthandler -m unittest discover tests 2>&1', 2400)

    print(f'\n########## VERDICT (FreeBSD {version}) ##########')
    print(f'  suite: exit {rc} ({"PASS" if rc == 0 else "FAIL"})')
    c.sendline('poweroff')
    try:
        c.expect(pexpect.EOF, timeout=90)
    except pexpect.TIMEOUT:
        c.terminate(force=True)
    return rc == 0


def check_deps():
    missing = [t for t in ('qemu-system-x86_64', 'qemu-img', 'mkfs.fat',
                           'mcopy', 'xz', 'curl') if not shutil.which(t)]
    if not os.access('/dev/kvm', os.W_OK):
        missing.append('/dev/kvm (writable)')
    try:
        import pexpect  # noqa: F401
    except ImportError:
        missing.append('python-pexpect')
    return missing


def main(argv):
    version = DEFAULT_VERSION
    fresh = '--fresh' in argv
    for a in argv:
        if not a.startswith('-'):
            version = a
    missing = check_deps()
    if missing:
        print('missing host dependencies:', ', '.join(missing))
        print('  install e.g.: sudo pacman -S qemu-full mtools dosfstools; '
              'pip install pexpect')
        return 2
    os.makedirs(CACHE, exist_ok=True)
    _, overlay = _paths(version)
    if fresh and os.path.exists(overlay):
        os.unlink(overlay)
    repo_disk = build_repo_disk()
    fetch_image(version)
    provision(version, repo_disk)
    return 0 if run_suite(version, overlay, repo_disk) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
