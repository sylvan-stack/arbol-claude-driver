import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Failure, errno } from './errors.js';
import { isObject } from './settings.js';
export async function buildIdentity(start: string = dirname(fileURLToPath(import.meta.url))): Promise<{ version: string; build: string }> {
  let directory = start;
  while (true) {
    const buildFile = join(directory, 'build.json');
    let record: unknown;
    try { record = JSON.parse(await readFile(buildFile, 'utf8')) as unknown; }
    catch (error) { if (!errno(error, 'ENOENT')) throw new Failure('internal', `Cannot read Build record ${buildFile}.`); }
    if (record !== undefined) {
      if (!isObject(record) || record.component !== 'arbol-claude-driver' ||
        typeof record.build !== 'string' || !/^[1-9a-f][0-9a-f]*$/.test(record.build) ||
        typeof record.packageVersion !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(record.packageVersion)) {
        throw new Failure('internal', `Invalid Build record ${buildFile}.`);
      }
      return { version: record.packageVersion, build: record.build };
    }
    let pkg: unknown;
    try { pkg = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8')) as unknown; }
    catch (error) { if (!errno(error, 'ENOENT')) throw new Failure('internal', 'Cannot read the Driver package version.'); }
    if (isObject(pkg) && pkg.name === '@sylvan-stack/arbol-claude-driver' && typeof pkg.version === 'string') return { version: pkg.version, build: '0' };
    const parent = dirname(directory);
    if (parent === directory) throw new Failure('internal', 'The Driver has no package or Build record.');
    directory = parent;
  }
}
