import { randomUUID } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { userInfo } from 'node:os';
import { childCommand, startChild } from './child.js';
import { cleanEnvironment } from './environment.js';
import { Failure } from './errors.js';
import { inspectTool, requireTool } from './tool.js';
import { isObject } from './settings.js';
import { ownerSkills, ownerMcpArguments } from './owner-tools.js';
import { atomicWrite, privateDirectory } from './state.js';
import { sessionLines } from './history.js';
import { encodeFolder } from './continuity.js';
import type { Subscriptions } from './subscriptions.js';
import type { ProcessEnv } from './types.js';

const enumerated = new Set(['control_response', 'result', 'success', 'error', 'error_during_execution', 'none', 'claude.ai',
  'firstParty', 'bypassPermissions', 'connected', 'failed', 'pending', 'needs-auth', 'low', 'medium', 'high', 'xhigh', 'max']);
// Probe records retain keys and types, never account, address, model or credential values.
export function probeShape(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(probeShape);
  if (isObject(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
    ['type', 'subtype', 'authMethod', 'apiProvider', 'current_permission_mode', 'status', 'effort'].includes(key) &&
    typeof item === 'string' && enumerated.has(item) ? item : probeShape(item)]));
  return value === null ? null : typeof value;
}
function verify(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Failure('tool_unsupported', message);
}
const base = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'bypassPermissions', '--system-prompt-snapshot', 'off'];

