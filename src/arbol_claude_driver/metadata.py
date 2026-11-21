"""Admitted metadata broker; vendor callbacks can only notify its fixed target.

No callback receives a component lease, maintenance authority or a state path.
The exact helper owns a fresh independent admission lease and local guard.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from .admission import Admission, current_admission, descriptor_number
from .core import Failure, Record
from .identity import canonical_id, private_file
from .identity_protocol import LIMIT, MANIFEST_LIMIT, canonical, decode, physical, token, valid_token
from .state import State, atomic_write, owner_alive, parse_owner, ps_time, read_json


def _check(value: Record, names: str) -> None:
    if set(value) != set(names.split()):
        raise Failure('internal', 'Metadata notification is malformed.')


async def _receive(channel: socket.socket, *, timeout: float | None = 20) -> Record | None:
    loop = asyncio.get_running_loop()

    async def exact(size: int, *, eof: bool = False) -> bytes | None:
        raw = bytearray()
        while len(raw) < size:
            part = await loop.sock_recv(channel, size - len(raw))
            if not part:
                if eof and not raw:
                    return None
                raise Failure('internal', 'Metadata channel closed before acknowledgement.')
            raw.extend(part)
        return bytes(raw)

    async with asyncio.timeout(timeout):
        head = await exact(4, eof=True)
        if head is None:
            return None
        size, = struct.unpack('!I', head)
        if not 0 < size <= LIMIT:
            raise Failure('internal', 'Metadata notification exceeds its bound.')
        raw = await exact(size)
        assert raw is not None
        return decode(raw)


async def _send(channel: socket.socket, value: Record) -> None:
    raw = canonical(value)
    if len(raw) > LIMIT:
        raise Failure('internal', 'Metadata notification exceeds its bound.')
    async with asyncio.timeout(20):
        await asyncio.get_running_loop().sock_sendall(channel, struct.pack('!I', len(raw)) + raw)


@dataclass
class Broker:
    child: asyncio.subprocess.Process
    channel: socket.socket
    port: int
    nonce: str
    closed: bool = False

    @classmethod
    async def start(cls, state: State, identifier: str, reservation: Record) -> Broker:
        admission = current_admission()
        ticket = admission.prepare_helper('bin/metadata-helper.py')
        parent, child_channel = socket.socketpair()
        parent.setblocking(False)
        child: asyncio.subprocess.Process | None = None
        try:
            child = await asyncio.create_subprocess_exec(
                *ticket.prefix, '--channel-fd', str(child_channel.fileno()),
                env=ticket.environment(dict(os.environ)), pass_fds=(*ticket.pass_fds, child_channel.fileno()),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            child_channel.close()
            handshake = asyncio.create_task(asyncio.to_thread(ticket.acknowledge, child.pid))
            try:
                await asyncio.shield(handshake)
            except BaseException:
                # Cancellation cannot abandon a registry-writing worker thread.
                await handshake
                raise
            keys = ('runId', 'subscriptionId', 'owner', 'startedAt', 'identity')
            reservation = {key: reservation[key] for key in keys if key in reservation}
            await _send(parent, {'mode': 'run', 'stateDirectory': str(state.directory),
                                 'subscription': canonical_id(identifier), 'reservation': reservation})
            ready = await _receive(parent)
            if (ready is None or set(ready) != {'type', 'port', 'nonce'} or ready['type'] != 'ready'
                    or type(ready['port']) is not int or not 0 < ready['port'] < 65536 or not valid_token(ready['nonce'])):
                raise Failure('internal', 'The metadata helper did not acknowledge startup.')
            return cls(child, parent, ready['port'], ready['nonce'])
        except BaseException:
            parent.close()
            child_channel.close()
            # Neither EOF nor termination fabricates terminal writer custody.
            if child is not None:
                try:
                    await asyncio.wait_for(child.wait(), 20)
                except TimeoutError:
                    child.kill()
                    await child.wait()
            raise
        finally:
            ticket.child_socket.close()

    def notification_code(self, kind: str) -> str:
        if kind not in ('tool', 'shell'):
            raise Failure('internal', 'Unknown metadata notification kind.')
        # This generated client imports stdlib only, writes no Driver file and
        # knows only the notification endpoint. It has no registry authority.
        return f'''import json,os,socket,struct,subprocess,sys
value={{"kind":{kind!r},"nonce":{self.nonce!r}}}
value["pid"]=os.getpid()
value["stamp"]=" ".join(subprocess.check_output(["/bin/ps","-p",str(os.getpid()),"-o","lstart="],env={{"PATH":"/usr/bin:/bin","LC_ALL":"C","TZ":"UTC"}},timeout=5).decode("ascii").split())
raw=(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\\n").encode("utf-8")
if len(raw)>4096: raise SystemExit(1)
with socket.create_connection(("127.0.0.1",{self.port}),timeout=20) as channel:
 channel.sendall(struct.pack("!I",len(raw))+raw)
 def exact(n):
  data=b""
  while len(data)<n:
   part=channel.recv(n-len(data))
   if not part: raise SystemExit(1)
   data+=part
  return data
 size=struct.unpack("!I",exact(4))[0]
 if size<1 or size>4096: raise SystemExit(1)
 if json.loads(exact(size))!={{"ok":True}}: raise SystemExit(1)
'''

    async def close(self, *, deadline: float | None = None) -> None:
        if self.closed:
            return
        try:
            remaining = 20 if deadline is None else max(0, deadline - time.monotonic())
            async with asyncio.timeout(remaining):
                await _send(self.channel, {'type': 'finish'})
                if await _receive(self.channel) != {'type': 'closed'}:
                    raise Failure('internal', 'Metadata helper cleanup was not acknowledged.')
                status = await self.child.wait()
                if status != 0:
                    raise Failure('internal', 'Metadata helper custody remains unresolved.')
                self.closed = True
        finally:
            # The independently leased helper may still be draining its own
            # bounded clients. Parent timeout never fabricates its terminal ACK.
            self.channel.close()



@dataclass
class Target:
    state: State
    identifier: str
    reservation: Record
    nonce: str = field(default_factory=token)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)

    def owns(self) -> bool:
        if self.state.identity is None or not self.state.identity.matches(self.identifier, self.reservation.get('identity')):
            return False
        current = read_json(self.state.directory / 'runs' / (self.reservation['runId'] + '.json'))
        keys = ('runId', 'subscriptionId', 'startedAt', 'owner', 'identity')
        return isinstance(current, dict) and all(current.get(key) == self.reservation.get(key) for key in keys)

    async def write(self, value: Record) -> None:
        if value.get('nonce') != self.nonce:
            raise Failure('internal', 'Metadata notification authority does not match.')
        kind = value.get('kind')
        if kind not in ('tool', 'shell'):
            raise Failure('internal', 'Metadata notification does not match its operation.')
        _check(value, 'kind nonce pid stamp')
        if type(value['pid']) is not int or value['pid'] <= 0 or not isinstance(value['stamp'], str):
            raise Failure('internal', 'Metadata process notification is malformed.')
        if not await owner_alive(parse_owner({'pid': value['pid'], 'startedAt': ps_time(value['stamp'])})):
            raise Failure('internal', 'Metadata process owner is no longer current.')
        if value['nonce'] != self.nonce:
            raise Failure('internal', 'Metadata notification authority does not match.')
        async with self.mutex, self.state.lock(self.identifier, allow_pending=True):
            if not self.owns():
                raise Failure('in_use', 'The metadata writer lifetime or operation changed.')
            folder = self.state.directory / 'tmp' / self.reservation['runId']
            path = folder / ('tool-owner' if kind == 'tool' else 'shell-calls')
            content = f"{value['pid']}\t{value['stamp']}\n"
            if kind == 'shell' and path.exists():
                private_file(path)
                if path.stat().st_size > MANIFEST_LIMIT - len(content.encode()):
                    raise Failure('internal', 'Metadata process ledger exceeds its bound.')
                content = path.read_text() + content
            # Parent prepared this exact folder. A retired target may never be
            # recreated by a callback after cleanup.
            physical(folder)
            atomic_write(path, content)

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            async with asyncio.timeout(20):
                head = await reader.readexactly(4)
                size, = struct.unpack('!I', head)
                if not 0 < size <= LIMIT:
                    raise Failure('internal', 'Metadata notification exceeds its bound.')
                value = decode(await reader.readexactly(size))
                await self.write(value)
                raw = canonical({'ok': True})
                writer.write(struct.pack('!I', len(raw)) + raw)
                await writer.drain()
        except Exception:
            # Failure is an EOF to the notification-only client. It never
            # supplies a success-shaped acknowledgement or writes a new path.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            finally:
                self.tasks.discard(task)


async def helper(admission: Admission, descriptor: int) -> None:
    channel = socket.socket(fileno=descriptor)
    channel.setblocking(False)
    os.set_inheritable(descriptor, False)
    state: State | None = None
    server: asyncio.Server | None = None
    target: Target | None = None
    try:
        config = await _receive(channel)
        if config is None:
            raise Failure('internal', 'Metadata helper configuration is missing.')
        _check(config, 'mode stateDirectory subscription reservation')
        identifier = canonical_id(config['subscription'])
        mode, reservation = config['mode'], config['reservation']
        if mode != 'run' or not isinstance(reservation, dict):
            raise Failure('internal', 'Metadata helper configuration is malformed.')
        _check(reservation, 'runId subscriptionId owner startedAt identity' if 'identity' in reservation else 'runId subscriptionId owner startedAt')
        canonical_id(reservation['runId'])
        if reservation['subscriptionId'] != identifier:
            raise Failure('internal', 'Metadata run target does not match.')
        parse_owner(reservation.get('owner'))
        directory = physical(config['stateDirectory'])
        state = State(directory)
        state.initialize(admission)
        target = Target(state, identifier, reservation)
        async with state.lock(identifier, allow_pending=True):
            if not target.owns():
                raise Failure('in_use', 'Metadata helper target is no longer admitted.')
        server = await asyncio.start_server(target.client, host='127.0.0.1', port=0, limit=LIMIT + 4, backlog=16)
        port = server.sockets[0].getsockname()[1]
        await _send(channel, {'type': 'ready', 'port': port, 'nonce': target.nonce})
        command = await _receive(channel, timeout=None)
        if command is not None and command != {'type': 'finish'}:
            raise Failure('internal', 'Metadata helper shutdown is malformed.')
        # EOF revokes future notifications; it is not proof about parent
        # custody. This helper can still settle its own exact terminal record.
        server.close()
        await server.wait_closed()
        if target.tasks:
            await asyncio.gather(*tuple(target.tasks))
        if command is not None:
            await _send(channel, {'type': 'closed'})
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        if target is not None and target.tasks:
            await asyncio.gather(*tuple(target.tasks), return_exceptions=True)
        channel.close()
        if state is not None:
            state.close()


def helper_entry(admission: Admission, arguments: list[str]) -> None:
    if len(arguments) != 2 or arguments[0] != '--channel-fd':
        raise Failure('invalid_input', 'The metadata helper requires its private channel.')
    descriptor = descriptor_number(arguments[1])
    # Shutdown is controlled by the admitted parent channel. A forced kill
    # deliberately leaves unresolved durable custody rather than a false ACK.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    asyncio.run(helper(admission, descriptor))
