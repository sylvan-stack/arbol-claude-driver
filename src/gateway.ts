import { Failure, safeText } from './errors.js';
import { isObject } from './settings.js';
import type { UsageReport } from './subscriptions.js';

export async function gateway(baseUrl: string, path: string, key: string, timeout: number, signal?: AbortSignal): Promise<unknown> {
  const url = new URL(path, new URL(baseUrl).origin);
  const deadline = AbortSignal.timeout(timeout);
  try {
    const response = await fetch(url, { headers: { Authorization: `Bearer ${key}` },
      redirect: 'error', signal: signal ? AbortSignal.any([signal, deadline]) : deadline });
    if (response.status === 401 || response.status === 403) {
      await response.body?.cancel();
      throw new Failure('credential_rejected', 'Z.AI rejected the key.', { remedy: 'Replace the key.', detail: { basis: 'fields', vendorStatus: response.status } });
    }
    const reader = response.body?.getReader();
    const chunks: Uint8Array[] = []; let size = 0;
    if (reader) while (true) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.byteLength;
      if (size > 2 * 1024 * 1024) { await reader.cancel(); throw new Failure('vendor_unavailable', 'Z.AI returned too much data.'); }
      chunks.push(part.value);
    }
    let data: unknown;
    try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
    catch { throw new Failure('vendor_unavailable', 'Z.AI returned an unreadable answer.'); }
    if (!response.ok || !isObject(data) || data.success === false || (path.includes('/quota/') && data.success !== true)) {
      const message = isObject(data) ? [data.msg, data.message, data.error].find(value => typeof value === 'string' && value) : undefined;
      throw new Failure('vendor_unavailable', typeof message === 'string' ? safeText(message, [key]).slice(0, 240) : 'Z.AI could not answer the request.',
        { detail: { basis: 'fields', vendorStatus: response.status } }, [key]);
    }
    return data;
  } catch (error) {
    if (error instanceof Failure) throw error;
    throw new Failure('vendor_unavailable', 'Z.AI could not be reached.');
  }
}
const number = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
export function normalizeQuota(input: unknown, now = new Date(), key = ''): UsageReport {
  if (!isObject(input) || input.success !== true || !isObject(input.data) || !Array.isArray(input.data.limits))
    throw new Failure('vendor_unavailable', 'Z.AI returned an unreadable quota answer.');
  const data = input.data;
  const entries = (data.limits as unknown[]).filter(isObject).filter(item =>
    ['TOKENS_LIMIT', 'CREDIT_LIMIT', 'TIME_LIMIT'].includes(item.type as string) && number(item.percentage) !== undefined);
  const minutes = (item: Record<string, unknown>): number | undefined => {
    const unit = ({ 1: 1440, 3: 60, 5: 1, 6: 10080 } as Record<string, number>)[String(item.unit)];
    const count = number(item.number);
    return unit !== undefined && count !== undefined && count > 0 ? count * unit : undefined;
  };
  const planEntries = entries.filter(item => item.type !== 'TIME_LIMIT').sort((a, b) => (minutes(a) ?? Infinity) - (minutes(b) ?? Infinity));
  const ordered = [...planEntries, ...entries.filter(item => item.type === 'TIME_LIMIT')];
  const totals = new Map<string, number>();
  for (const item of entries) if (Array.isArray(item.usageDetails)) for (const model of item.usageDetails) {
    if (isObject(model) && typeof model.modelCode === 'string' && number(model.usage) !== undefined)
      totals.set(model.modelCode, (totals.get(model.modelCode) ?? 0) + (model.usage as number));
  }
  const plan = [data.planName, data.plan, data.plan_type, data.packageName, data.level].find(value => typeof value === 'string' && value);
  return { outcome: 'ok', fetchedAt: now.toISOString(), ...(typeof plan === 'string' ? { plan: safeText(plan, [key]) } : {}),
    windows: ordered.map((item, index) => {
      const length = minutes(item), total = number(item.usage), remaining = number(item.remaining), current = number(item.currentValue);
      const used = total !== undefined && total > 0 && (remaining !== undefined || current !== undefined) ?
        100 * (remaining !== undefined ? total - remaining : current!) / total : item.percentage as number;
      const reset = number(item.nextResetTime);
      const validReset = reset !== undefined && Number.isFinite(new Date(reset).getTime()) &&
        !(length === 300 && item.type !== 'TIME_LIMIT' && reset - now.getTime() > 301 * 60000);
      const unit = ({ 1: 'day', 3: 'hour', 5: 'minute', 6: 'week' } as Record<string, string>)[String(item.unit)];
      const label = item.type === 'TIME_LIMIT' ? 'MCP' : length === 300 ? '5-hour window' : length === 10080 ? 'Weekly window' :
        length !== undefined ? `${item.number} ${unit}${item.number === 1 ? '' : 's'} window` : 'Usage window';
      return { kind: item.type === 'TIME_LIMIT' ? 'other' as const : index === 0 ? 'session' as const : 'weekly' as const, label,
        usedPercent: Math.round(Math.min(100, Math.max(0, used)) * 10) / 10,
        ...(length !== undefined ? { windowMinutes: length } : {}), ...(validReset ? { resetsAt: new Date(reset!).toISOString() } : {}) };
    }), ...(totals.size ? { extra: { models: [...totals].sort((a, b) => b[1] - a[1]).map(([model, usage]) => ({ model: safeText(model, [key]), usage })) } } : {}) };
}
