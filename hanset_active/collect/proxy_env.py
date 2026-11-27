"""Private proxy environment for scheduled collector launches."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat

from .config import outside


SELECTOR = 'HANSET_COLLX_PROXY_ENV_FILE'
PROXY_NAME = re.compile(r'HANSET_WEB_PROXY(?:_[a-zA-Z0-9_]*)?')


def read_proxy_env(location):
    path = Path(location)
    if not path.is_absolute():
        raise ValueError('proxy_env_file_invalid')
    outside(path)

    # Open each component without following symlinks, and check the opened file.
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(path.anchor, flags)
        stack.callback(os.close, parent)
        for part in path.parent.parts[1:]:
            parent = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('proxy_env_file_invalid')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            settings = json.load(stream)

    if not isinstance(settings, dict):
        raise ValueError('proxy_env_file_invalid')
    for name, value in settings.items():
        if (not PROXY_NAME.fullmatch(name) or not isinstance(value, str)
                or '\0' in value):
            raise ValueError('proxy_env_file_invalid')
        # Validate every value before changing the environment.
        os.fsencode(value)
    return settings


def apply_proxy_env(settings):
    for name in list(os.environ):
        if PROXY_NAME.fullmatch(name):
            del os.environ[name]
    os.environ.update(settings)
    del os.environ[SELECTOR]
