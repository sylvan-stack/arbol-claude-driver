import { cp, readFile, realpath, chmod } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const files = [
  'bin',
  'dist/src',
  'dist/test',
  'package.json',
  'README.md',
  'test/run-protocol-2026-10-06.json',
  'test/continuity-probe-2026-10-07.json',
  'test/errors-probe-2026-10-07.json',
  'test/errors-protocol-2026-10-07.json',
  'test/tools-protocol-2026-10-06.json',
];
const within = (parent, child) => {
  const path = relative(parent, child);
  return path === '' || (path !== '..' && !path.startsWith('..' + sep) && !isAbsolute(path));
};

async function packageBuild() {
  if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Packaging needs Node.js 24 or later.');
  const { ARBOL_BUILD_DIR: directory, ARBOL_BUILD_NUMBER: number } = process.env;
  if (!directory || !isAbsolute(directory) || !number || !/^[1-9a-f][0-9a-f]*$/.test(number)) {
    throw new Error('Set ARBOL_BUILD_DIR to an absolute Build folder and ARBOL_BUILD_NUMBER to its lower-case hexadecimal number.');
  }
  const build = await realpath(directory);
  const source = await realpath(root);
  if (within(build, source) || ['bin', 'dist', 'test'].some(folder => within(join(source, folder), build))) {
    throw new Error('The Build folder must not overlap the packaged source files.');
  }
  const record = JSON.parse(await readFile(join(build, 'build.json'), 'utf8'));
  const pkg = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (record.component !== 'arbol-claude-driver' || record.build !== number || record.packageVersion !== pkg.version) {
    throw new Error('The Build record must match this component, number and package version.');
  }
  for (const file of files) await cp(join(root, file), join(build, file), { recursive: true });
  await chmod(join(build, 'bin/arbol-claude-driver.mjs'), 0o755);
  process.stdout.write(`Packaged arbol-claude-driver@${number} in ${build}\n`);
}

try {
  await packageBuild();
} catch (error) {
  process.stderr.write(`Cannot package Claude Build: ${error.message}\n`);
  process.exitCode = 1;
}
