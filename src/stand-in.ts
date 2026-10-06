import { readFile, appendFile } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure } from './errors.js';
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
  const known = scenario === 'C31' && ['supported', 'old', 'missing'].includes(part) || scenario === 'C32' && part === 'info';
  if (extra.length || !known) throw new Failure('invalid_input', 'This stand-in supports C31 supported, old and missing, and C32 info.');
  const script = scenario === 'C32' ? scripts.C32.info : scripts.C31[part as keyof typeof scripts.C31];
  await appendFile(join(folder, 'received.jsonl'), JSON.stringify({
    scenario, part, arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd(),
    toolVersion: script.version ?? null, checks: {},
  }) + '\n', { mode: 0o600 });
  if (!script.version) return 127;
  if (args.length === 1 && args[0] === '--version') {
    process.stdout.write(`${script.version} (Claude Code)\n`);
    return 0;
  }
  throw new Failure('unsupported', 'This stand-in currently implements only the version probe.');
}
