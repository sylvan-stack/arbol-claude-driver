# Test implementations moved to tests/mid/archived/src/arbol_claude_driver/dev.py.
"""Packaged opt-in checks. Probes never write a user record."""
from __future__ import annotations

import asyncio
from typing import Any

from .core import Env, Failure, Record, now
from .subscriptions import Subscriptions

ENUMERATED = {'control_response', 'result', 'success', 'error', 'error_during_execution', 'none', 'claude.ai', 'firstParty', 'bypassPermissions', 'connected', 'failed', 'pending', 'needs-auth', 'low', 'medium', 'high', 'xhigh', 'max'}
BASE = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'bypassPermissions', '--system-prompt-snapshot', 'off']


def probe_shape(value: Any) -> Any:
    if isinstance(value, list):
        return [probe_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: item if key in {'type', 'subtype', 'authMethod', 'apiProvider', 'current_permission_mode', 'status', 'effort'} and isinstance(item, str) and item in ENUMERATED else probe_shape(item) for key, item in value.items()}
    return None if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in {int, float} else 'string' if isinstance(value, str) else 'object'


def verify(condition: Any, message: str) -> None:
    if not condition:
        raise Failure('tool_unsupported', message)


async def probe(subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> Record:
    return await _mid_tier_namespace()['probe'](subscriptions, inherited, cancel)


async def check_account(subscriptions: Subscriptions) -> Record:
    account = await subscriptions.require_signed_in('test-anthropic')
    if account['family'] != 'anthropic':
        raise Failure('invalid_input', 'test-anthropic must belong to the anthropic family.')
    usage = account.get('lastUsage', {'outcome': 'unsupported', 'fetchedAt': now(), 'windows': []})
    if any(window.get('limitReached') or window.get('usedPercent', 0) >= 100 for window in usage['windows']):
        raise Failure('rate_limited', 'The test Subscription has a usage window used up. Wait before running the check.')
    return dict(usage)


def usage_change(before: Record, after: Record) -> list[Record]:
    changes = []
    for window in after['windows']:
        previous: Record = next((item for item in before['windows'] if item['kind'] == window['kind'] and item['label'] == window['label']), {})
        changes.append({'kind': window['kind'], 'label': window['label'], **({'beforePercent': previous['usedPercent']} if 'usedPercent' in previous else {}), **({'afterPercent': window['usedPercent']} if 'usedPercent' in window else {}), **({'changePercent': round(window['usedPercent'] - previous['usedPercent'], 3)} if 'usedPercent' in previous and 'usedPercent' in window else {})})
    return changes


async def live_check(build_only: bool, subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> tuple[int, Record]:
    return await _mid_tier_namespace()['live_check'](build_only, subscriptions, inherited, cancel)

# Archived complete imports; restore needed names in their original scope, not at this file tail.


def _mid_tier_namespace():
    """Load test-only code on explicit invocation, leaving normal product imports alone."""
    from pathlib import Path as _TierPath
    from types import ModuleType as _TierModule
    import sys as _tier_sys
    name = __name__ + '.__mid_tier'
    if name not in _tier_sys.modules:
        path = _TierPath(__file__).resolve().parents[2] / 'tests/mid/archived/src/arbol_claude_driver/dev.py'
        if not path.is_file():
            raise RuntimeError('Mid-tier source is unavailable in this installation; use the source checkout or an explicitly qualified test package. See MID_TIER.md.')
        module = _TierModule(name)
        # Preserve public case identities consumed by qualification contracts.
        module.__dict__['__name__'] = __name__
        module.__file__ = __file__
        module.__package__ = __package__
        _tier_sys.modules[name] = module
        try:
            exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
        except BaseException:
            del _tier_sys.modules[name]
            raise
    return _tier_sys.modules[name].__dict__
