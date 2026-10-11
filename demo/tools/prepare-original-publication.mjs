// Explicit preparation after gameplay acceptance; never promotes or deploys.
// From demo/, run `npm ci` before using this pinned publication minifier.
// node prepare-original-publication.mjs QUALIFIED_DESCRIPTOR SOURCE_PACKAGE NEW_OUTPUT
import { mkdir, mkdtemp, lstat, readdir, readFile, writeFile, rename, rm, chmod } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, dirname, sep, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { transform } from 'esbuild';
import { WINE_ROOT } from './wine-root-contract.mjs';

if (process.argv.length !== 5) throw new Error('Require an explicitly accepted qualified descriptor, prepared source package and fresh output.');
const [descriptorPath, sourcePackage, output] = process.argv.slice(2).map(value => resolve(value));
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const publicRoot = resolve(demo, 'public');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const limit = 25 * 1024 * 1024;
async function regular(path) {
  if (!(await lstat(path)).isFile()) throw new Error('Refuse nonregular publication file: ' + path);
  const bytes = await readFile(path);
  if (bytes.length > limit) throw new Error('Static asset exceeds 25 MiB: ' + path);
  return bytes;
}
const descriptorBytes = await regular(descriptorPath);
const descriptor = JSON.parse(descriptorBytes.toString('utf8'));
const release = descriptor.release;
const requiredAssets = ['boxedwine-shell.js', 'boxedwine.css', 'boxedwine.js', 'boxedwine.wasm', 'embedded.css', 'game-runtime.html', 'GL4ES-LICENSE.txt', 'NOTICE.txt', 'owned-install.js'].sort();
if (descriptor.schema !== 'telos.original-runtime/v1' || descriptor.qualified !== true || !/^[0-9a-f]{64}$/.test(release || '') || descriptor.entry !== `/original-game/runtime-releases/${release}/game-runtime.html` || !descriptor.assets || JSON.stringify(Object.keys(descriptor.assets).sort()) !== JSON.stringify(requiredAssets)) throw new Error('Require exactly the accepted immutable runtime descriptor.');
const engineNames = ['boxedwine.wasm', 'boxedwine.js', 'boxedwine.css'];
if (!descriptor.sourceBundle || JSON.stringify(Object.keys(descriptor.sourceBundle).sort()) !== JSON.stringify([...engineNames].sort()) || engineNames.some(name => !/^[0-9a-f]{64}$/.test(descriptor.sourceBundle[name] || ''))) throw new Error('Qualified runtime descriptor must bind the raw engine bundle separately from transformed delivery assets.');
const root = descriptor.root;
if (root?.sha256 !== WINE_ROOT.sha256 || root.bytes !== WINE_ROOT.bytes || root.chunkBytes !== WINE_ROOT.chunkBytes || root.base !== `/original-game/wine-roots/${WINE_ROOT.sha256}/` || root.localFile !== '.local-root.zip' || root.publicFile !== 'wine-root.zip') throw new Error('Unexpected Wine-root identity.');
if ([publicRoot, sourcePackage].some(path => output === path || output.startsWith(path + sep))) throw new Error('Fresh publication must be outside public and source input trees.');
try { await lstat(output); throw new Error('Refuse existing output.'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const runtimeFiles = new Map();
const digest = createHash('sha256').update(JSON.stringify(root) + '\0');
for (const name of [...requiredAssets].sort((a, b) => a.localeCompare(b, 'en'))) {
  const bytes = await regular(resolve(publicRoot, 'original-game/runtime-releases', release, name));
  if (sha(bytes) !== descriptor.assets[name]) throw new Error('Accepted runtime hash mismatch: ' + name);
  digest.update(name + '\0' + bytes.length + '\0').update(bytes);
  runtimeFiles.set(name, bytes);
}
if (digest.digest('hex') !== release) throw new Error('Descriptor release differs from generated payload identity.');
const roots = resolve(publicRoot, 'original-game/wine-roots', WINE_ROOT.sha256);
const chunkBytes = await regular(resolve(roots, 'chunks.json'));
const chunks = JSON.parse(chunkBytes.toString('utf8'));
if (chunks.sha256 !== WINE_ROOT.sha256 || chunks.bytes !== WINE_ROOT.bytes || chunks.chunkBytes !== WINE_ROOT.chunkBytes || !Array.isArray(chunks.chunks) || chunks.chunks.length !== Math.ceil(WINE_ROOT.bytes / WINE_ROOT.chunkBytes)) throw new Error('Invalid root chunk metadata.');
const sourceManifest = JSON.parse((await regular(resolve(sourcePackage, 'source-manifest.json'))).toString('utf8'));
const wineEvidenceBytes = await regular(resolve(sourcePackage, 'wine-root-provenance.json'));
const wineEvidenceFile = JSON.parse(wineEvidenceBytes.toString('utf8'));
if (sourceManifest.schema !== 'telos.corresponding-source/v1' || !Array.isArray(sourceManifest.parts) || !sourceManifest.parts.length || engineNames.some(name => sourceManifest.bundleHashes?.[name] !== descriptor.sourceBundle[name] || sourceManifest.runtime?.sourceBundle?.[name] !== descriptor.sourceBundle[name])) throw new Error('Source package is not associated with the accepted raw runtime bundle.');
for (const name of ['boxedwine.wasm', 'boxedwine.css']) if (descriptor.sourceBundle[name] !== descriptor.assets[name]) throw new Error('Unexpected transformed engine payload: ' + name);
if (sourceManifest.runtime?.release !== release || JSON.stringify(Object.keys(sourceManifest.runtime.assets || {}).sort()) !== JSON.stringify(requiredAssets) || requiredAssets.some(name => sourceManifest.runtime.assets[name] !== descriptor.assets[name])) throw new Error('Source package lacks the exact accepted transformed runtime source binding.');

if (sourceManifest.wineRoot?.sha256 !== WINE_ROOT.sha256 || sourceManifest.wineRoot.bytes !== WINE_ROOT.bytes) throw new Error('Source package does not identify the selected guest root.');
const wineEvidence = sourceManifest.wineRootProvenance;
if (wineEvidence?.schema !== 'telos.wine-root-provenance/v1' || JSON.stringify(wineEvidenceFile) !== JSON.stringify(wineEvidence) || wineEvidence.archive?.sha256 !== WINE_ROOT.sha256 || wineEvidence.archive.bytes !== WINE_ROOT.bytes || wineEvidence.archive.status !== 'verified' || wineEvidence.archiveDelta?.status !== 'verified' || wineEvidence.archiveDelta?.baselineMemberCount !== wineEvidence.archiveDelta?.memberCount || wineEvidence.archiveDelta?.missing?.length || wineEvidence.archiveDelta?.extra?.length || wineEvidence.candidateDll?.sha256 !== (wineEvidence.archiveDelta.changed?.[0]?.candidateSha256 ?? null)) throw new Error('Source package is missing a Wine-root receipt verified against this exact archive and delta.');
if (WINE_ROOT.sha256 !== 'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5' && (wineEvidence.archiveDelta.changed?.length !== 1 || wineEvidence.archiveDelta.changed[0].path !== 'home/username/.wine/drive_c/webgl/wined3d.dll')) throw new Error('Wine-root delta does not match the reviewed candidate DLL-only change.');
if (wineEvidence.sourceOffer?.complete !== true) throw new Error('Publication blocked: Wine-root archive identity/delta is verified, but corresponding source/build linkage is incomplete: ' + (wineEvidence.sourceOffer?.reason || 'no source offer status supplied'));

await mkdir(dirname(output), { recursive:true });
const temporary = await mkdtemp(resolve(dirname(output), '.publication-'));
const assets = resolve(temporary, 'public');
const receipt = {};
async function put(destination, bytes) {
  await mkdir(dirname(destination), { recursive:true, mode:0o755 });
  await writeFile(destination, bytes, { mode:0o644 });
  if (destination.startsWith(assets + sep)) receipt[relative(assets, destination).split(sep).join('/')] = { bytes:bytes.length, sha256:sha(bytes) };
}
async function minifyPrototypeScript(sourceName, bytes) {
  const result = await transform(bytes.toString('utf8'), {
    loader: 'js',
    sourcefile: sourceName,
    minify: true,
    legalComments: 'inline',
    sourcemap: false,
  });
  return Buffer.from(result.code);
}
const forbidden = /^(?:private|installs?|installation|saves?|profiles?|logs?|cache|caches|__pycache__|jit-cache|tmp-jit-modules|\.git|node_modules)$/i;
async function copyPrototype(path, destination) {
  const stat = await lstat(path);
  if (stat.isSymbolicLink()) throw new Error('Refuse publication symlink: ' + path);
  if (stat.isDirectory()) {
    for (const name of (await readdir(path)).sort()) {
      if (forbidden.test(name) || /\.(?:exe|dll|sav|bif|key|tlk|o|a|zip)$/i.test(name)) continue;
      await copyPrototype(resolve(path, name), resolve(destination, name));
    }
  } else if (stat.isFile()) {
    const name = path.split(sep).at(-1);
    if (/\.(?:log|txt|csv|tsv|sqlite|db|bak|tmp)$/i.test(name)) return;
    if (!/\.(?:js|mjs|css|html|json|svg|png|jpe?g|webp|gif|mp4|webm|ogg|mp3|wav|woff2?)$/i.test(name) && !['_headers', '.assetsignore'].includes(name)) throw new Error('Review unknown prototype asset type: ' + path);
    const bytes = await regular(path);
    const relativeName = relative(publicRoot, path).split(sep).join('/');
    await put(destination, /^js\/.+\.(?:js|mjs)$/i.test(relativeName)
      ? await minifyPrototypeScript(relativeName, bytes)
      : bytes);
  }
  else throw new Error('Refuse special publication input: ' + path);
}
try {
  // Explicit existing prototype surfaces. Unknown top-level paths are refused
  // so a new private subtree cannot silently become a publication input.
  const prototype = new Set(['index.html', 'style.css', 'favicon.svg', '_headers', '.assetsignore', 'js', 'media', 'player']);
  for (const name of await readdir(publicRoot)) {
    if (name === 'original-game' || forbidden.test(name)) continue;
    if (!prototype.has(name)) throw new Error('Review new prototype publication root: ' + name);
    await copyPrototype(resolve(publicRoot, name), resolve(assets, name));
  }
  for (const name of ['asset-lease.js', 'index.html', 'style.css', 'owned-game-sw.js', 'embedded.css']) await put(resolve(assets, 'original-game', name), await regular(resolve(publicRoot, 'original-game', name)));
  await put(resolve(assets, 'original-game/WINE-NOTICES.txt'), await regular(resolve(sourcePackage, 'WINE-NOTICES.txt')));
  // Notices are admitted by their exact generated index, not the media copier.
  const noticesRoot = resolve(publicRoot, 'original-game/wine-notices');
  const noticeIndex = await regular(resolve(noticesRoot, 'notice-index.json'));
  const notices = JSON.parse(noticeIndex.toString('utf8'));
  if (!Array.isArray(notices)) throw new Error('Invalid notice index.');
  for (const notice of notices) {
    const path = notice.archive_path;
    if (typeof path !== 'string' || !/^usr\/(?:local\/)?share\/(?:doc|licences)\//.test(path) && !path.startsWith('usr/share/doc/') || path.split('/').some(part => !part || part === '.' || part === '..') || notice.notice_url !== '/original-game/wine-notices/' + path || !/^[0-9a-f]{64}$/.test(notice.sha256 || '')) throw new Error('Invalid indexed notice path.');
    const bytes = await regular(resolve(noticesRoot, path));
    if (sha(bytes) !== notice.sha256) throw new Error('Indexed notice changed: ' + path);
    await put(resolve(assets, 'original-game/wine-notices', path), bytes);
  }
  await put(resolve(assets, 'original-game/wine-notices/notice-index.json'), noticeIndex);
  for (const [name, bytes] of runtimeFiles) await put(resolve(assets, 'original-game/runtime-releases', release, name), bytes);
  await put(resolve(assets, 'original-game/runtime-qualified.json'), descriptorBytes);
  const wholeRoot = createHash('sha256');
  for (let i = 0; i < chunks.chunks.length; i++) {
    const chunk = chunks.chunks[i];
    if (chunk.name !== `${i}-${chunk.sha256}.bin` || !/^[0-9a-f]{64}$/.test(chunk.sha256 || '') || chunk.bytes !== Math.min(WINE_ROOT.chunkBytes, WINE_ROOT.bytes - i * WINE_ROOT.chunkBytes)) throw new Error('Unexpected root chunk entry.');
    const bytes = await regular(resolve(roots, chunk.name));
    if (bytes.length !== chunk.bytes || sha(bytes) !== chunk.sha256) throw new Error('Root chunk changed: ' + chunk.name);
    wholeRoot.update(bytes);
    await put(resolve(assets, 'original-game/wine-roots', WINE_ROOT.sha256, chunk.name), bytes);
  }
  if (wholeRoot.digest('hex') !== WINE_ROOT.sha256) throw new Error('Reconstructed root differs from accepted archive.');
  await put(resolve(assets, 'original-game/wine-roots', WINE_ROOT.sha256, 'chunks.json'), chunkBytes);
  const sourceBase = 'original-game/source-' + release;
  for (const name of ['index.html', 'SOURCE.txt', 'COPYING', 'WINE-NOTICES.txt', 'wine-root-provenance.json', 'source-manifest.json']) await put(resolve(assets, sourceBase, name), await regular(resolve(sourcePackage, name)));
  for (let i = 0; i < sourceManifest.parts.length; i++) {
    const part = sourceManifest.parts[i];
    if (part.name !== 'source.tar.gz.part' + String(i).padStart(5, '0') || !/^[0-9a-f]{64}$/.test(part.sha256 || '')) throw new Error('Unexpected source part path.');
    const bytes = await regular(resolve(sourcePackage, part.name));
    if (bytes.length !== part.bytes || sha(bytes) !== part.sha256) throw new Error('Source part changed: ' + part.name);
    await put(resolve(assets, sourceBase, part.name), bytes);
  }
  await put(resolve(assets, 'original-game/SOURCES.html'), Buffer.from(`<!doctype html><html lang="en"><meta charset="utf-8"><title>Runtime sources</title><h1>Runtime sources</h1><p><a href="/original-game/source-${release}/index.html">Source downloads and build instructions for runtime ${release}</a></p><p><a href="/original-game/WINE-NOTICES.txt">Wine guest filesystem provenance and notices</a></p></html>\n`));
  for (const name of ['worker/wine-root.mjs', 'tools/wine-root-contract.mjs']) await put(resolve(temporary, name), await regular(resolve(demo, name)));
  // Current config has only standalone // comment lines. Preserve its settings
  // while resolving the two paths against this explicit prepared directory.
  const configText = (await regular(resolve(demo, 'wrangler.jsonc'))).toString('utf8');
  const config = JSON.parse(configText.split('\n').filter(line => !line.trim().startsWith('//')).join('\n'));
  config.main = './worker/wine-root.mjs';
  config.assets.directory = './public';
  await put(resolve(temporary, 'wrangler.jsonc'), Buffer.from(JSON.stringify(config, null, 2) + '\n'));
  await put(resolve(temporary, 'publication-manifest.json'), Buffer.from(JSON.stringify({ schema:'telos.publication-payload/v1', release, sourceBundle:descriptor.sourceBundle, root:WINE_ROOT, source:'/' + sourceBase + '/index.html', assets:receipt }, null, 2) + '\n'));
  await chmod(temporary, 0o755);
  await rename(temporary, output);
  console.log('Prepared exactly accepted runtime ' + release + ' at ' + output + '. No deployment performed.');
} finally { await rm(temporary, { recursive:true, force:true }); }
