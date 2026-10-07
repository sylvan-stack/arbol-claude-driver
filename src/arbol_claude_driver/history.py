"""Native history construction and byte-compatible continuity fingerprints."""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
import uuid

from .core import Record, compact, now, utf8


def digest(value: str | bytes) -> str:
    return hashlib.sha256(utf8(value) if isinstance(value, str) else value).hexdigest()


class MessageDigest:
    def __init__(self) -> None:
        self.hash = hashlib.sha256(b'[')
        self.assistant = False
        self.messages = 0
        self.trailing = ""

    def separator(self) -> None:
        if self.trailing:
            self.hash.update(utf8(compact(self.trailing)[1:-1]))
            self.trailing = ""
        if self.assistant:
            self.hash.update(b'\"]')
            self.assistant = False
        if self.messages:
            self.hash.update(b',')

    def add(self, item: Record) -> None:
        if item.get('type') != 'message':
            return
        if item['role'] == 'assistant':
            if not self.assistant:
                self.separator()
                self.hash.update(b'["assistant","')
                self.assistant = True
                self.messages += 1
            text = self.trailing + item['text']
            self.trailing = text[-1] if text and 0xD800 <= ord(text[-1]) <= 0xDBFF else ''
            if self.trailing:
                text = text[:-1]
            self.hash.update(utf8(compact(text)[1:-1]))
        else:
            self.separator()
            images = [[image['mediaType'], digest(base64.b64decode(image['data']))] for image in item.get('images', [])]
            self.hash.update(utf8(compact(['user', item['text'], images])))
            self.messages += 1

    def snapshot(self) -> Record:
        clone = self.hash.copy()
        if self.trailing:
            clone.update(utf8(compact(self.trailing)[1:-1]))
        clone.update(b'\"]]' if self.assistant else b']')
        return {'messages': self.messages, 'digest': clone.hexdigest()}


def message_digest(items: list[Record]) -> Record:
    value = MessageDigest()
    for item in items:
        value.add(item)
    return value.snapshot()


def result_text(item: Record) -> str:
    result = item.get('result')
    text = ''
    if isinstance(result, str):
        text = result
    elif 'result' in item:
        if item['name'] == 'shell' and isinstance(result, dict) and 'output' in result:
            text = result['output'] if isinstance(result['output'], str) else compact(result['output'])
            if type(result.get('exitCode')) in (int, float) and result['exitCode'] != 0:
                text += f'\nexit code: {result["exitCode"]}'
        else:
            text = compact(result)
    if not item['ok'] and item.get('error'):
        text += ('\n' if text else '') + item['error']
    return text


def image_blocks(images: list[Record] | None = None) -> list[Record]:
    return [{'type': 'image', 'source': {'type': 'base64', 'media_type': image['mediaType'], 'data': image['data']}} for image in images or []]


def session_lines(history: list[Record], conversation: str, cwd: str, timestamp: float | None = None) -> list[Record]:
    content: list[tuple[str, list[Record]]] = []
    for item in history:
        if item['type'] == 'message':
            content.append((item['role'], [{'type': 'text', 'text': item['text']}, *image_blocks(item.get('images'))]))
        else:
            identifier = 'toolu_' + secrets.token_hex(12)
            shell = item['name'] == 'shell'
            content.append(('assistant', [{'type': 'tool_use', 'id': identifier, 'name': 'Bash' if shell else item['name'], 'input': {'command': item['input']['command']} if shell else item['input']}]))
            content.append(('user', [{'type': 'tool_result', 'tool_use_id': identifier, 'content': result_text(item), **({'is_error': True} if not item['ok'] else {})}]))
    parent: str | None = None
    lines = []
    current = time.time() if timestamp is None else timestamp
    for index, (role, blocks) in enumerate(content):
        identifier = str(uuid.uuid4())
        lines.append({'type': role, 'message': {'role': role, 'content': blocks}, 'uuid': identifier, 'parentUuid': parent, 'timestamp': now(current - 60 - (len(content) - index - 1)), 'sessionId': conversation, 'cwd': cwd, 'isSidechain': False, 'userType': 'external', 'version': 'synthetic-arbol'})
        parent = identifier
    return lines


def text_history(history: list[Record], message: Record) -> Record:
    parts = ['Earlier conversation, for context:\n']
    images = []
    for item in history:
        if item['type'] == 'message':
            parts.append(f'[{"User" if item["role"] == "user" else "Assistant"}]\n{item["text"]}' + (f'\n[Images: {len(item["images"])}]' if item.get('images') else ''))
            images += item.get('images', [])
        else:
            parts.append(f'[Tool call: {item["name"]}]\n{compact(item["input"])}\n[Tool {"result" if item["ok"] else "error"}]\n{result_text(item)}')
    return {'text': '\n'.join(parts) + '\n\nNew message:\n\n' + message['text'], 'images': [*images, *message.get('images', [])]}


def encode_folder(cwd: str) -> str:
    units = cwd.encode('utf-16-le', 'surrogatepass')
    return ''.join(chr(value) if re.fullmatch('[a-zA-Z0-9]', chr(value)) else '-' for value in (int.from_bytes(units[i:i+2], 'little') for i in range(0, len(units), 2)))
