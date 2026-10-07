export const errorCodes = [
  'invalid_input', 'not_found', 'in_use', 'unsupported', 'not_signed_in',
  'credential_rejected', 'rate_limited', 'context_exceeded', 'vendor_unavailable',
  'vendor_error', 'tool_missing', 'tool_unsupported', 'tool_failed', 'limit_reached',
  'state_newer', 'internal',
] as const;
export type ErrorCode = typeof errorCodes[number];
export interface DriverError {
  code: ErrorCode;
  message: string;
  retryable: boolean;
  retryAfterSeconds?: number;
  remedy?: string;
  detail?: {
    basis: 'fields' | 'wording';
    vendorStatus?: number;
    vendorCode?: string;
    limit?: 'start' | 'idle' | 'total';
    toolExit?: number;
    toolVersion?: string;
    neededVersion?: string;
    excerpt?: string;
  };
}
export function safeText(text: string, secrets: readonly string[] = [], limit = 1024): string {
  for (const secret of secrets) if (secret) text = text.replaceAll(secret, '[redacted]');
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= limit) return text;
  let start = bytes.length - limit;
  while (start < bytes.length && (bytes[start]! & 0xc0) === 0x80) start++;
  return bytes.subarray(start).toString('utf8');
}
export function redact<T>(value: T, secrets: readonly string[]): T {
  if (typeof value === 'string') return safeText(value, secrets, Infinity) as T;
  if (Array.isArray(value)) return value.map(item => redact(item, secrets)) as T;
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [safeText(key, secrets, Infinity), redact(item, secrets)])) as T;
  return value;
}
export class Failure extends Error {
  readonly result: DriverError;
  constructor(code: ErrorCode, message: string, extra: Omit<Partial<DriverError>, 'code' | 'message' | 'retryable'> = {}, secrets: readonly string[] = []) {
    super(safeText(message, secrets));
    if (!errorCodes.includes(code)) throw new TypeError('Unknown Driver error code');
    this.result = { ...extra, code, message: this.message, retryable: code === 'rate_limited' || code === 'vendor_unavailable' };
    if (this.result.detail?.excerpt !== undefined) this.result.detail = { ...this.result.detail, excerpt: safeText(this.result.detail.excerpt, secrets) };
    if (this.result.retryAfterSeconds !== undefined && code !== 'rate_limited') {
      delete this.result.retryAfterSeconds;
    }
  }
}
export function errno(error: unknown, code: string): boolean {
  return error instanceof Error && 'code' in error && error.code === code;
}
export function safeFailure(error: unknown): DriverError {
  return error instanceof Failure ? error.result : new Failure('internal', 'The Driver could not complete this operation.').result;
}
