"""Isolated immutable normal entry; refuse before CLI/settings/state imports."""
from __future__ import annotations

# sys and os are built-in/frozen interpreter support. Do not move a stdlib or
# application import above the initial runtime-root validation.
import sys
import os


def _refuse() -> None:
    sys.stdout.write('{"error":{"code":"internal","message":"Driver admission is unavailable; use the qualified registered launcher.","retryable":false}}\n')
    sys.stdout.flush()
    raise SystemExit(1)


def _inside(path: str, root: str) -> bool:
    return bool(path and os.path.isabs(path) and os.path.commonpath((os.path.realpath(path), root)) == root)


def bootstrap() -> None:
    script = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(script))
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or not sys.flags.ignore_environment or not sys.flags.safe_path
            or os.path.realpath(root) != root or os.path.basename(root) != 'driver-payload'
            or os.path.realpath(script) != script
            or any(not _inside(path, root) for path in (sys.executable, sys.prefix, sys.base_prefix,
                                                       sys.exec_prefix, sys.base_exec_prefix, *sys.path))):
        _refuse()
    if os.__spec__ is None or os.__spec__.origin != 'frozen':
        _refuse()

    # Initial imports now resolve only into the bundled runtime. D's exact
    # roots replace that startup list before low-level application support.
    import json
    from pathlib import Path
    from typing import Any

    try:
        with open(os.path.join(root, 'driver-capability.json'), 'rb') as stream:
            raw = stream.read(4097)
        if not raw or len(raw) > 4096:
            _refuse()
        declaration: Any = json.loads(raw.decode('utf-8', 'strict'))
        if not isinstance(declaration, dict) or not isinstance(declaration.get('importRoots'), list):
            _refuse()
        roots: list[str] = []
        for name in declaration['importRoots']:
            if (not isinstance(name, str) or not name or '\\' in name
                    or any(part in ('', '.', '..') for part in name.split('/')) or os.path.isabs(name)):
                _refuse()
            candidate = os.path.join(root, name)
            if not _inside(candidate, root) or not (os.path.isdir(candidate) or name.endswith('.zip') and os.path.isfile(candidate)):
                _refuse()
            roots.append(candidate)
        if not roots:
            _refuse()
        sys.path[:] = roots
        from arbol_claude_driver.identity_protocol import load_payload, refuse
        from arbol_claude_driver.admission import Admission, AUTHORITY_ENV, MAINTENANCE_CONTEXT, MAINTENANCE_FD
        payload = load_payload(Path(root), 'arbol-claude-driver', str(declaration.get('build', '')))
        entry = os.path.relpath(script, root).replace(os.sep, '/')
        if (payload.descriptor['entry'] != entry
                or sys.executable != str(payload.root / payload.descriptor['interpreter'])
                or Path(sys.executable).resolve(strict=True) != payload.root / payload.descriptor['interpreter']
                or sys.path != [str(payload.root / name) for name in payload.descriptor['importRoots']]):
            refuse()
        # The administrative consumer is introduced in leaf10. Until then the
        # reserved protocol refuses here, before importing the ordinary CLI.
        if any(key in os.environ for key in (MAINTENANCE_FD, MAINTENANCE_CONTEXT)):
            refuse()
        admission = Admission.receive(payload, entry, dict(os.environ))
        if admission.context['role'] != 'main':
            refuse()
        # Keep the caller's vendor environment, remove only admission authority.
        # The admitted object, never a fresh environment lookup, owns cleanup.
        for key in AUTHORITY_ENV:
            os.environ.pop(key, None)
    except (Exception, KeyboardInterrupt):
        _refuse()
    try:
        from arbol_claude_driver.cli import entry as command_entry
        command_entry(admission)
    finally:
        try:
            admission.finish()
        except Exception:
            # A final output may already exist. Leave unknown custody durable
            # and fail the process without emitting a second stdout document.
            sys.stderr.write('Driver custody could not be settled; admission remains unavailable.\n')
            raise SystemExit(1) from None


if __name__ == '__main__':
    bootstrap()
