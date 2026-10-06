import { Failure } from './errors.js';
export interface Command { name: string; positionals: string[]; options: Record<string, string | boolean | string[]>; toolRequired: boolean }
const schemas: Record<string, { values?: string[]; flags?: string[]; repeat?: string[]; min: number; max: number; tool?: boolean }> = {
  info: { min: 0, max: 0 },
  run: { values: ['request', 'subscription', 'model', 'effort', 'service-tier', 'system-prompt-file', 'cwd', 'tools', 'chat-session', 'run-id', 'output'], flags: ['control'], repeat: ['add-dir', 'label'], min: 0, max: 1, tool: true },
  'runs list': { min: 0, max: 0 },
  'runs stop': { min: 1, max: 1 },
  'subscriptions families': { min: 0, max: 0 },
  'subscriptions list': { min: 0, max: 0 },
  'subscriptions create': { values: ['family', 'label', 'id'], min: 0, max: 0 },
  'subscriptions update': { values: ['label'], min: 1, max: 1 },
  'subscriptions remove': { flags: ['force'], min: 1, max: 1 },
  'subscriptions sign-in': { flags: ['by-code'], min: 1, max: 1, tool: true },
  'subscriptions sign-out': { flags: ['force'], min: 1, max: 1 },
  'subscriptions check': { min: 1, max: 1, tool: true },
  'subscriptions usage': { min: 1, max: 1 },
  'subscriptions models': { min: 1, max: 1, tool: true },
  'subscriptions import': { values: ['from'], min: 1, max: 1 },
  'dev probe': { min: 0, max: 0, tool: true },
  'dev live-check': { min: 0, max: 0, tool: true },
  'dev build-check': { min: 0, max: 0, tool: true },
};
export function parseArguments(args: string[]): Command {
  const options: Command['options'] = {};
  const words: string[] = [];
  // The one global option is accepted before or after the operation.
  for (let i = 0; i < args.length; i++) {
    const arg = args[i]!;
    if (arg === '--') { words.push(...args.slice(i)); break; }
    if (arg === '--state-dir' || arg.startsWith('--state-dir=')) {
      const value = arg === '--state-dir' ? args[++i] : arg.slice('--state-dir='.length);
      if (!value || value.startsWith('--') || options['state-dir'] !== undefined) throw new Failure('invalid_input', 'Give --state-dir exactly one path.');
      options['state-dir'] = value;
    } else words.push(arg);
  }
  const first = words.shift();
  if (!first) throw new Failure('invalid_input', 'Name a command, such as info.');
  const name = ['subscriptions', 'runs', 'dev'].includes(first) ? `${first} ${words.shift() ?? ''}` : first;
  if (name === 'dev stand-in') {
    const separator = words.indexOf('--');
    if (separator !== 1 || !words[0]) throw new Failure('invalid_input', 'Use dev stand-in <folder> -- <arguments>.');
    return { name, positionals: words, options, toolRequired: false };
  }
  const schema = schemas[name];
  if (!schema) throw new Failure('invalid_input', 'Unknown Driver command.');
  const positionals: string[] = [];
  let positionalOnly = false;
  for (let i = 0; i < words.length; i++) {
    const arg = words[i]!;
    if (arg === '--' && !positionalOnly) { positionalOnly = true; continue; }
    if (!arg.startsWith('-') || positionalOnly) { positionals.push(arg); continue; }
    if (!arg.startsWith('--')) throw new Failure('invalid_input', 'Unknown Driver option. Build selection belongs to the arbol-builds launcher.');
    const equals = arg.indexOf('=');
    const key = arg.slice(2, equals < 0 ? undefined : equals);
    if (schema.flags?.includes(key)) {
      if (equals >= 0 || key in options) throw new Failure('invalid_input', `Option --${key} takes no value and may occur once.`);
      options[key] = true;
    } else if (schema.values?.includes(key) || schema.repeat?.includes(key)) {
      const value = equals < 0 ? words[++i] : arg.slice(equals + 1);
      if (!value || (equals < 0 && value.startsWith('--'))) throw new Failure('invalid_input', `Option --${key} needs a value.`);
      if (schema.repeat?.includes(key)) {
        const previous = options[key] as string[] | undefined;
        options[key] = [...(previous ?? []), value];
      } else {
        if (key in options) throw new Failure('invalid_input', `Option --${key} may occur once.`);
        options[key] = value;
      }
    } else throw new Failure('invalid_input', 'Unknown Driver option.');
  }
  if (positionals.length < schema.min || positionals.length > schema.max) throw new Failure('invalid_input', `Command ${name} has the wrong number of arguments.`);
  if (options.tools !== undefined && !['all', 'none'].includes(options.tools as string)) throw new Failure('invalid_input', 'Tools must be all or none.');
  if (options.output !== undefined && !['events', 'text'].includes(options.output as string)) throw new Failure('invalid_input', 'Output must be events or text.');
  return { name, positionals, options, toolRequired: schema.tool ?? false };
}
