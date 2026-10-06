export type ProcessEnv = Record<string, string | undefined>;
export interface Owner { pid: number; startedAt: string }
export interface ToolRecord { path: string; size: number; changedAt: string; version: string; askedAt: string }
export interface ToolInfo { name: string; path?: string; version?: string; supported: boolean; oldestSupported: string }
