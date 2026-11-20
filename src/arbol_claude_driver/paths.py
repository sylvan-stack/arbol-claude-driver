"""Installed Claude Driver paths for the existing format-1 release."""

import os
import stat
from pathlib import Path

from .core import Failure


def installation_root(home: str | Path) -> Path:
    return Path(home) / '.arbol'


def state_directory(home: str | Path) -> Path:
    return installation_root(home) / 'drivers/claude'


def require_existing_state(home: str, directory: Path) -> None:
    """Only an already initialized installation can use this compatibility Build."""
    try:
        for path in (installation_root(home).absolute(), directory.parent, directory):
            info = path.lstat()
            if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()):
                raise ValueError
            if path != directory.parent and stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError
        fd = os.open(directory / 'format', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16
                    or os.read(fd, 17).strip() != b'1'):
                raise ValueError
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise Failure('invalid_input', 'This Build requires existing private Claude state in format 1.') from None
