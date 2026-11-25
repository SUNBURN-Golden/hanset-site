"""Lock and artifact confirmation actions for the external deploy entry point."""

import fcntl
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'site'))

import release_manifest as rm

CONTRACT = 'hanset-release-v1'


def receipt_binding(release_id, manifest):
    return {'schema_version': 1, 'release_id': release_id,
            'manifest_sha256': rm.digest(rm.canonical(manifest))}


def verify_lock():
    path = Path(os.environ['HANSET_PUBLISH_LOCK_FILE'])
    fd = int(os.environ['HANSET_PUBLISH_LOCK_FD'])
    inherited = os.fstat(fd)
    if not stat.S_ISREG(inherited.st_mode) or not os.path.samestat(inherited, path.stat(follow_symlinks=False)):
        raise ValueError
    # A separate file description must contend, while the inherited
    # description must already own the exclusive flock.
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise ValueError
    finally:
        os.close(probe)


def operations(action, argument=None):
    verify_lock()
    if action == 'check-lock' and argument is None:
        return
    expected = rm.read_manifest(Path(os.environ['HANSET_PUBLISH_EXPECTED']))
    release_id = os.environ['HANSET_PUBLISH_RELEASE_ID']
    if not rm.HEX.fullmatch(release_id):
        raise ValueError
    receipt = Path(os.environ['HANSET_PUBLISH_RECEIPT'])
    marker = Path(os.environ['HANSET_PUBLISH_UPLOAD_STARTED'])
    binding = receipt_binding(release_id, expected)
    started = {**binding, 'outcome': 'upload_started'}
    if action == 'started' and argument and not marker.exists() and not receipt.exists():
        from release_artifact import seal

        dist = Path(argument)
        if dist != ROOT / 'site/dist':
            raise ValueError
        seal(dist, expected['applied_data_sha256'], expected=expected)
        rm.atomic_json(marker, started)
    elif (action == 'confirmed' and argument is None and not receipt.exists()
          and rm.read_json(marker) == started):
        rm.atomic_json(receipt, {**binding, 'outcome': 'confirmed'})
    else:
        raise ValueError


def main():
    os.umask(0o077)
    try:
        if len(sys.argv) not in (2, 3):
            return 2
        operations(*sys.argv[1:])
    except (rm.GateError, OSError, ValueError, KeyError, TypeError):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
