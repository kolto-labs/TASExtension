// Stage a measured file-transport fix without rebuilding the emulated runtime.
// All unchanged runtime assets are hard-linked into the immutable candidate.
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, writeFile, rm, rename, lstat, link, chmod } from 'node:fs/promises';
import { resolve, dirname, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const selectLocal = process.argv.includes('--select-local');
if (process.argv.slice(2).some(arg => arg !== '--select-local')) throw new Error('Usage: node stage-runtime-range-optimization.mjs [--select-local]');
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const publicRoot = resolve(demo, 'public/original-game');
const descriptorPath = resolve(publicRoot, 'runtime-local.json');
const descriptor = JSON.parse(await readFile(descriptorPath, 'utf8'));
if (descriptor.schema !== 'telos.original-runtime/v1' || !/^[0-9a-f]{64}$/.test(descriptor.release || '') || !descriptor.assets || !descriptor.root) {
  throw new Error('The selected local runtime descriptor is invalid.');
}
const oldReleaseDir = resolve(publicRoot, 'runtime-releases', descriptor.release);
const releasesDir = resolve(publicRoot, 'runtime-releases');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const payloads = new Map();
for (const [name, expectedHash] of Object.entries(descriptor.assets)) {
  if (basename(name) !== name || !/^[0-9a-f]{64}$/.test(expectedHash)) throw new Error(`Invalid runtime asset entry: ${name}`);
  const path = resolve(oldReleaseDir, name);
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error(`Runtime asset is not a regular file: ${name}`);
  const bytes = await readFile(path);
  if (hash(bytes) !== expectedHash) throw new Error(`Runtime asset hash mismatch: ${name}`);
  payloads.set(name, bytes);
}
const adapterName = 'owned-install.js';
const adapter = payloads.get(adapterName)?.toString('utf8');
if (!adapter) throw new Error('The selected runtime has no owned-file adapter.');
if (adapter.includes('const requestedBytes = end - start + 1;\n          if (text.length > requestedBytes)')) {
  console.log(`Selected runtime ${descriptor.release} already has the range-read optimization.`);
  process.exit(0);
}
const before = `        } else {
          text = readTextSlices(file, start, end, (cursor, next) => {
            const fallback = new XMLHttpRequest();
            fallback.open('GET', file.url, false);
            fallback.overrideMimeType('text/plain; charset=x-user-defined');
            fallback.setRequestHeader('Range', \`bytes=${'${cursor}'}-${'${next - 1}'}\`);
            fallback.send();
            io.requests++;
            const fallbackRange = \`bytes ${'${cursor}'}-${'${next - 1}'}/${'${file.bytes}'}\`;
            if (fallback.status !== 206 || fallback.getResponseHeader('Content-Range') !== fallbackRange) throw readFailure(file, \`range rejected, HTTP ${'${fallback.status}'}, ${'${fallbackRange}'}\`);
            return fallback.responseText;
          });
        }`;
const after = `        } else {
          text = request.responseText;
          const requestedBytes = end - start + 1;
          if (text.length > requestedBytes) throw readFailure(file, \`oversized range, ${'${text.length}'} bytes instead of ${'${requestedBytes}'}\`);
          if (text.length < requestedBytes) {
            io.shortReads++; io.readRetries++;
            text = readTextSlices(file, start, end, (cursor, next) => {
              const fallback = new XMLHttpRequest();
              fallback.open('GET', file.url, false);
              fallback.overrideMimeType('text/plain; charset=x-user-defined');
              fallback.setRequestHeader('Range', \`bytes=${'${cursor}'}-${'${next - 1}'}\`);
              fallback.send();
              io.requests++;
              const fallbackRange = \`bytes ${'${cursor}'}-${'${next - 1}'}/${'${file.bytes}'}\`;
              if (fallback.status !== 206 || fallback.getResponseHeader('Content-Range') !== fallbackRange) throw readFailure(file, \`range rejected, HTTP ${'${fallback.status}'}, ${'${fallbackRange}'}\`);
              return fallback.responseText;
            });
          }
        }`;
if (adapter.split(before).length !== 2) throw new Error('Runtime adapter does not match the reviewed range fallback; no release was staged.');
payloads.set(adapterName, Buffer.from(adapter.replace(before, after), 'utf8'));

const digest = createHash('sha256');
digest.update(JSON.stringify(descriptor.root) + '\0');
const assets = {};
for (const [name, bytes] of [...payloads].sort(([a], [b]) => a.localeCompare(b, 'en'))) {
  digest.update(name + '\0' + bytes.length + '\0').update(bytes);
  assets[name] = hash(bytes);
}
const release = digest.digest('hex');
const releaseDir = resolve(releasesDir, release);
await mkdir(releasesDir, { recursive:true, mode:0o755 });
const temporary = await mkdtemp(resolve(releasesDir, '.range-opt-'));
try {
  for (const name of payloads.keys()) {
    const target = resolve(temporary, name);
    if (name === adapterName) await writeFile(target, payloads.get(name), { mode:0o644 });
    else await link(resolve(oldReleaseDir, name), target);
  }
  await chmod(temporary, 0o755);
  try { await rename(temporary, releaseDir); }
  catch (error) {
    if (!['EEXIST','ENOTEMPTY'].includes(error.code)) throw error;
    for (const [name, expectedHash] of Object.entries(assets)) {
      const path = resolve(releaseDir, name);
      const metadata = await lstat(path);
      if (!metadata.isFile() || hash(await readFile(path)) !== expectedHash) throw new Error(`Existing candidate release is invalid: ${name}`);
    }
  }
} finally { await rm(temporary, { recursive:true, force:true }); }

const candidate = { ...descriptor, release, entry:`/original-game/runtime-releases/${release}/game-runtime.html`, assets };
if (selectLocal) {
  const nextDescriptor = resolve(publicRoot, `.runtime-local-${process.pid}.json`);
  try {
    await writeFile(nextDescriptor, JSON.stringify(candidate, null, 2) + '\n', { mode:0o644 });
    await rename(nextDescriptor, descriptorPath);
  } finally { await rm(nextDescriptor, { force:true }); }
}
console.log(`Staged range-read optimization ${release}; previous=${descriptor.release}; request body reused on normal sync HTTP reads; smaller-slice retry retained for short responses.${selectLocal ? ' Selected locally.' : ' Local selection unchanged.'}`);
