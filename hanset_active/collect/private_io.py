"""Private durable filesystem records for the collector."""
import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager


class Busy(RuntimeError):
    pass


def directory(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('unsafe_path')
    if not path.is_dir():
        directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_directory(path.parent)
    return path


@contextmanager
def lock(path, *, blocking=False):
    directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy('already_running') from None
        yield fd
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('record_not_regular')
        return stream.read()


def replace(path, data):
    directory(path.parent)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def read_json(path):
    return json.loads(read(path))
