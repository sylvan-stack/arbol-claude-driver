import { randomUUID } from 'node:crypto';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startChild } from './child.js';
import { cleanEnvironment } from './environment.js';
import { Failure, safeFailure } from './errors.js';
import { inspectTool, requireTool } from './tool.js';
import { privateDirectory } from './state.js';
import { addUsage } from './run-events.js';
import type { RunUsage } from './run-events.js';
import type { Subscriptions, UsageReport } from './subscriptions.js';
import type { ProcessEnv } from './types.js';

export const testSubscriptionId = 'test-anthropic';
export async function checkAccount(subscriptions: Subscriptions): Promise<UsageReport> {
  const account = await subscriptions.requireSignedIn(testSubscriptionId);
  if (account.family !== 'anthropic') throw new Failure('invalid_input', 'test-anthropic must belong to the anthropic family.');
  const usage = account.lastUsage ?? { outcome: 'unsupported' as const, fetchedAt: new Date().toISOString(), windows: [] };
  if (usage.windows.some(window => window.limitReached || (window.usedPercent ?? 0) >= 100)) {
    throw new Failure('rate_limited', 'The test Subscription has a usage window used up. Wait before running the check.');
  }
  return usage;
}
export function usageChange(before: UsageReport, after: UsageReport) {
  return after.windows.map(window => {
    const previous = before.windows.find(item => item.kind === window.kind && item.label === window.label);
    return { kind: window.kind, label: window.label,
      ...(previous?.usedPercent === undefined ? {} : { beforePercent: previous.usedPercent }),
      ...(window.usedPercent === undefined ? {} : { afterPercent: window.usedPercent }),
      ...(previous?.usedPercent === undefined || window.usedPercent === undefined ? {} : { changePercent: Math.round((window.usedPercent - previous.usedPercent) * 1000) / 1000 }) };
  });
}

