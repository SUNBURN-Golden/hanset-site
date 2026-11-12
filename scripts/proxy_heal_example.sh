#!/bin/bash
# Optional Linux example: bash scripts/proxy_heal_example.sh PORT
# Requires Python 3 with Linux pidfd support. Configure the collector's optional
# HANSET_WEB_PROXY_HEAL_CMD externally to invoke this script with the SOCKS port.
# The existing external keep-alive loop must reconnect after its ssh child exits.
# This script only sends TERM to one matching current-user ssh process. It never
# signals the keep-alive parent, uses broad process matching, or sends KILL.
# Exit codes: 0 TERM sent; 2 invalid port; 3 no match; 4 ambiguous matches;
# 5 metadata/identity/permission/signal failure; 6 pidfd API unavailable.
# No command lines or process metadata are printed, including on failure.
exec python3 - "$@" <<'PY'
import os
from pathlib import Path
import re
import signal
import sys


def parse_port(args):
    if len(args) != 1 or not re.fullmatch(r'[0-9]+', args[0]):
        return None
    digits = args[0].lstrip('0')
    if not digits or len(digits) > 5:
        return None
    port = int(digits)
    return port if 1 <= port <= 65535 else None


def matches_forward(argv, port):
    """Parse ssh options, ignoring values and stopping before positional args."""
    target = f'127.0.0.1:{port}'.encode()
    no_command = dynamic = False
    # OpenSSH short options that consume a value (possibly attached).
    takes_value = b'BbcDEeFIiJLlmOopQRSWw'
    flags = b'46AaCfGgKkMNnqsTtVvXxYy'
    index = 1
    while index < len(argv):
        arg = argv[index]
        if not arg.startswith(b'-') or arg in (b'-', b'--'):
            break
        offset = 1
        while offset < len(arg):
            option = arg[offset]
            if option in takes_value:
                value = arg[offset + 1:]
                if not value:
                    index += 1
                    if index >= len(argv):
                        return False
                    value = argv[index]
                if option == ord('D') and value == target:
                    dynamic = True
                break
            if option not in flags:
                return False
            if option == ord('N'):
                no_command = True
            offset += 1
        index += 1
    return no_command and dynamic


def read_process(pid, uid, proc_root):
    """Return a stable identity and argv for an actual same-user ssh process."""
    process = proc_root / str(pid)
    try:
        if process.stat().st_uid != uid:
            return None
        uids = next(line.split()[1:] for line in
                    (process / 'status').read_text().splitlines() if line.startswith('Uid:'))
        if len(uids) != 4 or any(int(value) != uid for value in uids):
            return None
        before = (process / 'stat').read_bytes()
        executable = process / 'exe'
        if Path(os.readlink(executable)).name != 'ssh':
            return None
        identity = executable.stat()
        argv = tuple((process / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0'))
        after = (process / 'stat').read_bytes()
        # /proc/stat comm can itself contain spaces or parentheses.
        start = before.rsplit(b')', 1)[1].split()[19]
        if start != after.rsplit(b')', 1)[1].split()[19] or process.stat().st_uid != uid:
            return None
        return start, identity.st_dev, identity.st_ino, argv
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def heal(port, proc_root=Path('/proc')):
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        return 6
    uid = os.getuid()
    if os.geteuid() != uid:
        return 5
    try:
        candidates = []
        for entry in proc_root.iterdir():
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            identity = read_process(pid, uid, proc_root)
            if identity is not None and matches_forward(identity[-1], port):
                candidates.append((pid, identity))
        if not candidates:
            return 3
        if len(candidates) != 1:
            return 4
        pid, identity = candidates[0]
        # Pin the process before rechecking ownership, executable, start ticks
        # and arguments. A recycled numeric PID can never receive our signal.
        descriptor = os.pidfd_open(pid, 0)
        try:
            if read_process(pid, uid, proc_root) != identity:
                return 5
            signal.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
        finally:
            os.close(descriptor)
        return 0
    except (OSError, ValueError):
        return 5


def main(args):
    port = parse_port(args)
    return 2 if port is None else heal(port)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
PY
