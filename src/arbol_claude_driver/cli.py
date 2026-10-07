"""The Driver's only public interface: one process for one CLI operation."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import stat
from pathlib import Path
from typing import Any

from .continuity import Runs
from .core import Failure, build_identity, fixed_wait, parse_arguments, read_settings, safe_failure
from .dev import live_check, probe
from .processes import ACTUAL_HOME, inspect_tool
from .protocol import Output, RunEvents
from .runtime import run
from .standin import stand_in
from .state import State
from .subscriptions import Authentication, FAMILIES, Subscriptions, family_id


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


async def main(args: list[str]) -> int:
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(name, cancel.set)
    output = Output(cancel)
    words = [word for index, word in enumerate(args) if word != '--state-dir' and not word.startswith('--state-dir=') and (not index or args[index - 1] != '--state-dir')]
    is_run = bool(words and words[0] == 'run')
    events = RunEvents(output, '--output=text' in args or '--output' in args and args[args.index('--output') + 1:args.index('--output') + 2] == ['text']) if is_run else None
    reader, transport = await input_stream()
    try:
        command = parse_arguments(args)
        if command.name == 'dev stand-in':
            # Vendor stand-ins handle signals like the vendor process itself.
            for name in [signal.SIGINT, signal.SIGTERM]:
                loop.remove_signal_handler(name)
                signal.signal(name, signal.SIG_DFL)
            return await stand_in(Path(command.positionals[0]).absolute(), command.positionals[2:], reader)
        fixed_wait(1)
        home = os.environ.get('HOME', ACTUAL_HOME)
        file = Path(home) / '.arbol/arbol-claude-driver.json'
        settings, notices = read_settings(file)
        state = State(str(command.options.get('state-dir', str(Path(home) / 'Library/Application Support/Arbol/drivers/claude'))))
        state.initialize()
        identity = build_identity()
        subscriptions = Subscriptions(state, settings, notices, file)
        authenticate = Authentication(subscriptions, dict(os.environ), cancel)
        if command.name == 'run' or command.name.startswith('runs ') or command.name in {'subscriptions create', 'subscriptions update', 'subscriptions sign-in', 'subscriptions sign-out', 'subscriptions remove', 'subscriptions import', 'subscriptions check', 'subscriptions usage'}:
            state.clear_unfinished_writes()
            await Runs(state).list()
            await authenticate.clean_all_progress()
        if cancel.is_set():
            return 2
        if command.name == 'info':
            await output.json({'driver': 'claude', **identity, 'contract': 1, 'stateDirectory': str(state.directory), 'settingsFile': str(file), 'tool': await inspect_tool(state.directory, settings.get('toolPath'), dict(os.environ), cancel), **({'notices': notices} if notices else {})})
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
            return await authenticate.sign_in(identifier, options.get('by-code') is True, output.json, reader)
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


def entry() -> None:
    try:
        status = asyncio.run(main(sys.argv[1:]))
    except (BrokenPipeError, KeyboardInterrupt):
        status = 2
    raise SystemExit(status)
