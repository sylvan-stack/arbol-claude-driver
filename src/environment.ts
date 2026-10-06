import type { ProcessEnv } from './types.js';
const inherited = [
  'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'TMPDIR', 'TMP', 'TEMP',
  'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
  'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
];
export function cleanEnvironment(source: ProcessEnv, home: string, configHome?: string): ProcessEnv {
  const env: ProcessEnv = {};
  for (const key of inherited) if (source[key] !== undefined) env[key] = source[key];
  env.HOME = home;
  if (configHome !== undefined) env.CLAUDE_CONFIG_DIR = configHome;
  return env;
}