export async function probe(subscriptions: Subscriptions, inherited: ProcessEnv, signal: AbortSignal) {
  const tool = await inspectTool(subscriptions.state, subscriptions.settings.toolPath, inherited, signal); requireTool(tool);
  const tempRoot = join(subscriptions.state.directory, 'tmp'); await privateDirectory(tempRoot);
  const directory = await mkdtemp(join(tempRoot, 'probe-'));
  const checks: Array<{ check: string; outcome: string; [key: string]: unknown }> = [];
  const report = { check: 'dev.probe', at: new Date().toISOString(), toolVersion: tool.version!, inferenceRequests: 0, checks };
  const actualHome = userInfo().homedir;
  const environment = (config: string): ProcessEnv => ({ ...cleanEnvironment(inherited, actualHome, config),
    PATH: dirname(process.execPath) + ':/usr/bin:/bin', TMPDIR: directory, CLAUDE_CODE_DISABLE_AUTO_MEMORY: '1', CLAUDE_CODE_MAX_RETRIES: '3' });
  const home = async (name: string): Promise<string> => { const path = join(directory, name); await privateDirectory(path); return path; };
  const command = (config: string, args: string[], input?: string) => childCommand(tool.path!, args, environment(config), config, 25000, signal, input, true);
  async function controls(config: string, args: string[], requests: string[]) {
    const child = startChild(tool.path!, args, environment(config), config, 25000, signal);
    const records: Record<string, any>[] = [];
    let remainder = '';
    const waiters = new Map<string, (record: Record<string, any>) => void>();
    child.process.stdout.setEncoding('utf8');
    child.process.stdout.on('data', (chunk: string) => {
      remainder += chunk;
      let end: number;
      while ((end = remainder.indexOf('\n')) >= 0) {
        const line = remainder.slice(0, end); remainder = remainder.slice(end + 1);
        let value: unknown; try { value = JSON.parse(line); } catch { continue; }
        if (isObject(value)) { records.push(value); if (isObject(value.response) && typeof value.response.request_id === 'string') waiters.get(value.response.request_id)?.(value); }
      }
    });
    try {
      for (const subtype of requests) {
        const id = randomUUID();
        const reply = new Promise<Record<string, any>>(resolve => waiters.set(id, resolve));
        // This is the only input of a control probe. No user record is ever written.
        child.process.stdin.write(JSON.stringify({ type: 'control_request', request_id: id, request: { subtype } }) + '\n');
        const record = await Promise.race([reply, child.done.then(() => undefined)]);
        verify(record?.response?.subtype === 'success', `Claude did not accept the ${subtype} probe.`);
        waiters.delete(id);
      }
      child.process.stdin.end(); verify((await child.done).code === 0, 'Claude did not end its control probe successfully.');
      return records;
    } finally { await child.stop(); }
  }
  let outcome = 'passed';
  try {
    const fresh = await home('fresh');
    const version = await command(fresh, ['--version']);
    verify(version.code === 0 && version.stdout.trim() === `${tool.version} (Claude Code)`, 'Claude changed its version record.');
    checks.push({ check: 'probe.base-info', outcome: 'passed', versionLine: `${tool.version} (Claude Code)` });

    const status = await command(fresh, ['auth', 'status', '--json']);
    const freshStatus = JSON.parse(status.stdout);
    verify(freshStatus.loggedIn === false, 'A disposable Claude home unexpectedly has a login.');
    const catalog = await controls(fresh, base, ['initialize']);
    verify(catalog[0]?.response?.response?.models?.length > 0, 'Claude returned no models without a prompt.');
    checks.push({ check: 'probe.subscriptions', outcome: 'passed', freshStatus: probeShape(freshStatus), initialize: probeShape(catalog) });
    const testing = await subscriptions.list().then(items => items.find(item => item.id === 'test-anthropic' && item.state === 'signed_in'));
    if (testing) {
      const signedHome = join(subscriptions.state.subscription(testing.id), 'home');
      const status = await command(signedHome, ['auth', 'status', '--json']);
      verify(JSON.parse(status.stdout).loggedIn === true, 'The test Subscription login is unavailable.');
      checks.push({ check: 'probe.signed-subscription', outcome: 'passed', status: probeShape(JSON.parse(status.stdout)),
        initialize: probeShape(await controls(signedHome, base, ['initialize'])) });
    }
    // A disposable browser command captures only the URL structure. It never opens a browser.
    const loginHome = await home('login'), address = join(directory, 'address'), browser = join(directory, 'browser');
    await writeFile(browser, `#!${process.execPath}\nimport {writeFileSync} from 'node:fs';\nwriteFileSync(${JSON.stringify(address)}, process.argv[2], {mode:0o600});\n`, { mode: 0o700 });
    const login = startChild(tool.path!, ['auth', 'login', '--claudeai'], { ...environment(loginHome), BROWSER: browser }, loginHome, 20000, signal);
    try {
      let url: string | undefined;
      const deadline = performance.now() + 12000;
      while (!url && performance.now() < deadline) {
        signal.throwIfAborted(); url = await readFile(address, 'utf8').catch(() => undefined);
        if (!url) await new Promise(resolve => setTimeout(resolve, 50));
      }
      verify(url, 'Claude did not hand off its login address.');
      const parsed = new URL(url); verify(['claude.com', 'claude.ai'].includes(parsed.hostname), 'Claude handed off an unexpected login host.');
      checks.push({ check: 'probe.browser-handoff', outcome: 'passed', protocol: parsed.protocol, host: parsed.hostname, queryKeys: [...parsed.searchParams.keys()].sort() });
    } finally { await login.stop(); }

    const settings = await controls(fresh, [...base, '--tools', '', '--model', 'sonnet', '--effort', 'low', '--no-session-persistence'], ['get_settings']);
    verify(settings[0]?.response?.response?.applied?.effort === 'low', 'Claude did not apply the requested probe effort.');
    checks.push({ check: 'probe.run-settings', outcome: 'passed', records: probeShape(settings) });

    const missing = await command(fresh, [...base, '--tools', '', '--resume', randomUUID()]);
    verify(missing.code === 1 && /No conversation found/.test(missing.stdout + missing.stderr), 'Claude changed its missing-conversation refusal.');
    const session = randomUUID(), file = join(fresh, 'projects', encodeFolder(fresh), session + '.jsonl');
    await atomicWrite(file, sessionLines([{ type: 'message', role: 'user', text: 'Synthetic history, never submitted for inference.' },
      { type: 'tool_call', name: 'shell', input: { command: 'printf fixture' }, ok: false, result: { output: 'fixture' } }], session, fresh).map(record => JSON.stringify(record)).join('\n') + '\n');
    const resumed = await controls(fresh, [...base, '--tools', '', '--resume', session], ['initialize']);
    checks.push({ check: 'probe.continuity', outcome: 'passed', missingExit: missing.code,
      missingRecord: probeShape(JSON.parse(missing.stdout.trim())), resumed: probeShape(resumed) });

    const stop = performance.now(); const interrupted = await controls(fresh, base, ['initialize', 'interrupt']);
    checks.push({ check: 'probe.stopping', outcome: 'passed', elapsedMs: Math.round(performance.now() - stop), records: probeShape(interrupted) });
    const toolsHome = await home('tools'); await ownerSkills(toolsHome, inherited.HOME || actualHome);
    const tools = await controls(toolsHome, [...base, '--permission-prompt-tool', 'stdio', ...await ownerMcpArguments(inherited.HOME || actualHome)], ['initialize', 'mcp_status']);
    verify(tools[0]?.response?.response?.current_permission_mode === 'bypassPermissions', 'Claude did not apply trusted mode.');
    checks.push({ check: 'probe.tools', outcome: 'passed', records: probeShape(tools) });

    const argument = await command(fresh, ['--arbol-unknown-option']);
    verify(argument.code !== 0 && /unknown option|unknown argument/i.test(argument.stderr ?? ''), 'Claude changed its argument-error refusal.');
    checks.push({ check: 'probe.argument-errors', outcome: 'passed', exit: argument.code, stderr: 'Unknown option diagnostic on stderr; no protocol result.' });
  } catch (error) {
    outcome = 'failed'; checks.push({ check: 'probe.failure', outcome: 'failed' }); throw error;
  } finally {
    await rm(directory, { recursive: true, force: true });
    const research = join(subscriptions.state.directory, 'research'); await mkdir(research, { recursive: true, mode: 0o700 });
    const path = join(research, `probe-${report.at.replaceAll(':', '-')}.json`);
    await writeFile(path, JSON.stringify({ ...report, outcome }, null, 2) + '\n', { mode: 0o600 });
  }
  return { ...report, outcome };
}
