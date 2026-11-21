"""The Driver's only public interface: one process for one CLI operation."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .maintenance import Maintenance

from .admission import Admission
from .continuity import Runs
from .core import Failure, build_identity, compact, fixed_wait, parse_arguments, read_settings, retired_route, safe_failure
from .dev import live_check, probe
from .identity import canonical_id, ensure_directory, parse_expected
from .paths import installation_root, state_directory
from .processes import ACTUAL_HOME, inspect_tool
from .protocol import Output, RunEvents
from .runtime import run
from .standin import stand_in
from .state import State
from .subscriptions import Authentication, FAMILIES, Subscriptions, family_id


def initialized_state(home: str, override: object, admission: Admission) -> State:
    default = state_directory(home)
    state = State(str(override) if override is not None else default)
    if state.directory == Path(os.path.abspath(default)):
        ensure_directory(installation_root(home))
    state.initialize(admission)
    return state


async def input_stream() -> tuple[asyncio.StreamReader, asyncio.ReadTransport | None]:
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    mode = os.fstat(0).st_mode
    if stat.S_ISREG(mode):
        reader.feed_data(sys.stdin.buffer.read())
        reader.feed_eof()
        return reader, None
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) and sys.stdin.isatty()):
        reader.feed_eof()
        return reader, None
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    except (OSError, ValueError):
        reader.feed_eof()
        return reader, None
    return reader, transport


async def main(args: list[str], admission: Admission | None = None) -> int:
    if admission is None or admission.closed:
        raise Failure('internal', 'Driver admission is unavailable; use the qualified registered launcher.')
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(name, cancel.set)
    output = Output(cancel)
    words = [word for index, word in enumerate(args) if word != '--state-dir' and not word.startswith('--state-dir=') and (not index or args[index - 1] != '--state-dir')]
    is_run = bool(words and words[0] == 'run')
    events = RunEvents(output, '--output=text' in args or '--output' in args and args[args.index('--output') + 1:args.index('--output') + 2] == ['text']) if is_run else None
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    transport: asyncio.ReadTransport | None = None
    try:
        command = parse_arguments(args)
        if command.name.startswith('state '):
            raise Failure('invalid_input', 'State maintenance requires the exclusive registered driver-state route.')
        if command.name in {'run', 'dev stand-in'}:
            reader, transport = await input_stream()
        if command.name == 'dev stand-in':
            # Vendor stand-ins handle signals like the vendor process itself.
            for name in [signal.SIGINT, signal.SIGTERM]:
                loop.remove_signal_handler(name)
                signal.signal(name, signal.SIG_DFL)
            return await stand_in(Path(command.positionals[0]).absolute(), command.positionals[2:], reader)
        if command.name == 'subscriptions identity':
            identifier = command.positionals[0]
            if identifier in {'zai', 'claude-zai'}:
                raise retired_route()
            canonical_id(identifier)
            expected = parse_expected(str(command.options['expect']) if 'expect' in command.options else None)
            home = os.environ.get('HOME', ACTUAL_HOME)
            file = installation_root(home) / 'arbol-claude-driver.json'
            state = initialized_state(home, command.options.get('state-dir'), admission)
            subscriptions = Subscriptions(state, {}, [], file)
            await output.json(await subscriptions.identity(identifier, expected))
            return 0
        fixed_wait(1)
        home = os.environ.get('HOME', ACTUAL_HOME)
        file = installation_root(home) / 'arbol-claude-driver.json'
        settings, notices = read_settings(file)
        state = initialized_state(home, command.options.get('state-dir'), admission)
        identity = build_identity(payload=admission.payload)
        subscriptions = Subscriptions(state, settings, notices, file)
        authenticate = Authentication(subscriptions, dict(os.environ), cancel)
        if command.name == 'run' or command.name.startswith('runs ') or command.name in {'subscriptions create', 'subscriptions update', 'subscriptions sign-in', 'subscriptions sign-out', 'subscriptions remove', 'subscriptions import', 'subscriptions check', 'subscriptions usage'}:
            state.clear_unfinished_writes()
            await Runs(state).list()
            await authenticate.clean_all_progress()
        if cancel.is_set():
            return 2
        if command.name == 'info':
            await output.json({'driver': 'claude', **identity, 'contract': 2, 'capabilities': {'subscriptionIdentity': 1, 'cueContext': 1}, 'stateDirectory': str(state.directory), 'settingsFile': str(file), 'tool': await inspect_tool(state.directory, settings.get('toolPath'), dict(os.environ), cancel), **({'notices': notices} if notices else {})})
            return 0
        if command.name == 'dev probe':
            await output.json(await probe(subscriptions, dict(os.environ), cancel))
            return 0
        if command.name in {'dev build-check', 'dev live-check'}:
            status, dev_result = await live_check(command.name == 'dev build-check', subscriptions, dict(os.environ), cancel)
            await output.json(dev_result)
            return status
        if command.name == 'run':
            assert events
            return await run(command, subscriptions, events, dict(os.environ), identity['build'], cancel, reader)
        if command.name == 'runs list':
            await output.json([{key: record[key] for key in ['runId', 'subscriptionId', 'startedAt', 'labels'] if key in record} for record in await Runs(state).list()])
            return 0
        if command.name == 'runs stop':
            await Runs(state).stop(command.positionals[0])
            return 0
        identifier = command.positionals[0] if command.positionals else ''
        options = command.options
        result: Any = None
        if command.name == 'subscriptions families':
            result = FAMILIES
        elif command.name == 'subscriptions list':
            result = await subscriptions.list()
        elif command.name == 'subscriptions create':
            if not isinstance(options.get('label'), str):
                raise Failure('invalid_input', 'Give the new Subscription a label.')
            result = await subscriptions.create(family_id(options.get('family')), str(options['label']), str(options['id']) if 'id' in options else None)
        elif command.name == 'subscriptions update':
            await authenticate.clean_progress(identifier)
            result = await subscriptions.update(identifier, str(options['label']) if 'label' in options else None)
        elif command.name == 'subscriptions models':
            result = await authenticate.models(identifier)
        elif command.name == 'subscriptions check':
            result = await authenticate.check(identifier)
        elif command.name == 'subscriptions usage':
            result = await authenticate.usage(identifier)
        elif command.name == 'subscriptions sign-in':
            return await authenticate.sign_in(identifier, options.get('by-code') is True, output.json)
        elif command.name == 'subscriptions sign-out':
            result = await authenticate.sign_out(identifier, options.get('force') is True)
        elif command.name == 'subscriptions remove':
            await authenticate.sign_out(identifier, options.get('force') is True, True)
            return 0
        elif command.name == 'subscriptions import':
            if not isinstance(options.get('from'), str):
                raise Failure('invalid_input', 'Name the settings file with --from.')
            result = await authenticate.import_settings(identifier, Path(str(options['from'])).absolute())
        if result is not None:
            if notices:
                result = [{**item, 'notices': notices} for item in result] if isinstance(result, list) else {**result, 'notices': notices}
            await output.json(result)
            return 0
        raise Failure('unsupported', f'Command {command.name} is not implemented in this Driver yet.')
    except BaseException as error:
        if events:
            try:
                return await events.finish(error, cancel.is_set())
            except (OSError, TimeoutError):
                return 2
        if cancel.is_set():
            return 2
        await output.json({'error': safe_failure(error)})
        return 1
    finally:
        if transport:
            transport.close()
        for name in [signal.SIGINT, signal.SIGTERM]:
            loop.remove_signal_handler(name)
            # A replacing peer may still be polling this process after the
            # command has settled. Preserve its chosen status during shutdown.
            signal.signal(name, signal.SIG_IGN)


def entry(admission: Admission | None = None) -> None:
    try:
        status = asyncio.run(main(sys.argv[1:], admission))
    except Failure as error:
        sys.stdout.write(compact({'error': safe_failure(error)}) + '\n')
        sys.stdout.flush()
        status = 1
    except (BrokenPipeError, KeyboardInterrupt):
        status = 2
    finally:
        # asyncio.run has drained/cancelled all tasks, and final output and
        # transport cleanup have completed. Close only our identity guards;
        # the bootstrap retains component admission until this returns.
        State.close_all()
    raise SystemExit(status)


def administrative_entry(admission: Maintenance) -> None:
    from .identity_protocol import LIMIT, canonical
    try:
        result = asyncio.run(admission.execute())
        admission.validate_result(result)
        raw = canonical(result)
        if len(raw) > LIMIT:
            raise Failure('internal', 'Maintenance result exceeds its bound.')
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        status = 0
    except Exception as error:
        sys.stdout.write(compact({'error': safe_failure(error)}) + '\n')
        sys.stdout.flush()
        status = 1
    raise SystemExit(status)
