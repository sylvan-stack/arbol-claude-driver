import { Failure } from './errors.js';
import { fixedWait } from './state.js';
import type { RunRequest } from './run-request.js';

// Monotonic clocks also cover a refused resume's replacement invocation.
export class RunClock {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private started = false;
  private blocked = false;
  private lastLife: number;
  private ended = false;
  private readonly idle: number;
  private readonly attemptStart: number;
  constructor(readonly limits: RunRequest['limits'], readonly expire: (failure: Failure) => void,
    readonly totalStart: number, readonly now = () => performance.now(), readonly schedule = setTimeout, readonly clear = clearTimeout) {
    this.lastLife = this.attemptStart = now();
    this.idle = limits?.idleSeconds === undefined ? fixedWait(900000) : limits.idleSeconds * 1000;
    this.arm();
  }
  life(started: boolean, blocked: boolean): void {
    this.started ||= started;
    this.blocked = blocked;
    this.lastLife = this.now();
    this.arm();
  }
  close(): void { this.ended = true; this.clear(this.timer); }
  private arm(): void {
    this.clear(this.timer);
    if (this.ended) return;
    const deadlines: Array<{ limit: 'start' | 'idle' | 'total'; at: number }> = [];
    if (!this.started) deadlines.push({ limit: 'start', at: this.attemptStart + fixedWait(60000) });
    if (this.started && !this.blocked && this.idle > 0) deadlines.push({ limit: 'idle', at: this.lastLife + this.idle });
    if (this.limits?.totalSeconds !== undefined) deadlines.push({ limit: 'total', at: this.totalStart + this.limits.totalSeconds * 1000 });
    deadlines.sort((a, b) => a.at - b.at);
    const next = deadlines[0];
    if (!next) return;
    this.timer = this.schedule(() => {
      if (this.now() < next.at) { this.arm(); return; }
      this.ended = true;
      this.expire(new Failure('limit_reached', `The run reached its ${next.limit} time limit.`, { detail: { basis: 'fields', limit: next.limit } }));
    }, Math.max(1, Math.min(2147483647, Math.ceil(next.at - this.now()))));
  }
}
