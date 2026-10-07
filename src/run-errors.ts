import { Failure } from './errors.js';
import type { ErrorCode } from './errors.js';
import type { UsageReport } from './subscriptions.js';
import { isObject } from './settings.js';
import { OLDEST_SUPPORTED } from './tool.js';

export function retryWait(text: string): number | undefined {
  const match = /\b(retry after|retry in|try again in|wait)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)?/i.exec(text);
  if (!match || !match[3] && match[1]!.toLowerCase() !== 'retry after') return undefined;
  // An unrecognised unit must not silently become seconds.
  if (!match[3] && /^\s*[a-z]/i.test(text.slice(match.index + match[0].length))) return undefined;
  const seconds = Number(match[2]) * (match[3]?.toLowerCase().startsWith('m') ? 60 : 1);
  return Number.isFinite(seconds) ? seconds : undefined;
}
export function limitReport(record: Record<string, any>, now = new Date()): UsageReport | undefined {
  const info = record.rate_limit_info;
  if (!isObject(info) || !isObject(info.unifiedWindows)) return undefined;
  const windows: UsageReport['windows'] = [];
  for (const [key, kind, minutes, label] of [['five_hour', 'session', 300, 'Five hours'], ['seven_day', 'weekly', 10080, 'Seven days']] as const) {
    const raw = info.unifiedWindows[key];
    if (!isObject(raw)) continue;
    const utilization = typeof raw.utilization === 'number' && Number.isFinite(raw.utilization) && raw.utilization >= 0 ? raw.utilization : undefined;
    const reset = typeof raw.resetsAt === 'number' && Number.isFinite(raw.resetsAt) ? new Date(raw.resetsAt * 1000) : undefined;
    windows.push({ kind, label, windowMinutes: minutes, ...(utilization === undefined ? {} : { usedPercent: utilization * 100, limitReached: utilization >= 1 }),
      ...(reset && Number.isFinite(reset.getTime()) ? { resetsAt: reset.toISOString() } : {}) });
  }
  return { outcome: 'ok', fetchedAt: now.toISOString(), windows, extra: {
    ...(typeof info.status === 'string' ? { status: info.status } : {}),
    ...(typeof info.rateLimitType === 'string' ? { rateLimitType: info.rateLimitType } : {}),
    ...(typeof info.isUsingOverage === 'boolean' ? { isUsingOverage: info.isUsingOverage } : {}),
  } };
}
export function runFailure(result: Record<string, any> | undefined, assistant: Record<string, any> | undefined,
  family: string, version: string, secrets: readonly string[] = [], usage?: UsageReport, now = Date.now()): Failure {
  const message = assistant?.message?.content?.filter((block: any) => block.type === 'text' && typeof block.text === 'string').map((block: any) => block.text).join('\n') ||
    (Array.isArray(result?.errors) ? result.errors.filter((value: unknown) => typeof value === 'string').join('\n') : '') ||
    (typeof result?.result === 'string' ? result.result : '') || 'Claude reported an error.';
  const vendorCode = typeof assistant?.error === 'string' && assistant.error ? assistant.error : undefined;
  const vendorStatus = typeof result?.api_error_status === 'number' && Number.isInteger(result.api_error_status) ? result.api_error_status : undefined;
  let code: ErrorCode = 'vendor_error', basis: 'fields' | 'wording' = 'fields';
  if (vendorCode === 'authentication_failed' || vendorStatus === 401 || vendorStatus === 403) code = 'credential_rejected';
  else if (vendorCode === 'model_not_found' || vendorStatus === 404) code = 'invalid_input';
  else if (vendorCode && /rate.?limit/i.test(vendorCode) || vendorStatus === 429) code = 'rate_limited';
  else if (vendorStatus !== undefined && vendorStatus >= 500 || vendorCode === 'server_error') code = 'vendor_unavailable';
  else if (vendorStatus === 400 && /(?:prompt|conversation|context).*(?:too long|too large|exceed)|(?:too long|too large).*(?:prompt|conversation|context)/i.test(message)) code = 'context_exceeded';
  else if (vendorCode || vendorStatus !== undefined && vendorStatus >= 400 && vendorStatus < 500) code = 'vendor_error';
  else if (result?.subtype === 'error_max_turns' || result?.terminal_reason === 'max_turns') code = 'limit_reached';
  else if (vendorStatus === undefined) {
    basis = 'wording';
    if (/rate limit|rate_limit|too many requests|quota exceeded|usage limit|overloaded/i.test(message)) code = 'rate_limited';
    else if (/failed to authenticate|authentication_error|invalid authentication/i.test(message)) code = 'credential_rejected';
  }
  let wait = code === 'rate_limited' ? retryWait(message) : undefined;
  if (code === 'rate_limited' && wait === undefined && family === 'anthropic' && usage) {
    const kind = usage.extra?.rateLimitType === 'five_hour' ? 'session' : usage.extra?.rateLimitType === 'seven_day' ? 'weekly' : undefined;
    const reset = kind ? usage.windows.find(window => window.kind === kind)?.resetsAt : undefined;
    if (reset !== undefined) wait = Math.max(0, (Date.parse(reset) - now) / 1000);
  }
  return new Failure(code, message, { detail: { basis, toolVersion: version,
    ...(vendorCode ? { vendorCode } : {}), ...(vendorStatus === undefined ? {} : { vendorStatus }) },
    ...(wait === undefined ? {} : { retryAfterSeconds: wait }),
    ...(code === 'credential_rejected' ? { remedy: family === 'zai' ? 'Replace the key.' : 'Sign in again.' } :
      code === 'invalid_input' ? { remedy: 'Choose a model from the catalog.' } : code === 'context_exceeded' ? { remedy: 'Start a new chat.' } : {}) }, secrets);
}
export function exitFailure(stderr: string, exit: number | null, version: string, secrets: readonly string[], missing = false): Failure {
  missing ||= exit === 127 && /exec:.*(?:not found|no such file)/i.test(stderr);
  const unsupported = /unknown (?:option|argument)|unrecognized (?:option|argument)|unexpected argument/i.test(stderr);
  return new Failure(missing ? 'tool_missing' : unsupported ? 'tool_unsupported' : 'tool_failed',
    missing ? `claude was not found; version not found, needed ${OLDEST_SUPPORTED}.` : `Claude ended without a result${stderr ? ': ' + stderr : '.'}`, {
      detail: { basis: unsupported ? 'wording' : 'fields', toolVersion: version, ...(missing || unsupported ? { neededVersion: OLDEST_SUPPORTED } : {}), ...(exit === null ? {} : { toolExit: exit }), ...(stderr ? { excerpt: stderr } : {}) },
      ...(unsupported ? { remedy: 'Update claude.' } : missing ? { remedy: 'Install claude.' } : {}),
    }, secrets);
}
