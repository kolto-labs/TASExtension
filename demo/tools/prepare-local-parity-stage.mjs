// Build a sanitized, non-deployable snapshot of the current localhost UI and
// its unqualified runtime. This is useful for parity inspection only.
import { mkdir, mkdtemp, lstat, readdir, readFile, writeFile, rename, rm, chmod } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, dirname, sep, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

if (process.argv.length !== 3) throw new Error('Usage: node prepare-local-parity-stage.mjs FRESH_OUTPUT_DIRECTORY');
const output = resolve(process.argv[2]);
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = resolve(demo, 'public');
if (output === source || output.startsWith(source + sep) || output === demo || output.startsWith(demo + sep)) throw new Error('Parity output must be outside the source tree.');
try { await lstat(output); throw new Error('Refuse existing parity output.'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const maxAssetBytes = 25 * 1024 * 1024;
const blockedDirectory = /^(?:private|installs?|installation|saves?|profiles?|logs?|cache|caches|jit-cache|tmp-jit-modules|\.git|node_modules)$/i;
const blockedFile = /\.(?:exe|dll|sav|bif|key|tlk|zip|7z|rar|iso|msi|o|a|log|sqlite|db|bak|tmp)$/i;
const allowedExtension = /\.(?:js|mjs|css|html|json|svg|png|jpe?g|webp|gif|mp4|webm|ogg|mp3|wav|woff2?)$/i;
const copied = {};
async function readRegular(path) {
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error('Refuse non-regular source file: ' + path);
  return readFile(path);
}
async function put(stage, rel, bytes) {
  if (bytes.length > maxAssetBytes) throw new Error('Static asset exceeds 25 MiB: ' + rel);
  const destination = resolve(stage, 'public', rel);
  await mkdir(dirname(destination), { recursive:true, mode:0o755 });
  await writeFile(destination, bytes, { mode:0o644 });
  copied[rel] = { bytes:bytes.length, sha256:sha(bytes) };
}
async function copyTree(stage, input, rel) {
  const metadata = await lstat(input);
  if (metadata.isSymbolicLink()) throw new Error('Refuse publication symlink: ' + input);
  if (metadata.isDirectory()) {
    for (const name of (await readdir(input)).sort()) {
      if (blockedDirectory.test(name) || blockedFile.test(name)) continue;
      await copyTree(stage, resolve(input, name), `${rel}/${name}`);
    }
  } else if (metadata.isFile()) {
    const name = input.split(sep).at(-1);
    if (blockedFile.test(name)) return;
    if (!allowedExtension.test(name) && !['_headers', '.assetsignore'].includes(name)) throw new Error('Review unexpected local prototype asset: ' + rel);
    await put(stage, rel, await readRegular(input));
  } else throw new Error('Refuse special publication input: ' + input);
}

const localDescriptor = JSON.parse((await readRegular(resolve(source, 'original-game/runtime-local.json'))).toString('utf8'));
const runtimeNames = ['boxedwine-shell.js', 'boxedwine.css', 'boxedwine.js', 'boxedwine.wasm', 'embedded.css', 'game-runtime.html', 'GL4ES-LICENSE.txt', 'NOTICE.txt', 'owned-install.js'];
const canonicalRuntimeNames = [...runtimeNames].sort((a, b) => a.localeCompare(b, 'en'));
if (localDescriptor.schema !== 'telos.original-runtime/v1' || localDescriptor.qualified !== false || !/^[0-9a-f]{64}$/.test(localDescriptor.release || '') || localDescriptor.entry !== `/original-game/runtime-releases/${localDescriptor.release}/game-runtime.html` || JSON.stringify(Object.keys(localDescriptor.assets || {}).sort((a, b) => a.localeCompare(b, 'en'))) !== JSON.stringify(canonicalRuntimeNames)) throw new Error('Expected the exact current localhost unqualified descriptor.');
const root = localDescriptor.root;
if (!root || root.sha256 !== 'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5' || root.bytes !== 99450694 || root.base !== `/original-game/wine-roots/${root.sha256}/`) throw new Error('Unexpected localhost Wine-root identity.');
const runtimeDigest = createHash('sha256').update(JSON.stringify(root) + '\0');
const runtimeFiles = new Map();
for (const name of [...runtimeNames].sort((a, b) => a.localeCompare(b, 'en'))) {
  const bytes = await readRegular(resolve(source, 'original-game/runtime-releases', localDescriptor.release, name));
  if (sha(bytes) !== localDescriptor.assets[name]) throw new Error('Local immutable runtime hash mismatch: ' + name);
  runtimeDigest.update(name + '\0' + bytes.length + '\0').update(bytes);
  runtimeFiles.set(name, bytes);
}
if (runtimeDigest.digest('hex') !== localDescriptor.release) throw new Error('Local runtime release digest mismatch.');

const rootDir = resolve(source, 'original-game/wine-roots', root.sha256);
const rootChunksBytes = await readRegular(resolve(rootDir, 'chunks.json'));
const rootChunks = JSON.parse(rootChunksBytes.toString('utf8'));
if (rootChunks.sha256 !== root.sha256 || rootChunks.bytes !== root.bytes || rootChunks.chunkBytes !== root.chunkBytes || !Array.isArray(rootChunks.chunks) || rootChunks.chunks.length !== Math.ceil(root.bytes / root.chunkBytes)) throw new Error('Local Wine-root chunk index is invalid.');
const archive = createHash('sha256');
const rootFiles = new Map();
for (let i = 0; i < rootChunks.chunks.length; i++) {
  const chunk = rootChunks.chunks[i];
  if (chunk.name !== `${i}-${chunk.sha256}.bin` || chunk.bytes !== Math.min(root.chunkBytes, root.bytes - i * root.chunkBytes) || !/^[0-9a-f]{64}$/.test(chunk.sha256 || '')) throw new Error('Unexpected local root chunk entry.');
  const bytes = await readRegular(resolve(rootDir, chunk.name));
  if (bytes.length !== chunk.bytes || sha(bytes) !== chunk.sha256) throw new Error('Local root chunk integrity mismatch.');
  archive.update(bytes);
  rootFiles.set(chunk.name, bytes);
}
if (archive.digest('hex') !== root.sha256) throw new Error('Local Wine-root chunks do not reconstruct the pinned archive.');
const localArchivePath = resolve(rootDir, '.local-root.zip');
const archiveFileHash = createHash('sha256');
const archiveHandle = await import('node:fs');
for await (const chunk of archiveHandle.createReadStream(localArchivePath)) archiveFileHash.update(chunk);
if (archiveFileHash.digest('hex') !== root.sha256) throw new Error('Local .local-root.zip does not match the chunked root selected by localhost.');
rootFiles.set('chunks.json', rootChunksBytes);

const temporary = await mkdtemp(resolve(dirname(output), '.local-parity-stage-'));
try {
  const staticRoots = new Set(['index.html', 'style.css', 'favicon.svg', '_headers', '.assetsignore', 'js', 'media', 'player', 'wasm-game']);
  for (const name of await readdir(source)) {
    if (name === 'original-game' || blockedDirectory.test(name)) continue;
    if (!staticRoots.has(name)) throw new Error('Review new public-root entry before staging: ' + name);
    await copyTree(temporary, resolve(source, name), name);
  }
  for (const name of ['asset-lease.js', 'index.html', 'style.css', 'owned-game-sw.js', 'embedded.css']) await put(temporary, `original-game/${name}`, await readRegular(resolve(source, 'original-game', name)));
  for (const [name, bytes] of runtimeFiles) await put(temporary, `original-game/runtime-releases/${localDescriptor.release}/${name}`, bytes);
  await put(temporary, 'original-game/runtime-local.json', await readRegular(resolve(source, 'original-game/runtime-local.json')));
  const localNotices = 'Wine-root receipt for the localhost runtime\n\nThe selected localhost descriptor binds the Wine root to SHA256 a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5 (99450694 bytes). This matches the upstream BoxedWine web-v13 archive pin at https://boxedwine.org/v2/13/TinyCore15Wine11.0-web.zip. The modified 2d07dd candidate is not selected by this localhost descriptor. The Wine 11 source reference pinned by the distributor is db11d0fe6a169c457e23d007e20404643d067aa8. See wine-notices/notice-index.json for retained notices from the archive.\n\nThis is an unqualified local parity snapshot, not a release or source-completeness claim. No KOTOR installation files or saves are included.\n';
  await put(temporary, 'original-game/WINE-NOTICES.txt', Buffer.from(localNotices));
  const noticesDir = resolve(source, 'original-game/wine-notices');
  const noticeIndexBytes = await readRegular(resolve(noticesDir, 'notice-index.json'));
  const noticeIndex = JSON.parse(noticeIndexBytes.toString('utf8'));
  if (!Array.isArray(noticeIndex)) throw new Error('Invalid local Wine notice index.');
  for (const notice of noticeIndex) {
    const path = notice.archive_path;
    if (typeof path !== 'string' || !/^usr\/(?:local\/)?share\/(?:doc|licences)\//.test(path) || path.split('/').some(part => !part || part === '.' || part === '..') || notice.notice_url !== '/original-game/wine-notices/' + path || !/^[0-9a-f]{64}$/.test(notice.sha256 || '')) throw new Error('Invalid local Wine notice path.');
    const bytes = await readRegular(resolve(noticesDir, path));
    if (sha(bytes) !== notice.sha256) throw new Error('Wine notice hash mismatch: ' + path);
    await put(temporary, `original-game/wine-notices/${path}`, bytes);
  }
  await put(temporary, 'original-game/wine-notices/notice-index.json', noticeIndexBytes);
  // Public origins request this descriptor, so stage an explicit, honest
  // unqualified result instead of letting SPA fallback serve index.html as JSON.
  await put(temporary, 'original-game/runtime-qualified.json', Buffer.from(JSON.stringify(localDescriptor, null, 2) + '\n'));
  for (const [name, bytes] of rootFiles) await put(temporary, `original-game/wine-roots/${root.sha256}/${name}`, bytes);
  const worker = await readRegular(resolve(demo, 'worker/wine-root.mjs'));
  await mkdir(resolve(temporary, 'worker'), { recursive:true, mode:0o755 });
  await writeFile(resolve(temporary, 'worker/wine-root.mjs'), worker, { mode:0o644 });
  const rootContract = `// Mirror-only contract from the current localhost descriptor; not a qualification claim.\nexport const WINE_ROOT = Object.freeze({\n  sha256:'${root.sha256}',\n  bytes:${root.bytes},\n  chunkBytes:${root.chunkBytes},\n});\nexport const LOCAL_WINE_ROOTS = Object.freeze([WINE_ROOT]);\n`;
  await mkdir(resolve(temporary, 'tools'), { recursive:true, mode:0o755 });
  await writeFile(resolve(temporary, 'tools/wine-root-contract.mjs'), rootContract, { mode:0o644 });
  const config = JSON.parse((await readRegular(resolve(demo, 'wrangler.jsonc'))).toString('utf8').split('\n').filter(line => !line.trim().startsWith('//')).join('\n'));
  config.name = 'kptas-local-parity-stage-not-for-deploy';
  config.main = './worker/wine-root.mjs';
  config.assets.directory = './public';
  await writeFile(resolve(temporary, 'wrangler.jsonc'), JSON.stringify(config, null, 2) + '\n', { mode:0o644 });
  const sourceFacts = {
    schema:'telos.local-parity-stage/v1',
    sourceOrigin:'http://127.0.0.1:47653/',
    runtime:{ release:localDescriptor.release, qualified:false, assets:localDescriptor.assets },
    wineRoot:{ sha256:root.sha256, bytes:root.bytes, chunkBytes:root.chunkBytes, localArchiveValidatedByChunks:true },
    deployment:{ deployable:false, reason:'Current localhost runtime is explicitly unqualified and has not passed visible real-game menu/play acceptance. Do not deploy this stage.' },
    privacy:{ userSuppliedKotorFilesIncluded:false, excludedExtensions:['exe','dll','sav','bif','key','tlk','zip','7z','rar','iso','msi'], userFilesExpectedToBeSuppliedAtRuntime:true },
    assets:copied,
  };
  await mkdir(resolve(temporary, 'public'), { recursive:true });
  await writeFile(resolve(temporary, 'public/LOCAL-PARITY-NOT-DEPLOYABLE.txt'), Buffer.from('Inspection stage only. This bundle carries the current localhost UI and unqualified local runtime. It is deliberately not deployable.\n'));
  async function inventory(path) {
    const records = {};
    for (const name of await readdir(path)) {
      const entry = resolve(path, name);
      const metadata = await lstat(entry);
      if (metadata.isSymbolicLink()) throw new Error('Refuse staged symlink: ' + entry);
      if (metadata.isDirectory()) Object.assign(records, await inventory(entry));
      else if (metadata.isFile()) {
        if (entry === resolve(temporary, 'stage-manifest.json')) continue;
        const bytes = await readFile(entry);
        records[relative(temporary, entry).split(sep).join('/')] = { bytes:bytes.length, sha256:sha(bytes) };
      } else throw new Error('Refuse staged special file: ' + entry);
    }
    return records;
  }
  sourceFacts.files = await inventory(temporary);
  await writeFile(resolve(temporary, 'stage-manifest.json'), JSON.stringify(sourceFacts, null, 2) + '\n');
  await chmod(temporary, 0o755);
  await rename(temporary, output);
  console.log(`Prepared sanitized localhost parity snapshot at ${output}. Runtime remains unqualified; no deployment performed.`);
} finally { await rm(temporary, { recursive:true, force:true }); }
