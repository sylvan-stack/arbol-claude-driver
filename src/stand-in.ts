import { readFile, appendFile, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure } from './errors.js';
import { childCommand } from './child.js';
import { subscriptionProtocol } from './subscription-protocol.js';
import { createInterface } from 'node:readline';
// The version line is captured by test/probe-info.mjs against the installed tool.
// No protocol records are invented here. Later tasks add scripts from raw probes.
export const scripts = {
  C31: {
    supported: { version: '2.1.289', source: '~/Artifacts/arbol-claude-driver/research/base-info-2026-10-06.json' },
    old: { version: '2.1.288', source: 'Synthetic older version for the C31 boundary check' },
    missing: { version: undefined, source: 'Synthetic absent executable for the C31 boundary check' },
  },
  C32: {
    info: { version: '2.1.289', source: '~/Artifacts/arbol-claude-driver/research/base-info-2026-10-06.json' },
  },
};
export async function standIn(folder: string, args: string[]): Promise<number> {
  const [scenario, part = 'supported', ...extra] = (await readFile(join(folder, 'scenario'), 'utf8')).trim().split(/\s+/);
  const subscriptionScenario = ['C2', 'C4', 'C5', 'C6', 'C7', 'C8', 'C39', 'C40', 'C41'].includes(scenario!);
  const known = scenario === 'C31' && ['supported', 'old', 'missing'].includes(part) || scenario === 'C32' && part === 'info' || subscriptionScenario;
  if (extra.length || !known) throw new Failure('invalid_input', 'This stand-in does not implement that scenario.');
  const script = subscriptionScenario ? scripts.C31.supported : scenario === 'C32' ? scripts.C32.info : scripts.C31[part as keyof typeof scripts.C31];
  const home = process.env.CLAUDE_CONFIG_DIR;
  const credential = home ? join(home, 'stand-in-login.json') : undefined;
  const loggedIn = credential ? await readFile(credential, 'utf8').then(() => true).catch(() => false) : false;
  await appendFile(join(folder, 'received.jsonl'), JSON.stringify({
    scenario, part, arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd(),
    toolVersion: script.version ?? null, checks: subscriptionScenario ? {
      home: home ?? null, privateHome: process.cwd() === home, source: subscriptionProtocol.source,
      synthetic: 'Successful login, rejection and unavailable variants of the recorded protocol',
      injectedVariablesAbsent: ['ANTHROPIC_API_KEY', 'CLAUDECODE', 'CLAUDE_SESSION_ID', 'OPENAI_API_KEY', 'CODEX_HOME', 'NODE_OPTIONS'].every(name => process.env[name] === undefined),
      hasCredential: loggedIn,
    } : {},
  }) + '\n', { mode: 0o600 });
  if (!script.version) return 127;
  if (args.length === 1 && args[0] === '--version') {
    process.stdout.write(`${script.version} (Claude Code)\n`);
    return 0;
  }
  if (subscriptionScenario && home && credential) {
    if (args.join(' ') === 'auth status --json') {
      const accepted = loggedIn && part !== 'rejected';
      process.stdout.write(JSON.stringify({ ...subscriptionProtocol.status, configDirectory: home, projectsDirectory: join(home, 'projects'),
        loggedIn: accepted, authMethod: accepted ? 'claude.ai' : 'none', ...(accepted ? { email: 'fixture@example.invalid', orgName: 'Fixture organization', subscriptionType: 'fixture' } : {}) }) + '\n');
      return accepted ? 0 : 1;
    }
    if (args.join(' ') === 'auth logout') { await rm(credential, { force: true }); return 0; }
    if (args.join(' ') === 'auth login --claudeai') {
      // URLs and terminal prompt are from probe.subscriptions; ephemeral values are synthetic.
      const url = 'https://claude.com/cai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A12345%2Fcallback&state=fixture';
      const codeUrl = 'https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fixture';
      process.stdout.write(`Opening browser to sign in…\nIf the browser didn't open, visit: ${codeUrl}\nPaste code here if prompted > `);
      if (!process.env.BROWSER) return 1;
      await childCommand(process.env.BROWSER, [url], process.env, home);
      if (part === 'waiting') { process.once('SIGTERM', () => { process.exit(143); }); await new Promise<void>(() => { setInterval(() => {}, 60000); }); return 1; }
      if (part === 'by-code') {
        const lines = createInterface({ input: process.stdin });
        const value = await lines[Symbol.asyncIterator]().next(); lines.close();
        if (value.value !== 'fixture-code') return 1;
      }
      if (part === 'failed') return 1;
      await writeFile(credential, JSON.stringify({ credential: 'fixture-vendor-secret' }), { mode: 0o600 });
      process.stdout.write('Login successful.\n'); return 0;
    }
    if (args.includes('--input-format')) {
      if (part === 'unavailable') return 1;
      const lines = createInterface({ input: process.stdin });
      for await (const line of lines) {
        const input = JSON.parse(line);
        if (input.type === 'control_request' && input.request?.subtype === 'initialize') {
          const record = structuredClone(subscriptionProtocol.initialize) as any;
          record.response.request_id = input.request_id;
          process.stdout.write(JSON.stringify(record) + '\n');
          break;
        }
      }
      lines.close(); return 0;
    }
  }
  throw new Failure('unsupported', 'This stand-in currently implements only the version probe.');
}
