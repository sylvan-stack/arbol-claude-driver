import { spawn } from 'node:child_process';
import { access, readFile, realpath, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir, userInfo } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';

const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";

async function run(program, args, cwd, env) {
  return new Promise((resolveResult, reject) => {
    const child = spawn(program, args, { cwd, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('close', code => resolveResult(code ?? 1));
  });
}

async function checkBuild() {
  if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Build checks need Node.js 24 or later.');
  const mode = process.argv[2];
  if (!['contract', 'live'].includes(mode) || process.argv.length !== 3) throw new Error('Use check-build.mjs contract or live.');
  const { ARBOL_BUILD_DIR: directory, ARBOL_BUILD_NUMBER: number } = process.env;
  if (!directory || !isAbsolute(directory) || !number || !/^[1-9a-f][0-9a-f]*$/.test(number)) {
    throw new Error('Set ARBOL_BUILD_DIR to an absolute Build folder and ARBOL_BUILD_NUMBER to its lower-case hexadecimal number.');
  }
  const build = await realpath(directory);
  const record = JSON.parse(await readFile(join(build, 'build.json'), 'utf8'));
  if (record.component !== 'arbol-claude-driver' || record.build !== number || record.entry !== 'bin/arbol-claude-driver.mjs') {
    throw new Error('The Build record must match this component, number and entry.');
  }
  const entry = join(build, record.entry);
  await access(entry, constants.X_OK);
  if (!(await stat(entry)).isFile() || await realpath(entry) !== entry) throw new Error('The Build entry must be an executable file inside its folder.');

  if (mode === 'contract') {
    const repo = resolve(process.env.ARBOL_CONFORMANCE_REPO ?? join(homedir(), 'repos/sylvan-stack/arbol'));
    const pkg = JSON.parse(await readFile(join(repo, 'package.json'), 'utf8'));
    if (pkg.name !== '@sylvan-stack/arbol') throw new Error('ARBOL_CONFORMANCE_REPO must name the Arbol repository.');
    const env = { ...process.env, PATH: dirname(process.execPath) + ':' + (process.env.PATH ?? '/usr/bin:/bin') };
    for (const args of [['ci'], ['run', 'build']]) {
      const code = await run('npm', args, repo, env);
      if (code !== 0) return code;
    }
    return run(process.execPath, [join(repo, 'conformance/bin/arbol-conformance.mjs'), '--driver', quote(entry)], build, env);
  }

  const state = process.env.ARBOL_BUILD_TEST_STATE_DIR;
  if (state !== undefined && (!state || !isAbsolute(state))) throw new Error('ARBOL_BUILD_TEST_STATE_DIR must be an absolute test state directory.');
  const home = userInfo().homedir;
  const env = { HOME: home, PATH: `${dirname(process.execPath)}:${join(home, '.local/bin')}:/usr/bin:/bin`, LANG: process.env.LANG || 'C' };
  return run(process.execPath, [entry, 'dev', 'build-check', ...(state ? ['--state-dir', state] : [])], build, env);
}

try {
  process.exitCode = await checkBuild();
} catch (error) {
  process.stderr.write(`Cannot check Claude Build: ${error.message}\n`);
  process.exitCode = 1;
}
