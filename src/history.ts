import { createHash, randomBytes, randomUUID } from 'node:crypto';
import type { Image } from './run-request.js';
export type HistoryItem = { type: 'message'; role: 'user' | 'assistant'; text: string; images?: Image[] }
  | { type: 'tool_call'; name: string; input: unknown; ok: boolean; result?: unknown; error?: string };
export const digest = (text: string | Buffer): string => createHash('sha256').update(text).digest('hex');
export const chatKey = digest;

// Append escaped text to an open JSON string: consecutive assistant blocks need no reply buffer.
export class MessageDigest {
  private hash = createHash('sha256').update('[');
  private assistant = false;
  messages = 0;
  add(item: HistoryItem): void {
    if (item.type !== 'message') return;
    if (item.role === 'assistant') {
      if (!this.assistant) { this.separator(); this.hash.update('["assistant","'); this.assistant = true; this.messages++; }
      this.hash.update(JSON.stringify(item.text).slice(1, -1));
    } else {
      this.separator(); this.hash.update(JSON.stringify(['user', item.text,
        (item.images ?? []).map(image => [image.mediaType, digest(Buffer.from(image.data, 'base64'))])]));
      this.messages++;
    }
  }
  private separator(): void {
    if (this.assistant) { this.hash.update('"]'); this.assistant = false; }
    if (this.messages) this.hash.update(',');
  }
  snapshot(): { messages: number; digest: string } {
    return { messages: this.messages, digest: this.hash.copy().update(this.assistant ? '"]]' : ']').digest('hex') };
  }
}
export function messageDigest(items: HistoryItem[]): { messages: number; digest: string } {
  const hash = new MessageDigest(); for (const item of items) hash.add(item); return hash.snapshot();
}
export function resultText(item: Extract<HistoryItem, { type: 'tool_call' }>): string {
  let text = '';
  if (typeof item.result === 'string') text = item.result;
  else if (item.result !== undefined) {
    if (item.name === 'shell' && item.result !== null && typeof item.result === 'object' && 'output' in item.result) {
      const result = item.result as { output: unknown; exitCode?: unknown };
      text = typeof result.output === 'string' ? result.output : JSON.stringify(result.output) ?? '';
      if (typeof result.exitCode === 'number' && result.exitCode !== 0) text += `\nexit code: ${result.exitCode}`;
    } else text = JSON.stringify(item.result);
  }
  if (!item.ok && item.error) text += (text ? '\n' : '') + item.error;
  return text;
}
export const imageBlocks = (images: Image[] = []): object[] => images.map(image => ({ type: 'image',
  source: { type: 'base64', media_type: image.mediaType, data: image.data } }));
export function sessionLines(history: HistoryItem[], conversation: string, cwd: string, now = Date.now()): object[] {
  const content: { role: 'user' | 'assistant'; blocks: object[] }[] = [];
  for (const item of history) {
    if (item.type === 'message') content.push({ role: item.role, blocks: [{ type: 'text', text: item.text }, ...imageBlocks(item.images)] });
    else {
      const id = 'toolu_' + randomBytes(12).toString('hex');
      const input = item.name === 'shell' ? { command: (item.input as { command: string }).command } : item.input;
      content.push({ role: 'assistant', blocks: [{ type: 'tool_use', id, name: item.name === 'shell' ? 'Bash' : item.name, input }] });
      content.push({ role: 'user', blocks: [{ type: 'tool_result', tool_use_id: id, content: resultText(item), ...(!item.ok ? { is_error: true } : {}) }] });
    }
  }
  let parent: string | null = null;
  return content.map((item, index) => {
    const uuid = randomUUID();
    const line = { type: item.role, message: { role: item.role, content: item.blocks }, uuid, parentUuid: parent,
      timestamp: new Date(now - 60000 - (content.length - index - 1) * 1000).toISOString(), sessionId: conversation,
      cwd, isSidechain: false, userType: 'external', version: 'synthetic-arbol' };
    parent = uuid; return line;
  });
}
export function textHistory(history: HistoryItem[], input: { text: string; images?: Image[] }): { text: string; images: Image[] } {
  const parts = ['Earlier conversation, for context:\n']; const images: Image[] = [];
  for (const item of history) {
    if (item.type === 'message') {
      parts.push(`[${item.role === 'user' ? 'User' : 'Assistant'}]\n${item.text}` +
        (item.images?.length ? `\n[Images: ${item.images.length}]` : ''));
      images.push(...(item.images ?? []));
    } else parts.push(`[Tool call: ${item.name}]\n${JSON.stringify(item.input)}\n[Tool ${item.ok ? 'result' : 'error'}]\n${resultText(item)}`);
  }
  return { text: parts.join('\n') + '\n\nNew message:\n\n' + input.text, images: [...images, ...(input.images ?? [])] };
}
