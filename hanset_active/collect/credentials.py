"""Runtime-only Qwen Token Plan credential. Never logged, persisted, or committed."""
import functools
import os
import stat
from pathlib import Path

from hanset_active.config import REPO_ROOT

DEFAULT_QWEN_KEY_FILE = '/home/box/.qwen/token_plan_key'
MAX_KEY_BYTES = 4096
MAX_KEY_LEN = 300
MIN_KEY_LEN = 8


def key_file():
    return os.environ.get('HANSET_QWEN_KEY_FILE', '').strip() or DEFAULT_QWEN_KEY_FILE


@functools.lru_cache(maxsize=8)
def _read_key(path):
    try:
        raw = Path(path)
        # Refuse symlinks at the file and at every parent, like plan._safe_path.
        if raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents):
            return ''
        resolved = raw.resolve()
        # Defense in depth: never read a key that lives inside the checkout.
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            return ''
        with os.fdopen(os.open(raw, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_KEY_BYTES:
                return ''
            blob = handle.read(MAX_KEY_BYTES + 1)
    except (OSError, RuntimeError, ValueError):
        return ''
    if len(blob) > MAX_KEY_BYTES:
        return ''
    try:
        key = blob.decode('utf-8').strip()
    except UnicodeError:
        return ''
    if not MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN or '\n' in key or '\x00' in key:
        return ''
    return key


def qwen_key():
    return _read_key(key_file())
