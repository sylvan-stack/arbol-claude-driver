"""Offline Claude scripts derived from retained 2.1.289 envelopes.

All content, identifiers, counts, timing and failure variants are synthetic.
The recorded JSON envelopes remain unchanged in test/.
"""
from __future__ import annotations

import asyncio
import copy
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from .core import Failure, Record, compact, now, loads
from .history import encode_folder
from .processes import child_command

FIXTURES = Path(__file__).resolve().parents[2] / 'test'
RUN_SCENARIOS = {'C4', 'C5', 'C7', 'C8', 'C26', 'C27', 'C28', 'C29', 'C30', 'C9', 'C10', 'C11', 'C12', 'C13', 'C14', 'C15', 'C16', 'C17', 'C18', 'C19', 'C20', 'C25', 'C32', 'C33', 'C35', 'C36', 'C37', 'C38', 'C40', 'C42', 'C43'}


class StandIn:
    def __init__(self, folder: Path, args: list[str], scenario: str, part: str):
        self.folder, self.args, self.scenario, self.part = folder, args, scenario, part
        self.raw = loads((FIXTURES / 'run-protocol-2026-10-06.json').read_text())
        self.subscription = loads((FIXTURES / 'subscription-protocol-2026-10-06.json').read_text())
        self.tasks: list[asyncio.Task[None]] = []
        self.pending: dict[str, str] = {}
        self.answered: list[str] = []
        self.conversation = self.option('--resume') or self.option('--session-id') or str(uuid.uuid4())
        self.home = Path(os.environ.get('CLAUDE_CONFIG_DIR', '.'))
        self.history: list[Record] = []
        self.file: Path | None = None
        self.checks: Record = {}

    def option(self, name: str) -> str | None:
        try:
            return self.args[self.args.index(name) + 1]
        except (ValueError, IndexError):
            return None

    def log(self, stage: str | None, checks: Record) -> None:
        record = {'scenario': self.scenario, 'part': self.part, 'arguments': self.args, 'environment': sorted(os.environ), 'workingDirectory': os.getcwd(), 'checks': checks, **({'stage': stage} if stage else {})}
        with (self.folder / 'received.jsonl').open('a') as handle:
            handle.write(compact(record) + '\n')

    def output(self, record: Record) -> None:
        sys.stdout.write(compact(record) + '\n')
        sys.stdout.flush()

    def text(self, text: str, parent: str | None = None) -> None:
        self.output({**self.raw['text'], 'parent_tool_use_id': parent, 'message': {**self.raw['text']['message'], 'content': [{'type': 'text', 'text': text}]}})

    def delta(self, text: str) -> None:
        self.output({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}})

    def call(self, identifier: str, name: str, value: Record, parent: str | None = None) -> None:
        self.output({'type': 'assistant', 'parent_tool_use_id': parent, 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': identifier, 'name': name, 'input': value}]}})

    def result(self, identifier: str, value: Any, error: bool = False, parent: str | None = None) -> None:
        self.output({'type': 'user', 'parent_tool_use_id': parent, 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': identifier, 'content': value, 'is_error': error}]}})

    def final(self, failed: bool = False, text: str = 'answer', fields: Record | None = None) -> None:
        self.output({**copy.deepcopy(self.raw['result']), **({'modelUsage': {self.option('--model') or 'glm-fixture': {'costBasis': 'unknown'}}} if os.environ.get('ANTHROPIC_BASE_URL') else {}), 'is_error': failed, 'result': text, **(fields or {})})

    def after(self, delay: float, action: Any) -> None:
        async def timer() -> None:
            await asyncio.sleep(delay)
            value = action()
            if asyncio.iscoroutine(value):
                await value
        self.tasks.append(asyncio.create_task(timer()))

    def ask(self, identifier: str, multiple: bool = False) -> None:
        value = {'questions': [{'question': 'Choose ' + identifier, 'header': identifier, 'multiSelect': multiple, 'options': [{'label': 'Blue', 'description': 'Cool'}, {'label': 'Red', 'description': 'Warm'}]}]}
        self.call('call-' + identifier, 'AskUserQuestion', value)
        self.pending[identifier] = 'call-' + identifier
        self.output({'type': 'control_request', 'request_id': identifier, 'request': {'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'tool_use_id': 'call-' + identifier, 'requires_user_interaction': True, 'input': value}})

    def persist(self, message: Record | None = None, answer: Record | None = None) -> None:
        if '--no-session-persistence' in self.args:
            return
        if self.file is None:
            name = encode_folder(os.getcwd())
            directory = self.home / 'projects' / (name[:200] + '-abcdef' if len(name) > 200 else name)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.file = directory / (self.conversation + '.jsonl')
        if message is None:
            self.file.write_text('{}\n')
            return
        parent = self.history[-1].get('uuid') if self.history else None
        for item in [message, answer]:
            assert item is not None
            identifier = str(uuid.uuid4())
            with self.file.open('a') as handle:
                handle.write(compact({'type': item['type'], 'message': item['message'], 'uuid': identifier, 'parentUuid': parent, 'sessionId': self.conversation, 'cwd': os.getcwd(), 'isSidechain': False, 'userType': 'external', 'version': self.raw['toolVersion'], 'timestamp': now()}) + '\n')
            self.file.chmod(0o600)
            parent = identifier

    async def prepare(self) -> int | None:
        resumed = self.option('--resume')
        if self.scenario == 'C14':
            probe = loads((FIXTURES / 'continuity-probe-2026-10-07.json').read_text())
            if resumed and self.part in {'refuse', 'refuse-stderr'}:
                record = copy.deepcopy(probe['missingConversation']['records'][0])
                record.update(session_id=self.conversation, errors=[f'No conversation found with session ID: {self.conversation}'])
                if self.part != 'refuse-stderr':
                    self.output(record)
                sys.stderr.write(record['errors'][0] + '\n')
                return 1
            projects = self.home / 'projects'
            if projects.exists():
                for folder in projects.iterdir():
                    candidate = folder / (self.conversation + '.jsonl')
                    if candidate.is_file():
                        self.file = candidate
                        self.history = [loads(line) for line in candidate.read_text().splitlines() if line]
                        break
            blocks = [block for record in self.history for block in record.get('message', {}).get('content', [])]
            calls = [block for block in blocks if block.get('type') == 'tool_use']
            results = [block for block in blocks if block.get('type') == 'tool_result']
            shell = next((item for item in calls if item['name'] == 'Bash' and item.get('input', {}).get('command') == 'printf history-shell-output'), None)
            result = next((item for item in results if shell and item['tool_use_id'] == shell['id']), None)
            foreign = next((item for item in calls if item['name'] == 'foreign_tool' and item.get('input', {}).get('value') == 'foreign-input'), None)
            self.checks = {'source': probe['check'], 'toolVersion': self.raw['toolVersion'], 'nativeShellCall': bool(shell), 'nativeShellFailed': bool(result and result.get('is_error')), 'nativeForeignCall': bool(foreign and any(item['tool_use_id'] == foreign['id'] and item.get('content') == '{"value":"foreign-value"}' for item in results)), 'historyTexts': [block['text'] for block in blocks if block.get('type') == 'text'], 'historyImages': [{'mediaType': block['source']['media_type'], 'data': block['source']['data']} for block in blocks if block.get('type') == 'image'], 'nativeHistory': self.file is not None, 'systemPrompt': self.option('--append-system-prompt') or '', 'promptSnapshotOff': self.option('--system-prompt-snapshot') == 'off', **({'nativeShellResult': result['content']} if result else {})}
        if self.scenario in {'C32', 'C7'} and resumed:
            projects = self.home / 'projects'
            if not projects.exists() or not any((folder / (resumed + '.jsonl')).is_file() for folder in projects.iterdir()):
                self.output({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'errors': ['No conversation found']})
                return 1
        return None

    async def control(self, value: Record) -> int | None:
        subtype = value['request']['subtype']
        if self.scenario in {'C32', 'C7'}:
            self.log('control', {'subtype': subtype, 'source': self.subscription['source']})
        if subtype == 'initialize':
            response = copy.deepcopy(self.subscription['initialize']['response']['response'])
            if self.scenario in {'C32', 'C7'}:
                response['current_permission_mode'] = 'bypassPermissions'
        elif subtype == 'get_settings':
            response = {'applied': {} if self.scenario in {'C4', 'C26', 'C27', 'C28', 'C29', 'C30'} else {'model': self.option('--model') or 'default', 'effort': None if self.part == 'dropped-effort' else self.option('--effort') or 'high'}}
        else:
            response = {'mcpServers': []} if subtype == 'mcp_status' else {}
        if subtype != 'interrupt' or self.part != 'stubborn':
            self.output({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': value['request_id'], 'response': response}})
        if subtype == 'interrupt':
            self.log('interrupt', {'input': value, 'source': '~/Artifacts/arbol-claude-driver/research/tool-probes-2026-10-05.md#7-stop'})
            if self.part == 'stubborn':
                return None
            for task in self.tasks:
                task.cancel()
            if self.part == 'subagent':
                self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
                return None
            if self.part == 'question':
                self.output({'type': 'control_cancel_request', 'request_id': 'fixture-ask'})
            for identifier in self.pending:
                self.output({'type': 'control_cancel_request', 'request_id': identifier})
            self.pending.clear()
            if self.scenario in {'C5', 'C15', 'C16', 'C17', 'C38'} or self.scenario == 'C33' and self.part == 'stopping':
                self.text('partial')
            self.final(True, 'Interrupted')
        return None

    async def response(self, value: Record) -> None:
        self.log('answer' if self.part == 'live' else 'response', {'input': value, 'delivered': True})
        identifier = value['response']['request_id']
        if self.scenario in {'C4', 'C26', 'C27', 'C28', 'C29', 'C30'}:
            self.log('answered', {})
            self.result('waiting-call', 'done')
            self.final()
        elif self.part == 'live':
            self.result('ask-call', 'Ready')
            self.call('shell-call', 'Bash', {'command': 'printf ready'})
            self.result('shell-call', 'ready')
            self.text('ready')
            self.final(text='ready')
        else:
            call = self.pending.pop(identifier, None)
            if call:
                self.answered.append(identifier)
                self.result(call, value['response']['response'], value['response']['response'].get('behavior') == 'deny')
            if self.scenario == 'C25':
                if identifier == 'allow':
                    self.output({'type': 'control_request', 'request_id': 'deny', 'request': {'subtype': 'can_use_tool'}})
                else:
                    self.text('permission-completed')
                    self.final()
            elif not self.pending:
                self.text('answers-' + '-'.join(self.answered))
                self.final()

    async def stopping(self, value: Record) -> None:
        part = 'hold' if self.part == 'stopping' else self.part
        self.persist()
        self.output(self.raw['init'])
        pid = None
        if part in {'background', 'hold', 'stubborn', 'subagent', 'linger'}:
            path = self.folder / ('worker-' + str(os.getpid()))
            worker = await asyncio.create_subprocess_exec(os.environ['CLAUDE_CODE_SHELL'], '-c', f'sleep 60 >/dev/null 2>&1 & echo $! > {str(path)!r}', env=dict(os.environ), start_new_session=True, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await worker.wait()
            pid = int(path.read_text().strip())
        self.log('input', {'input': value, 'toolPid': os.getpid(), 'backgroundPid': pid, 'backgroundWasAlive': pid is not None, 'shell': os.environ.get('CLAUDE_CODE_SHELL'), 'source': self.raw['source'], 'synthetic': 'stop variants'})
        self.delta('partial')
        if part in {'tool', 'question'}:
            self.call('fixture-call', 'Bash' if part == 'tool' else 'AskUserQuestion', {'command': 'sleep 60'})
        if part == 'question':
            self.output({'type': 'control_request', 'request_id': 'fixture-ask', 'request': {'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'tool_use_id': 'fixture-call', 'input': {'questions': [{'question': 'Choose a colour', 'header': 'Colour', 'multiSelect': False, 'options': [{'label': 'Blue', 'description': 'Blue'}, {'label': 'Red', 'description': 'Red'}]}]}}})
        if part == 'subagent':
            self.call('fixture-agent', 'Agent', {'command': 'sleep 60'})
            self.result('fixture-agent', 'Agent launched')
            self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_id': 'fixture-task', 'task_type': 'local_agent', 'description': 'worker'}]})
            self.final()
            self.call('fixture-subcall', 'Bash', {'command': 'sleep 60'}, 'fixture-agent')
            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(.02)
                    self.log('subagent-tick', {'active': True})
            self.tasks.append(asyncio.create_task(heartbeat()))
        elif part in {'plain', 'background', 'linger'}:
            self.text('answer')
            self.final()

    async def tools(self, value: Record) -> int | None:
        skills_path = self.home / 'skills'
        skill_path = skills_path / 'fixture-skill'
        skills = os.readlink(skill_path) if skill_path.is_symlink() else None
        skill = (skill_path / 'SKILL.md').read_text() if (skill_path / 'SKILL.md').is_file() else None
        mcp_path = self.option('--mcp-config')
        mcp = loads(Path(mcp_path).read_text()).get('mcpServers') if mcp_path else None
        self.log('input', {'input': value, 'skills': skills, 'skill': skill, 'mcp': mcp, 'configHome': str(self.home), 'memoryOff': os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') == '1', 'source': '~/Artifacts/arbol-claude-driver/research/probe-scripts-2026-10-06/live/subagent2.json'})
        self.output(self.raw['init'])
        if self.scenario in {'C9', 'C37'}:
            (skills_path / 'synced/test-account').mkdir(parents=True, exist_ok=True)
            (skills_path / 'synced/test-account/sentinel').write_text('private account skill')
            self.text('owner-tools')
            self.final()
        elif self.scenario == 'C25':
            self.output({'type': 'control_request', **({'request_id': 'allow'} if self.part != 'missing-id' else {}), 'request': {'subtype': 'can_use_tool', 'tool_name': 'Write', 'input': {'file_path': 'fixture', 'content': 'value'}}})
        elif self.scenario == 'C33':
            self.ask('unhandled')
        elif self.scenario in {'C36'}:
            if self.part == 'decline':
                self.ask('decline')
            else:
                self.ask('first')
                self.ask('second', True)
                self.ask('withdrawn')
                self.output({'type': 'control_cancel_request', 'request_id': 'withdrawn'})
                self.pending.pop('withdrawn')
                self.result('call-withdrawn', 'withdrawn', True)
        elif self.scenario == 'C35':
            self.call('refused', 'Bash', {'command': 'rejected-command', 'cwd': os.getcwd()})
            self.result('refused', 'Claude refused this command.', True)
            self.call('agent', 'Agent', {'prompt': 'synthetic sub-agent', 'model': 'haiku'})
            self.result('agent', 'Agent launched')
            self.call('background', 'Bash', {'command': 'synthetic-background', 'run_in_background': True})
            self.result('background', 'Background command launched')
            self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_id': 'agent-task', 'task_type': 'local_agent'}, {'task_id': 'bash-task', 'task_type': 'local_bash'}]})
            self.text('main-before')
            self.final()
            if self.part == 'early-exit':
                return 0
            async def complete() -> None:
                self.call('subcall', 'Bash', {'command': 'echo sub-result'}, 'agent')
                self.result('subcall', 'sub-result', parent='agent')
                self.text('sub-agent-only', 'agent')
                self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_id': 'bash-task', 'task_type': 'local_bash'}]})
                self.text('main-after-agent')
                self.final()
                await asyncio.sleep(.1)
                self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
                self.log('background-ended', {'ended': True})
                self.text('main-after-command')
                self.final()
            self.after(.1, complete)
        return None

    async def errors(self, value: Record) -> int | None:
        errors = loads((FIXTURES / 'errors-probe-2026-10-07.json').read_text())
        limits = loads((FIXTURES / 'errors-protocol-2026-10-07.json').read_text())
        self.log('input', {'source': 'test/errors-probe-2026-10-07.json', 'limitSource': limits['source'], 'synthetic': 'Codes, statuses, waits, utilization and timing are synthetic variants of recorded envelopes', 'maxRetries': os.environ.get('CLAUDE_CODE_MAX_RETRIES')})
        def failed(code: str | None = None, status: int | None = None, text: str = 'Synthetic vendor refusal', fields: Record | None = None) -> None:
            if code:
                self.output({**errors['rawLoginFailure']['assistant'], 'error': code, 'message': {'content': [{'type': 'text', 'text': text}]}})
            self.final(True, text, {'api_error_status': status, **(fields or {})})
        def limit(value: float) -> None:
            self.output({**limits['rateLimit'], 'rate_limit_info': {**limits['rateLimit']['rate_limit_info'], 'rateLimitType': 'seven_day', 'unifiedWindows': {'five_hour': {'utilization': value, 'resetsAt': 1900000000}, 'seven_day': {'utilization': value * 4, 'resetsAt': 1900010000}}}})
        def done() -> None:
            self.result('waiting-call', 'done')
            self.final()
        if self.scenario == 'C29' and self.part == 'start':
            return None
        self.output(self.raw['init'])
        if self.scenario == 'C4':
            failed('authentication_failed', 401, 'Failed to authenticate.')
        elif self.scenario == 'C26':
            mapping = {'authentication': ('authentication_failed', None, 'Not logged in · Please run /login'), '401': (None, 401, 'quota exceeded but the credential was rejected'), '403': (None, 403, 'quota exceeded but the credential was rejected'), 'model': ('model_not_found', 404, 'Synthetic vendor refusal'), '404': (None, 404, 'Synthetic vendor refusal'), 'rate': ('rate_limit_error', None, 'Synthetic vendor refusal'), '429': (None, 429, 'Synthetic vendor refusal'), 'server': ('server_error', None, 'Synthetic vendor refusal'), '503': (None, 503, 'Synthetic vendor refusal'), 'context': (None, 400, 'The conversation is too long.'), '400': (None, 400, 'quota exceeded but another refusal was stated'), '418': (None, 418, 'quota exceeded but another refusal was stated'), 'other-code': ('unrecognised_refusal', None, 'rate limit mentioned in an unrelated refusal'), 'wording-rate': (None, None, 'Usage limit; retry after 5 minutes'), 'wording-auth': (None, None, 'Invalid authentication')}
            if self.part in {'missing', 'argument', 'exit'}:
                sys.stderr.write('exec: claude: not found\n' if self.part == 'missing' else 'error: unknown option --future\n' if self.part == 'argument' else 'Synthetic tool failure\n')
                return 127 if self.part == 'missing' else 1
            if self.part == 'turns':
                failed(text='Too many turns', fields={'subtype': 'error_max_turns'})
            else:
                failed(*mapping.get(self.part, (None, None, 'Synthetic vendor refusal')))
        elif self.scenario == 'C27':
            if self.part == 'reset':
                limit(.25)
            failed(status=429, text='Retry after 5 minutes' if self.part == 'minutes' else 'Try again in 8 seconds' if self.part == 'seconds' else 'Account limit reached')
        elif self.scenario == 'C28':
            self.output({**errors['rawRejectedKey']['retries'][0], 'max_retries': 3})
            def second() -> None:
                self.log('second-retry', {})
                self.output({'type': 'system', 'subtype': 'api_retry', 'attempt': 2, 'error_status': 401})
                failed('authentication_failed', 401)
            self.after(.45, second)
        elif self.scenario == 'C29':
            if self.part in {'call', 'total'}:
                self.call('waiting-call', 'Bash', {'command': 'synthetic-work'})
                self.after(.45, done)
            elif self.part == 'background':
                self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_type': 'local_bash', 'task_id': 'waiting-task'}]})
                self.final()
                def background_done() -> None:
                    self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
                    self.final()
                self.after(.45, background_done)
            elif self.part == 'question':
                self.call('waiting-call', 'AskUserQuestion', {'command': 'synthetic-work'})
                self.output({'type': 'control_request', 'request_id': 'waiting', 'request': {'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'tool_use_id': 'waiting-call', 'input': {'questions': [{'question': 'Choose', 'options': [{'label': 'Blue'}, {'label': 'Red'}]}]}}})
            elif self.part == 'heartbeat':
                async def beats() -> None:
                    for _ in range(7):
                        await asyncio.sleep(.06)
                        self.output({'type': 'system', 'subtype': 'thinking_tokens'})
                    self.final()
                self.tasks.append(asyncio.create_task(beats()))
            elif self.part == 'idle-off':
                self.after(.45, self.final)
        elif self.scenario == 'C30':
            limit(.1)
            limit(.25)
            self.final()
        return None

    async def user(self, value: Record) -> int | None:
        scenario, part = self.scenario, self.part
        if scenario in {'C5', 'C15', 'C16', 'C17', 'C38'} or scenario == 'C33' and part == 'stopping':
            await self.stopping(value)
            return None
        if scenario in {'C4', 'C26', 'C27', 'C28', 'C29', 'C30'}:
            return await self.errors(value)
        if scenario in {'C9', 'C25', 'C35', 'C36', 'C37'} or scenario == 'C33' and part == 'question':
            return await self.tools(value)
        if scenario == 'C14':
            self.log('input', {**self.checks, 'input': value, 'labelledHistory': value['message']['content'][0]['text'].startswith('Earlier conversation, for context:')})
            self.output({**self.raw['init'], 'session_id': self.conversation})
            if part == 'hold':
                await asyncio.sleep(1.2)
            answer = {**copy.deepcopy(self.raw['text']), 'session_id': self.conversation, 'parent_tool_use_id': None, 'message': {**self.raw['text']['message'], 'content': [{'type': 'text', 'text': 'answer'}]}}
            self.output(answer)
            self.persist(value, answer)
            self.final(part == 'fail', 'Synthetic vendor failure.' if part == 'fail' else 'answer', {'session_id': self.conversation})
            return None
        if scenario in {'C32', 'C7'}:
            self.log('input', {'input': value, 'source': self.raw['source'], 'memoryOff': os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') == '1'})
            self.output(self.raw['init'])
            prompt = value['message']['content'][0]['text']
            if part == 'live':
                if 'AskUserQuestion' in prompt:
                    questions = [{'question': 'Ready?', 'header': 'Ready', 'multiSelect': False, 'options': [{'label': 'Ready'}, {'label': 'Later'}]}]
                    self.call('ask-call', 'AskUserQuestion', {'questions': questions})
                    self.output({'type': 'control_request', 'request_id': 'ask', 'request': {'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'tool_use_id': 'ask-call', 'input': {'questions': questions}}})
                    return None
                if 'sleep 8' in prompt:
                    self.call('stopping-call', 'Bash', {'command': 'sleep 8'})
                    return None
                if 'sub-agent' in prompt:
                    self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_id': 'background', 'task_type': 'local_agent'}]})
                    self.final()
                    self.text('sub-agent ready', 'background-call')
                    self.output({'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': []})
                self.persist()
                reply_text = 'willow' if 'What word' in prompt else 'ready'
                self.text(reply_text)
                self.final(text=reply_text)
                return None
            secret = os.environ.get('ANTHROPIC_AUTH_TOKEN', '') if scenario == 'C7' else ''
            content = secret or ('other' if part == 'build-failed' else 'ready')
            self.delta(content)
            self.text(content)
            self.output({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': content}]}})
            if scenario == 'C7':
                self.call('secret-call', 'Read', {'value': secret})
                self.result('secret-call', {'value': secret}, True)
                if part == 'stderr':
                    sys.stderr.write('Synthetic failure: ' + secret)
                    return 1
            self.final(part == 'failed', content, {**({'api_error_status': 400, 'errors': [content]} if part == 'failed' else {}), **({'modelUsage': {secret: {'costBasis': 'unknown'}}} if secret else {})})
            return None
        self.log('input', {'source': self.raw['source'], 'toolVersion': self.raw['toolVersion'], 'input': value, 'autoMemoryDisabled': os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') == '1', 'streamArguments': '--verbose' in self.args, 'noSessionPersistence': '--no-session-persistence' in self.args})
        if part == 'malformed':
            self.output({'type': 'control_request', 'request': {'subtype': 'unknown'}})
            return None
        prompt = value['message']['content'][0]['text']
        model = self.option('--model')
        self.output({**self.raw['init'], 'model': model or 'glm-fixture' if os.environ.get('ANTHROPIC_BASE_URL') else 'claude-sonnet-5-5' if model == 'sonnet' else 'claude-opus-5-5' if model == 'opus' else 'claude-haiku-4-5-20251001'})
        if part == 'hold' or prompt == 'hold':
            await asyncio.sleep(1.2)
        if scenario == 'C18' and prompt == 'slow':
            for _ in range(32):
                self.text('s' * 64 * 1024)
        else:
            self.delta('an')
            self.text('answer')
        if scenario == 'C42':
            self.output({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'usage': {'input_tokens': 3, 'output_tokens': 2}}}})
            self.output({'type': 'stream_event', 'event': {'type': 'message_stop'}})
        if scenario in {'C11', 'C20', 'C42'}:
            self.call('fixture-call', 'Read' if part == 'structure' else 'Bash', {'command': 'printf fixture-output'})
            if scenario != 'C42':
                tool_content: Any = {'nested': ['😀' * 60000]} if scenario == 'C20' and part == 'structure' else '😀' * 60000 if scenario == 'C20' else 'fixture-output'
                self.result('fixture-call', tool_content)
                self.text('after-tool')
        if scenario == 'C13':
            for input_count, output_count in [(3, 2), (4, 3)]:
                self.output({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'model': self.raw['init']['model'], 'usage': {'input_tokens': input_count}}}})
                self.output({'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': output_count}}})
                self.output({'type': 'stream_event', 'event': {'type': 'message_stop'}})
            result = copy.deepcopy(self.raw['result'])
            result.update(is_error=False, usage={'input_tokens': 7, 'output_tokens': 5}, modelUsage={self.raw['init']['model']: {'costBasis': 'unknown'}})
            result.pop('total_cost_usd', None)
            self.output(result)
        elif scenario == 'C42':
            return 1
        else:
            self.final(scenario == 'C12' or prompt == 'fail', 'Synthetic vendor failure with a success subtype.' if scenario == 'C12' or prompt == 'fail' else 'answer')
        return None


async def stand_in(folder: Path, args: list[str], input_reader: asyncio.StreamReader) -> int:
    # The CLI event writer makes stdout nonblocking. This independent vendor
    # fixture uses TextIO writes, which require a blocking fd for whole JSONL.
    os.set_blocking(sys.stdout.fileno(), True)
    fields = (folder / 'scenario').read_text().split()
    scenario, part = fields[0], fields[1] if len(fields) > 1 else 'supported'
    subscription_scenario = scenario in {'C2', 'C4', 'C5', 'C6', 'C7', 'C8', 'C32', 'C39', 'C40', 'C41'} | RUN_SCENARIOS
    if len(fields) > 2 or not (scenario == 'C31' and part in {'supported', 'old', 'missing'} or subscription_scenario):
        raise Failure('invalid_input', 'This stand-in does not implement that scenario.')
    script = StandIn(folder, args, scenario, part)
    version = '2.1.288' if scenario == 'C31' and part == 'old' else None if scenario == 'C31' and part == 'missing' else '2.1.289'
    home = os.environ.get('CLAUDE_CONFIG_DIR')
    credential = Path(home) / 'stand-in-login.json' if home else None
    logged = credential is not None and credential.is_file()
    checks = {'home': home, 'privateHome': os.getcwd() == home, 'source': script.subscription['source'], 'synthetic': 'Successful login, rejection and unavailable variants of the recorded protocol', 'injectedVariablesAbsent': not any(key in os.environ for key in ['ANTHROPIC_API_KEY', 'CLAUDECODE', 'CLAUDE_SESSION_ID', 'OPENAI_API_KEY', 'CODEX_HOME', 'NODE_OPTIONS']), 'hasCredential': logged} if subscription_scenario else {}
    record = {'scenario': scenario, 'part': part, 'arguments': args, 'environment': sorted(os.environ), 'workingDirectory': os.getcwd(), 'toolVersion': version, 'checks': checks}
    with (folder / 'received.jsonl').open('a') as handle:
        handle.write(compact(record) + '\n')
    if version is None:
        return 127
    if args == ['--version']:
        sys.stdout.write(version + ' (Claude Code)\n')
        return 0
    if '--arbol-unknown-option' in args and scenario == 'C32':
        sys.stderr.write('error: unknown option --arbol-unknown-option\n')
        return 1
    if credential is not None:
        if args == ['auth', 'status', '--json']:
            accepted = logged and part != 'rejected'
            script.output({**script.subscription['status'], 'configDirectory': home, 'projectsDirectory': str(Path(home or '.') / 'projects'), 'loggedIn': accepted, 'authMethod': 'claude.ai' if accepted else 'none', **({'email': 'fixture@example.invalid', 'orgName': 'Fixture organization', 'subscriptionType': 'fixture'} if accepted else {})})
            return 0 if accepted else 1
        if args == ['auth', 'logout']:
            credential.unlink(missing_ok=True)
            return 0
        if args == ['auth', 'login', '--claudeai']:
            url = 'https://claude.com/cai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A12345%2Fcallback&state=fixture'
            code = 'https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fixture'
            sys.stdout.write(f"Opening browser to sign in…\nIf the browser didn't open, visit: {code}\nPaste code here if prompted > ")
            sys.stdout.flush()
            if not os.environ.get('BROWSER'):
                return 1
            await child_command(os.environ['BROWSER'], [url], dict(os.environ), home or '.')
            if part == 'waiting':
                await asyncio.Event().wait()
            if part == 'by-code' and (await input_reader.readline()).decode().strip() != 'fixture-code':
                return 1
            if part == 'failed':
                return 1
            credential.write_text('{"credential":"fixture-vendor-secret"}')
            credential.chmod(0o600)
            sys.stdout.write('Login successful.\n')
            return 0
    if '--input-format' in args:
        if part == 'unavailable':
            return 1
        prepared = await script.prepare()
        if prepared is not None:
            return prepared
        if part == 'stubborn':
            signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            while line := await input_reader.readline():
                value = loads(line)
                if value.get('type') == 'control_request':
                    if scenario not in RUN_SCENARIOS:
                        if value.get('request', {}).get('subtype') == 'initialize':
                            response = copy.deepcopy(script.subscription['initialize'])
                            response['response']['request_id'] = value['request_id']
                            script.output(response)
                            return 0
                    else:
                        result = await script.control(value)
                        if result is not None:
                            return result
                elif value.get('type') == 'control_response':
                    await script.response(value)
                elif value.get('type') == 'user':
                    result = await script.user(value)
                    if result is not None:
                        return result
            if part == 'linger':
                await asyncio.Event().wait()
            return 0
        finally:
            for task in script.tasks:
                task.cancel()
            await asyncio.gather(*script.tasks, return_exceptions=True)
            if scenario in {'C9', 'C25', 'C35', 'C36', 'C37'}:
                script.log('stdin-closed', {'pending': list(script.pending)})
    raise Failure('unsupported', 'This stand-in currently implements only the version probe.')