export async function liveCheck(buildOnly: boolean, subscriptions: Subscriptions, inherited: ProcessEnv, signal: AbortSignal) {
  // These refusals happen before a version probe or any tool launch.
  const usageBefore = await checkAccount(subscriptions);
  const tool = await inspectTool(subscriptions.state, subscriptions.settings.toolPath, inherited, signal); requireTool(tool);
  const tempRoot = join(subscriptions.state.directory, 'tmp'); await privateDirectory(tempRoot);
  const directory = await mkdtemp(join(tempRoot, buildOnly ? 'build-check-' : 'live-check-'));
  const cli = fileURLToPath(new URL('../../bin/arbol-claude-driver.mjs', import.meta.url));
  const env = { ...cleanEnvironment(inherited, inherited.HOME!), PATH: dirname(process.execPath) + ':' + dirname(tool.path!) + ':/usr/bin:/bin',
    ...(inherited.ARBOL_TEST_TIME_SCALE ? { ARBOL_TEST_TIME_SCALE: inherited.ARBOL_TEST_TIME_SCALE } : {}) };
  const checks: Array<Record<string, unknown>> = [];
  const report = { check: buildOnly ? 'dev.build-check' : 'dev.live-check', at: new Date().toISOString(), toolVersion: tool.version!,
    subscriptionId: testSubscriptionId, model: 'haiku', turnsAttempted: 0, inferenceRequests: 0, tokenUsage: {} as RunUsage, usageBefore, checks };
  let status = 0, error: unknown;
  async function message(name: string, prompt: string, options: { tools?: boolean; control?: boolean; stop?: boolean; chat?: string; history?: unknown[] } = {}) {
    signal.throwIfAborted(); await checkAccount(subscriptions);
    const request = join(directory, randomUUID() + '.json');
    await writeFile(request, JSON.stringify({ subscriptionId: testSubscriptionId, model: 'haiku', tools: options.tools ? 'all' : 'none',
      ...(options.tools || options.chat ? { workingDirectory: directory } : {}), ...(options.chat ? { chatSessionId: options.chat } : {}),
      history: options.history ?? [], input: { role: 'user', text: prompt }, limits: { totalSeconds: 60 } }), { mode: 0o600 });
    const child = startChild(process.execPath, [cli, 'run', '--request', request, '--state-dir', subscriptions.state.directory,
      ...(options.control || options.stop ? ['--control'] : [])], env, directory, 80000, signal);
    report.turnsAttempted++;
    let remainder = '', stopped = false, started = false, recorded = false;
    const events: Record<string, any>[] = [];
    child.process.stdout.setEncoding('utf8');
    child.process.stdout.on('data', (chunk: string) => {
      remainder += chunk; let end: number;
      while ((end = remainder.indexOf('\n')) >= 0) {
        const line = remainder.slice(0, end); remainder = remainder.slice(end + 1);
        let record: Record<string, any>; try { record = JSON.parse(line); } catch { continue; }
        events.push(record);
        if (record.type === 'started' && !started) { started = true; report.inferenceRequests++; }
        if (record.type === 'ask_opened' && !options.stop) {
          const answers = Object.fromEntries(record.ask.questions.map((question: any) => [question.id, [question.options[0].label]]));
          child.process.stdin.write(JSON.stringify({ type: 'answer', askId: record.askId, answer: { type: 'answers', answers } }) + '\n');
        }
        if (options.stop && record.type === 'tool_started' && !stopped) {
          stopped = true; child.process.stdin.write(JSON.stringify({ type: 'stop' }) + '\n');
        }
        if (record.type === 'finished') child.process.stdin.end();
      }
    });
    if (!options.control && !options.stop) child.process.stdin.end();
    function recordResult(exit: number) {
      recorded = true;
      const final = events.at(-1), answer = events.filter(record => record.type === 'text' && !record.parentCallId).map(record => record.text).join('');
      const usage: RunUsage = final?.usage ?? events.filter(record => record.type === 'usage').at(-1)?.usage ?? {};
      report.tokenUsage = addUsage(report.tokenUsage, usage);
      if (usage.costUsd !== undefined) report.tokenUsage.costUsd = (report.tokenUsage.costUsd ?? 0) + usage.costUsd;
      checks.push({ check: name, outcome: final?.outcome ?? 'failed', exit, tokenUsage: usage,
        continuity: events[0]?.continuity, eventTypes: events.map(record => record.type), ...(final?.error ? { error: final.error } : {}) });
      return { final, answer };
    }
    try {
      const result = await child.done;
      const { final, answer } = recordResult(result.code);
      if (events.filter(record => record.type === 'finished').length !== 1 || final?.outcome !== (options.stop ? 'cancelled' : 'completed') || result.code !== (options.stop ? 2 : 0)) {
        throw new Failure('tool_failed', `The ${name} check did not finish as expected.`);
      }
      return { events, answer };
    } finally { if (!recorded) recordResult(signal.aborted ? 2 : 1); child.process.stdin.end(); await child.stop(); }
  }
  const require = (condition: unknown, name: string): void => { if (!condition) throw new Failure('tool_failed', `The ${name} check did not produce the expected behavior.`); };
  try {
    const plain = await message('live.ready-usage', 'Answer with the single word: ready');
    require(/\bready\b/i.test(plain.answer), 'ready answer');
    if (!buildOnly) {
      const question = await message('live.tools-questions', 'Use AskUserQuestion to ask Ready? with options Ready and Later. Then use Bash to run printf ready. Finish with ready.', { tools: true, control: true });
      require(question.events.some(event => event.type === 'ask_opened') && question.events.some(event => event.type === 'tool_finished' && event.ok), 'tools and questions');
      const background = await message('live.background', 'Start one Haiku sub-agent in the background. Tell it to run printf ready and return ready. Wait for it and finish with ready.', { tools: true });
      require(background.events.some(event => event.parentCallId) && /\bready\b/i.test(background.answer), 'background sub-agent');
      const chat = randomUUID(), firstText = 'Remember the word willow. Answer only ready.';
      const first = await message('live.continuity-fresh', firstText, { chat });
      const continued = await message('live.continuity-resumed', 'What word did I give you? Answer only that word.', { chat,
        history: [{ type: 'message', role: 'user', text: firstText }, { type: 'message', role: 'assistant', text: first.answer }] });
      require(continued.events[0]?.continuity === 'resumed' && /\bwillow\b/i.test(continued.answer), 'continuity');
      await message('live.stopping', 'Use Bash to run sleep 8 then printf ready.', { tools: true, stop: true });
    }
  } catch (failure) { error = failure; status = signal.aborted ? 2 : 1; }
  finally { await rm(directory, { recursive: true, force: true }); }
  const usageAfter = (await subscriptions.get(testSubscriptionId)).lastUsage ?? usageBefore;
  const result = { ...report, outcome: status === 0 ? 'passed' : status === 2 ? 'cancelled' : 'failed',
    usageAfter, usageChange: usageChange(usageBefore, usageAfter), ...(error ? { error: safeFailure(error) } : {}) };
  const research = join(subscriptions.state.directory, 'research'); await mkdir(research, { recursive: true, mode: 0o700 });
  await writeFile(join(research, `${buildOnly ? 'build-check' : 'live-check'}-${report.at.replaceAll(':', '-')}.json`), JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
  return { status, result };
}
