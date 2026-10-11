// Reuse an admitted runtime's compiled payloads for one reviewed adapter change.
// No game files, rebuilding, local selection, or publication happen here.
import { readFile, writeFile, mkdir, mkdtemp, rename, rm, lstat, readdir, chmod } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const [descriptorArg, adapterArg, outputArg] = process.argv.slice(2);
if (!descriptorArg || !adapterArg || !outputArg || process.argv.length !== 5) {
  throw new Error('Usage: stage-runtime-adapter.mjs <parent-descriptor.json> <reviewed-owned-install.js> <candidate-directory>');
}
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const descriptorBytes = await readFile(resolve(descriptorArg));
const parent = JSON.parse(descriptorBytes);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
if (parent.schema !== 'telos.original-runtime/v1' || !/^[0-9a-f]{64}$/.test(parent.release || '')
    || parent.entry !== `/original-game/runtime-releases/${parent.release}/game-runtime.html`
    || !parent.root || !parent.assets || !parent.assets['owned-install.js']) throw new Error('Invalid parent runtime descriptor.');
const source = resolve(demo, 'public/original-game/runtime-releases', parent.release);
const names = Object.keys(parent.assets).sort((a, b) => a.localeCompare(b, 'en'));
if (names.some(name => !/^[A-Za-z0-9_.-]+$/.test(name))
    || names.length !== (await readdir(source)).length) throw new Error('Unexpected parent runtime asset set.');
const payloads = new Map();
const parentDigest = createHash('sha256').update(JSON.stringify(parent.root) + '\0');
for (const name of names) {
  const path = resolve(source, name);
  if (!(await lstat(path)).isFile()) throw new Error('Parent asset is not a regular file: ' + name);
  const bytes = await readFile(path);
  if (sha(bytes) !== parent.assets[name]) throw new Error('Parent asset changed: ' + name);
  parentDigest.update(name + '\0' + bytes.length + '\0').update(bytes);
  payloads.set(name, bytes);
}
if (parentDigest.digest('hex') !== parent.release) throw new Error('Parent release digest mismatch.');
const adapterPath = resolve(adapterArg);
if (!(await lstat(adapterPath)).isFile()) throw new Error('Adapter must be a regular file.');
const adapter = await readFile(adapterPath);
if (adapter.length === 0 || adapter.length > 1024 * 1024) throw new Error('Invalid adapter size.');
payloads.set('owned-install.js', adapter);
const digest = createHash('sha256').update(JSON.stringify(parent.root) + '\0');
const assets = {};
for (const [name, bytes] of payloads) {
  digest.update(name + '\0' + bytes.length + '\0').update(bytes);
  assets[name] = sha(bytes);
}
const release = digest.digest('hex');
if (release === parent.release) throw new Error('Adapter has no change.');
const output = resolve(outputArg);
await mkdir(output, { recursive:true, mode:0o700 });
const temporary = await mkdtemp(resolve(output, '.adapter-stage-'));
try {
  for (const [name, bytes] of payloads) await writeFile(resolve(temporary, name), bytes, { mode:0o644 });
  await chmod(temporary, 0o755);
  await rename(temporary, resolve(output, release));
} finally { await rm(temporary, { recursive:true, force:true }); }
const descriptor = { ...parent, release, entry:`/original-game/runtime-releases/${release}/game-runtime.html`, qualified:false, assets };
const receipt = { schema:'telos.runtime-adapter-stage/v1', createdAt:new Date().toISOString(),
  parentRelease:parent.release, parentDescriptorSha256:sha(descriptorBytes), adapterSource:adapterPath,
  adapterSha256:sha(adapter), release, root:parent.root,
  unchangedAssets:names.filter(name => name !== 'owned-install.js'), rebuilt:false, selected:false };
await writeFile(resolve(output, release + '.descriptor.json'), JSON.stringify(descriptor, null, 2) + '\n', { flag:'wx', mode:0o600 });
await writeFile(resolve(output, release + '.receipt.json'), JSON.stringify(receipt, null, 2) + '\n', { flag:'wx', mode:0o600 });
console.log(JSON.stringify({ release, directory:resolve(output, release), descriptor:resolve(output, release + '.descriptor.json'), receipt:resolve(output, release + '.receipt.json') }));
