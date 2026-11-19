"""Installed Claude Driver paths (ARC-5)."""

from pathlib import Path


def installation_root(home: Path | str) -> Path:
    return Path(home) / '.arbol'


def state_directory(home: Path | str) -> Path:
    return installation_root(home) / 'drivers/claude'
