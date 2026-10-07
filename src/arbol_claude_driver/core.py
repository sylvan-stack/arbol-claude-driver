"""Command frames, runtime validation and contract errors."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard, cast

from .models import DriverError, ErrorCode

Record = dict[str, Any]
Env = dict[str, str]
ERROR_CODES = frozenset('invalid_input not_found in_use unsupported not_signed_in credential_rejected rate_limited context_exceeded vendor_unavailable vendor_error tool_missing tool_unsupported tool_failed limit_reached state_newer internal'.split())


def now(timestamp: float | None = None) -> str:
    date = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return date.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def valid_date(value: Any) -> bool:
    try:
        return isinstance(value, str) and math.isfinite(epoch(value))
    except (ValueError, OverflowError):
        return False


def loads(text: str | bytes) -> Any:
    def invalid(value: str) -> Any:
        raise ValueError('Invalid JSON constant: ' + value)
    return json.loads(text, parse_constant=invalid)


def compact(value: Any) -> str:
    """ECMAScript JSON.stringify bytes for contract hashes and native history."""
    def string(text: str) -> str:
        encoded = json.dumps(text, ensure_ascii=False)
        result: list[str] = []
        i = 0
        while i < len(encoded):
            code = ord(encoded[i])
            if 0xD800 <= code <= 0xDBFF and i + 1 < len(encoded) and 0xDC00 <= ord(encoded[i + 1]) <= 0xDFFF:
                result.append(chr(0x10000 + ((code - 0xD800) << 10) + ord(encoded[i + 1]) - 0xDC00))
                i += 2
                continue
            result.append(f'\\u{code:04x}' if 0xD800 <= code <= 0xDFFF else encoded[i])
            i += 1
        return ''.join(result)

    if value is None:
        return 'null'
    if type(value) is bool:
        return 'true' if value else 'false'
    if isinstance(value, str):
        return string(value)
    if type(value) in (int, float):
        numeric = float(value)
        if not math.isfinite(numeric):
            return 'null'
        if numeric == 0:
            return '0'
        if type(value) is int and abs(value) <= 9007199254740991:
            return str(value)
        representation = repr(numeric)
        if 1e-6 <= abs(numeric) < 1e21:
            from decimal import Decimal
            return format(Decimal(representation), 'f').rstrip('0').rstrip('.') if '.' in format(Decimal(representation), 'f') else format(Decimal(representation), 'f')
        mantissa, separator, exponent = representation.partition('e')
        mantissa = mantissa.removesuffix('.0')
        if separator:
            power = int(exponent)
            return mantissa + 'e' + ('+' if power >= 0 else '-') + str(abs(power))
        return mantissa
    if isinstance(value, list):
        return '[' + ','.join(compact(item) for item in value) + ']'
    if isinstance(value, dict):
        indices = sorted((key for key in value if re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) < 4294967295), key=int)
        keys = indices + [key for key in value if key not in indices]
        return '{' + ','.join(string(key) + ':' + compact(value[key]) for key in keys) + '}'
    raise TypeError('Not a JSON value')


def utf8(text: str) -> bytes:
    return text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace').encode('utf-8')


def safe_text(text: str, secrets: list[str] | tuple[str, ...] = (), limit: int | None = 1024) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[redacted]')
    data = utf8(text)
    if limit is None or len(data) <= limit:
        return text
    return data[-limit:].decode('utf-8', 'ignore')


def redact(value: Any, secrets: list[str] | tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return safe_text(value, secrets, None)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {safe_text(key, secrets, None): redact(item, secrets) for key, item in value.items()}
    return value


class Failure(Exception):
    result: DriverError

    def __init__(self, code: str, message: str, extra: Record | None = None, secrets: list[str] | tuple[str, ...] = ()):
        if code not in ERROR_CODES:
            raise ValueError('Unknown Driver error code')
        super().__init__(safe_text(message, secrets))
        self.result = cast(DriverError, {**(extra or {}), 'code': cast(ErrorCode, code), 'message': str(self), 'retryable': code in {'rate_limited', 'vendor_unavailable'}})
        if 'excerpt' in self.result.get('detail', {}):
            self.result['detail']['excerpt'] = safe_text(self.result['detail']['excerpt'], secrets)
        if code != 'rate_limited':
            self.result.pop('retryAfterSeconds', None)


def safe_failure(error: BaseException | None) -> Record:
    return dict(error.result if isinstance(error, Failure) else Failure('internal', 'The Driver could not complete this operation.').result)


def number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def fixed_wait(milliseconds: float, env: Env | None = None) -> float:
    source = os.environ if env is None else env
    if 'ARBOL_TEST_TIME_SCALE' not in source:
        return milliseconds / 1000
    try:
        scale = float(source['ARBOL_TEST_TIME_SCALE'])
    except ValueError:
        scale = 0
    if not math.isfinite(scale) or scale <= 0:
        raise Failure('invalid_input', 'ARBOL_TEST_TIME_SCALE must be a positive finite number.')
    return milliseconds / scale / 1000


def clean_environment(source: Env, home: str, config_home: str | None = None) -> Env:
    inherited = 'PATH USER LOGNAME LANG LC_ALL LC_CTYPE LC_MESSAGES TMPDIR TMP TEMP HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS'.split()
    env = {key: source[key] for key in inherited if key in source}
    env['HOME'] = home
    if config_home is not None:
        env['CLAUDE_CONFIG_DIR'] = config_home
    return env


@dataclass
class Command:
    name: str
    positionals: list[str]
    options: dict[str, str | bool | list[str]]


SCHEMAS: dict[str, tuple[list[str], list[str], list[str], int, int]] = {
    'info': ([], [], [], 0, 0),
    'run': ('request subscription model effort service-tier system-prompt-file cwd tools chat-session run-id output'.split(), ['control'], ['add-dir', 'label'], 0, 1),
    'runs list': ([], [], [], 0, 0), 'runs stop': ([], [], [], 1, 1),
    'subscriptions families': ([], [], [], 0, 0), 'subscriptions list': ([], [], [], 0, 0),
    'subscriptions create': (['family', 'label', 'id'], [], [], 0, 0),
    'subscriptions update': (['label'], [], [], 1, 1),
    'subscriptions remove': ([], ['force'], [], 1, 1),
    'subscriptions sign-in': ([], ['by-code'], [], 1, 1),
    'subscriptions sign-out': ([], ['force'], [], 1, 1),
    'subscriptions check': ([], [], [], 1, 1), 'subscriptions usage': ([], [], [], 1, 1),
    'subscriptions models': ([], [], [], 1, 1), 'subscriptions import': (['from'], [], [], 1, 1),
    'dev probe': ([], [], [], 0, 0), 'dev live-check': ([], [], [], 0, 0), 'dev build-check': ([], [], [], 0, 0),
}


def parse_arguments(args: list[str]) -> Command:
    options: dict[str, str | bool | list[str]] = {}
    words: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            words += args[i:]
            break
        if arg == '--state-dir' or arg.startswith('--state-dir='):
            if arg == '--state-dir':
                i += 1
                value = args[i] if i < len(args) else ''
            else:
                value = arg[len('--state-dir='):]
            if not value or value.startswith('--') or 'state-dir' in options:
                raise Failure('invalid_input', 'Give --state-dir exactly one path.')
            options['state-dir'] = value
        else:
            words.append(arg)
        i += 1
    if not words:
        raise Failure('invalid_input', 'Name a command, such as info.')
    first = words.pop(0)
    name = first + ' ' + (words.pop(0) if words else '') if first in {'subscriptions', 'runs', 'dev'} else first
    if name == 'dev stand-in':
        if len(words) < 2 or words[1] != '--':
            raise Failure('invalid_input', 'Use dev stand-in <folder> -- <arguments>.')
        return Command(name, words, options)
    if name not in SCHEMAS:
        raise Failure('invalid_input', 'Unknown Driver command.')
    values, flags, repeat, minimum, maximum = SCHEMAS[name]
    positionals: list[str] = []
    only = False
    i = 0
    while i < len(words):
        arg = words[i]
        if arg == '--' and not only:
            only = True
        elif not arg.startswith('-') or only:
            positionals.append(arg)
        elif not arg.startswith('--'):
            raise Failure('invalid_input', 'Unknown Driver option. Build selection belongs to the arbol-builds launcher.')
        else:
            key, equal, value = arg[2:].partition('=')
            if key in flags:
                if equal or key in options:
                    raise Failure('invalid_input', f'Option --{key} takes no value and may occur once.')
                options[key] = True
            elif key in values or key in repeat:
                if not equal:
                    i += 1
                    value = words[i] if i < len(words) else ''
                if not value or (not equal and value.startswith('--')):
                    raise Failure('invalid_input', f'Option --{key} needs a value.')
                if key in repeat:
                    previous = options.get(key)
                    options[key] = [*(previous if isinstance(previous, list) else []), value]
                else:
                    if key in options:
                        raise Failure('invalid_input', f'Option --{key} may occur once.')
                    options[key] = value
            else:
                raise Failure('invalid_input', 'Unknown Driver option.')
        i += 1
    if not minimum <= len(positionals) <= maximum:
        raise Failure('invalid_input', f'Command {name} has the wrong number of arguments.')
    for key, allowed in [('tools', ['all', 'none']), ('output', ['events', 'text'])]:
        if key in options and options[key] not in allowed:
            raise Failure('invalid_input', f'{key.title()} must be {allowed[0]} or {allowed[1]}.')
    return Command(name, positionals, options)


def read_settings(file: Path) -> tuple[Record, list[Record]]:
    try:
        text = file.read_text()
    except FileNotFoundError:
        return {}, []
    except OSError:
        raise Failure('invalid_input', f'Cannot read settings file {file}.') from None
    try:
        root = loads(text)
    except ValueError:
        raise Failure('invalid_input', f'Settings file {file} is not valid JSON.') from None
    notices: list[Record] = []

    def bad(field: str) -> None:
        raise Failure('invalid_input', f'Settings file {file}: invalid field {field}.')

    def obj(value: Any, field: str) -> Record:
        if not isinstance(value, dict):
            bad(field)
        return dict(value)

    def unknown(value: Record, known: list[str], prefix: str) -> None:
        for key in value:
            if key not in known:
                notices.append({'code': 'unknown_setting', 'message': f'Ignoring unknown setting {prefix}{key} in {file}.'})

    def strings(value: Record, keys: list[str], prefix: str) -> Record:
        result: Record = {}
        for key in keys:
            if key in value:
                if not isinstance(value[key], str) or not value[key]:
                    bad(prefix + key)
                result[key] = value[key]
        return result

    root = obj(root, '(root)')
    unknown(root, ['toolPath', 'defaultSubscription', 'subscriptions'], '')
    settings = strings(root, ['toolPath', 'defaultSubscription'], '')
    if 'subscriptions' in root:
        settings['subscriptions'] = {}
        for identifier, value in obj(root['subscriptions'], 'subscriptions').items():
            prefix = f'subscriptions.{identifier}.'
            item = obj(value, prefix[:-1])
            unknown(item, ['defaultModel', 'defaultEffort', 'settings', 'secret'], prefix)
            clean = strings(item, ['defaultModel', 'defaultEffort'], prefix)
            if 'settings' in item:
                clean['settings'] = obj(item['settings'], prefix + 'settings')
            if 'secret' in item:
                secret = obj(item['secret'], prefix + 'secret')
                if secret.get('from') == 'keychain':
                    unknown(secret, ['from'], prefix + 'secret.')
                    clean['secret'] = {'from': 'keychain'}
                elif secret.get('from') == 'command':
                    unknown(secret, ['from', 'command'], prefix + 'secret.')
                    command = secret.get('command')
                    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command) or not command[0]:
                        bad(prefix + 'secret.command')
                    clean['secret'] = {'from': 'command', 'command': command}
                else:
                    bad(prefix + 'secret.from')
            settings['subscriptions'][identifier] = clean
    return settings, notices


def build_identity(start: Path | None = None) -> Record:
    directory = (start or Path(__file__).parent).resolve()
    while True:
        build = directory / 'build.json'
        if build.exists():
            try:
                value = loads(build.read_text())
            except (ValueError, OSError):
                raise Failure('internal', f'Cannot read Build record {build}.') from None
            if not isinstance(value, dict) or value.get('component') != 'arbol-claude-driver' or not isinstance(value.get('build'), str) or not re.fullmatch(r'[1-9a-f][0-9a-f]*', value['build']) or not isinstance(value.get('packageVersion'), str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', value['packageVersion']):
                raise Failure('internal', f'Invalid Build record {build}.')
            return {'version': value['packageVersion'], 'build': value['build']}
        package = directory / 'package.json'
        if package.exists():
            try:
                value = loads(package.read_text())
            except (ValueError, OSError):
                raise Failure('internal', 'Cannot read the Driver package version.') from None
            if isinstance(value, dict) and value.get('name') == '@sylvan-stack/arbol-claude-driver' and isinstance(value.get('version'), str):
                return {'version': value['version'], 'build': '0'}
        if directory.parent == directory:
            raise Failure('internal', 'The Driver has no package or Build record.')
        directory = directory.parent
