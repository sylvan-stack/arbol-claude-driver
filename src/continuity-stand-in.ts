import { appendFile, mkdir, readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';
export async function continuityStandIn(folder: string, args: string[], part: string): Promise<number> {
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const probe = JSON.parse(await readFile(new URL('../../test/continuity-probe-2026-10-07.json', import.meta.url), 'utf8'));
  const option = (name: string): string | undefined => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
  const resumed = option('--resume'); const conversation = resumed ?? option('--session-id')!;
  const output = (record: object): void => { process.stdout.write(JSON.stringify(record) + '\n'); };
  if (resumed && (part === 'refuse' || part === 'refuse-stderr')) {
    const record = structuredClone(probe.missingConversation.records[0]); record.session_id = conversation;
    record.errors = [`No conversation found with session ID: ${conversation}`];
    if (part !== 'refuse-stderr') output(record);
    process.stderr.write(record.errors[0] + '\n'); return 1;
  }
  const projects = join(process.env.CLAUDE_CONFIG_DIR!, 'projects');
  let file: string | undefined;
  const folders = await readdir(projects).catch(() => []);
  for (const name of folders) {
    const candidate = join(projects, name, conversation + '.jsonl');
    if (await readFile(candidate).then(() => true).catch(() => false)) { file = candidate; break; }
  }
  const history = file ? (await readFile(file, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) : [];
  const blocks = history.flatMap(line => Array.isArray(line.message?.content) ? line.message.content : []);
  const calls = blocks.filter(block => block.type === 'tool_use');
  const results = blocks.filter(block => block.type === 'tool_result');
  const nativeShell = calls.find(call => call.name === 'Bash' && call.input.command === 'printf history-shell-output');
  const shellResult = nativeShell && results.find(result => result.tool_use_id === nativeShell.id);
  const foreign = calls.find(call => call.name === 'foreign_tool' && call.input.value === 'foreign-input');
  const checks = { source: probe.check, toolVersion: raw.toolVersion, nativeShellCall: !!nativeShell,
    nativeShellResult: shellResult?.content, nativeShellFailed: shellResult?.is_error === true,
    nativeForeignCall: !!foreign && results.some(result => result.tool_use_id === foreign.id && result.content === '{"value":"foreign-value"}'),
    historyTexts: blocks.filter(block => block.type === 'text').map(block => block.text),
    historyImages: blocks.filter(block => block.type === 'image').map(block => ({ mediaType: block.source.media_type, data: block.source.data })),
    nativeHistory: !!file, systemPrompt: option('--append-system-prompt') ?? '', promptSnapshotOff: option('--system-prompt-snapshot') === 'off' };
  const common = { scenario: 'C14', part, arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd() };
  const lines = createInterface({ input: process.stdin });
  for await (const line of lines) {
    const input = JSON.parse(line);
    if (input.type === 'control_request') {
      if (input.request.subtype === 'initialize') { const record = structuredClone(subscriptionProtocol.initialize); record.response.request_id = input.request_id; output(record); }
      else if (input.request.subtype === 'get_settings') output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id,
        response: { applied: { model: option('--model') ?? 'default', effort: option('--effort') ?? 'high' } } } });
      else if (input.request.subtype === 'interrupt') { output({ ...raw.result, is_error: true }); return 1; }
    } else if (input.type === 'user') {
      await appendFile(join(folder, 'received.jsonl'), JSON.stringify({ ...common, stage: 'input', checks: { ...checks, input,
        labelledHistory: input.message.content[0].text.startsWith('Earlier conversation, for context:') } }) + '\n');
      output({ ...raw.init, session_id: conversation });
      if (part === 'hold') await new Promise(resolve => setTimeout(resolve, 1200));
      const answer = { ...structuredClone(raw.text), session_id: conversation, parent_tool_use_id: null,
        message: { ...raw.text.message, content: [{ type: 'text', text: 'answer' }] } };
      output(answer);
      // Emulate the vendor's session file, independently of the Driver's history writer.
      if (!args.includes('--no-session-persistence')) {
        if (!file) {
          const name = process.cwd().replace(/[^a-zA-Z0-9]/g, '-');
          const directory = join(projects, name.length > 200 ? name.slice(0, 200) + '-abcdef' : name);
          await mkdir(directory, { recursive: true, mode: 0o700 }); file = join(directory, conversation + '.jsonl');
        }
        let parent = history.at(-1)?.uuid ?? null;
        for (const item of [input, answer]) {
          const uuid = randomUUID();
          await appendFile(file, JSON.stringify({ type: item.type, message: item.message, uuid, parentUuid: parent, sessionId: conversation,
            cwd: process.cwd(), isSidechain: false, userType: 'external', version: raw.toolVersion, timestamp: new Date().toISOString() }) + '\n', { mode: 0o600 });
          parent = uuid;
        }
      }
      output({ ...raw.result, session_id: conversation, is_error: part === 'fail', result: part === 'fail' ? 'Synthetic vendor failure.' : 'answer' });
    }
  }
  return 0;
}
