// Preparation only: never builds, qualifies, promotes or deploys a runtime.
// Usage: node package-original-source.mjs FROZEN_SOURCE RAW_BUNDLE ADAPTER_WORKSPACE GPL2_TEXT RUNTIME_DESCRIPTOR NEW_OUTPUT [CANDIDATE_WINE_RECEIPT]
import { mkdir, mkdtemp, readdir, lstat, readFile, writeFile, chmod, open, rm, rename } from 'node:fs/promises';
import { WINE_ROOT } from './wine-root-contract.mjs';
import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { resolve, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

if (![8, 9].includes(process.argv.length)) throw new Error('Require explicit frozen production source, raw output bundle, adapter workspace, GPLv2 text, selected runtime descriptor and fresh output directory.');
const [frozen, bundle, workspace, licensePath, descriptorPath, output] = process.argv.slice(2, 8).map(path => resolve(path));
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const build = resolve(frozen, 'project/emscripten/Build/MultiThreadedJit');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
async function regular(path) {
  if (!(await lstat(path)).isFile()) throw new Error('Require regular file: ' + path);
  return readFile(path);
}
function inspectWineArchiveDelta(baseArchive, candidateArchive) {
  const program = String.raw`import sys,json,hashlib,zipfile
base,candidate=sys.argv[1:]
def sha(b): return hashlib.sha256(b).hexdigest()
with zipfile.ZipFile(base) as a, zipfile.ZipFile(candidate) as b:
    an=set(a.namelist()); bn=set(b.namelist())
    changed=[]
    for name in sorted(an & bn):
        av=a.read(name); bv=b.read(name)
        if av != bv: changed.append({'path':name,'baseBytes':len(av),'baseSha256':sha(av),'candidateBytes':len(bv),'candidateSha256':sha(bv)})
    print(json.dumps({'baseMembers':len(an),'candidateMembers':len(bn),'missing':sorted(an-bn),'extra':sorted(bn-an),'changed':changed}))`;
  const result = spawnSync('python3', ['-c', program, baseArchive, candidateArchive], { encoding:'utf8', maxBuffer:4 * 1024 * 1024 });
  if (result.error || result.status !== 0) throw new Error('Could not inspect Wine root archive delta with Python zipfile: ' + (result.error?.message || result.stderr));
  return JSON.parse(result.stdout);
}
for (const path of [frozen, bundle, workspace]) {
  if (!(await lstat(path)).isDirectory()) throw new Error('Require a regular source/bundle directory.');
}
if ([frozen, bundle].some(path => output === path || output.startsWith(path + sep))) throw new Error('Output must be outside source and bundle.');
try { await lstat(output); throw new Error('Output already exists.'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const license = await regular(licensePath);
const licenseText = license.toString('utf8');
if (!licenseText.includes('GNU GENERAL PUBLIC LICENSE') || !licenseText.includes('Version 2, June 1991') || !licenseText.includes('END OF TERMS AND CONDITIONS')) throw new Error('Supply complete unmodified GPLv2 license text.');
const bundleHashes = {};
for (const name of ['boxedwine.wasm', 'boxedwine.js', 'boxedwine.html', 'boxedwine-shell.js', 'boxedwine.css']) {
  const bytes = await regular(resolve(bundle, name));
  if (sha(bytes) !== sha(await regular(resolve(build, name)))) throw new Error('Bundle differs from frozen production output: ' + name);
  bundleHashes[name] = sha(bytes);
}
// The delivered JavaScript/HTML/CSS are themselves source: preserve the exact
// transformed source forms, independently of any later staging recipe drift.
const descriptor = JSON.parse((await regular(descriptorPath)).toString('utf8'));
const names = ['boxedwine-shell.js', 'boxedwine.css', 'boxedwine.js', 'boxedwine.wasm', 'embedded.css', 'game-runtime.html', 'GL4ES-LICENSE.txt', 'NOTICE.txt', 'owned-install.js'].sort();
if (descriptor.schema !== 'telos.original-runtime/v1' || !/^[0-9a-f]{64}$/.test(descriptor.release || '') || descriptor.entry !== `/original-game/runtime-releases/${descriptor.release}/game-runtime.html` || JSON.stringify(Object.keys(descriptor.assets || {}).sort()) !== JSON.stringify(names)) throw new Error('Require exact selected runtime identity.');
const engineNames = ['boxedwine.wasm', 'boxedwine.js', 'boxedwine.css'];
if (!descriptor.sourceBundle || JSON.stringify(Object.keys(descriptor.sourceBundle).sort()) !== JSON.stringify([...engineNames].sort()) || engineNames.some(name => descriptor.sourceBundle[name] !== bundleHashes[name])) throw new Error('Selected runtime does not bind the exact raw production bundle hashes.');
const runtimeFiles = new Map();
const runtimeDigest = createHash('sha256').update(JSON.stringify(descriptor.root) + '\0');
for (const name of [...names].sort((a,b) => a.localeCompare(b, 'en'))) {
  const bytes = await regular(resolve(demo, 'public/original-game/runtime-releases', descriptor.release, name));
  if (sha(bytes) !== descriptor.assets[name]) throw new Error('Selected transformed runtime changed: ' + name);
  runtimeDigest.update(name + '\0' + bytes.length + '\0').update(bytes);
  runtimeFiles.set(name, bytes);
}
if (runtimeDigest.digest('hex') !== descriptor.release) throw new Error('Selected runtime does not match its immutable payload identity.');
const mmapAllocator = 'var mmapAlloc=size=>{size=alignMemory(size,65536);var ptr=_emscripten_builtin_memalign(65536,size);if(ptr)zeroMemory(ptr,size);return ptr};';
const rawRuntimeJs = (await regular(resolve(bundle, 'boxedwine.js'))).toString('utf8');
if (rawRuntimeJs.split(mmapAllocator).length !== 2) throw new Error('Raw BoxedWine output does not contain the reviewed mmap allocator anchor.');
const transformedRuntimeJs = Buffer.from(rawRuntimeJs.replace(mmapAllocator, mmapAllocator + 'Module.telosMmapAlloc=mmapAlloc;'));
if (sha(transformedRuntimeJs) !== descriptor.assets['boxedwine.js'] || sha(runtimeFiles.get('boxedwine.js')) !== descriptor.assets['boxedwine.js']) throw new Error('Delivered BoxedWine JavaScript is not the exact reviewed allocator-export transform of the raw build output.');
for (const name of ['boxedwine.wasm', 'boxedwine.css']) if (bundleHashes[name] !== descriptor.assets[name]) throw new Error('Selected runtime changed an untransformed engine payload: ' + name);
const runtime = { release:descriptor.release, assets:descriptor.assets, sourceBundle:descriptor.sourceBundle };
const baseRootArchive = resolve(workspace, 'artifacts/TinyCore15Wine11.0-web.zip');
const selectedRootArchive = resolve(demo, 'public/original-game/wine-roots', WINE_ROOT.sha256, '.local-root.zip');
const upstreamWebPin = JSON.parse((await regular(resolve(workspace, 'upstream/tools/buildWine/webgl_filesystems_v13.json'))).toString('utf8'));
const upstreamWinePin = JSON.parse((await regular(resolve(workspace, 'upstream/tools/buildWine/filesystem_wine11.json'))).toString('utf8'));
const upstreamProfile = upstreamWebPin.profiles?.['web-v13'];
const baseRoot = await regular(baseRootArchive);
if (!upstreamProfile || upstreamProfile.sha256 !== sha(baseRoot) || upstreamProfile.size !== baseRoot.length || upstreamWinePin.wine_commit !== 'db11d0fe6a169c457e23d007e20404643d067aa8') throw new Error('The local official Wine-root source pins no longer identify the exact baseline archive.');
const selectedRootBytes = await regular(selectedRootArchive);
if (selectedRootBytes.length !== WINE_ROOT.bytes || sha(selectedRootBytes) !== WINE_ROOT.sha256) throw new Error('The selected Wine-root archive does not match the pinned runtime contract.');
const rootDelta = inspectWineArchiveDelta(baseRootArchive, selectedRootArchive);
if (rootDelta.missing.length || rootDelta.extra.length || (WINE_ROOT.sha256 === upstreamProfile.sha256 ? rootDelta.changed.length !== 0 : rootDelta.changed.length !== 1 || rootDelta.changed[0].path !== 'home/username/.wine/drive_c/webgl/wined3d.dll')) throw new Error('Selected Wine root differs from its source archive in unexpected members.');
const preparedWineSourceManifestPath = resolve(workspace, '.work/wine-clear-fix-20261004/prepared/prepared-source-manifest.json');
let preparedWineSource = null;
try {
  const bytes = await regular(preparedWineSourceManifestPath);
  const value = JSON.parse(bytes.toString('utf8'));
  if (value.wine_source_commit === upstreamWinePin.wine_commit && Array.isArray(value.patches)) preparedWineSource = { manifestSha256:sha(bytes), patchCount:value.patches.length, sourceCommit:value.wine_source_commit };
} catch (error) { if (error.code !== 'ENOENT') throw error; }
const changedRootEntry = rootDelta.changed[0] || null;
const wineRootProvenance = {
  schema:'telos.wine-root-provenance/v1',
  archive:{ sha256:WINE_ROOT.sha256, bytes:WINE_ROOT.bytes, status:'verified', evidence:'Exact selected archive bytes matched the immutable runtime Wine-root contract.' },
  baseline:{ sha256:upstreamProfile.sha256, bytes:upstreamProfile.size, url:'https://boxedwine.org/v2/13/TinyCore15Wine11.0-web.zip', status:'verified', evidence:'Local upstream web-v13 profile was hash- and size-matched against the baseline archive.' },
  archiveDelta:{ baselineMemberCount:rootDelta.baseMembers, memberCount:rootDelta.candidateMembers, missing:rootDelta.missing, extra:rootDelta.extra, changed:rootDelta.changed, status:'verified', evidence:'Every decompressed ZIP member was compared against the baseline archive.' },
  wineSource:{ repository:'https://github.com/wine-mirror/wine', commit:upstreamWinePin.wine_commit, commitStatus:'verified_reference_only', evidence:'Pinned by the upstream Wine 11 web-filesystem build profile; this alone does not prove the changed DLL was built from this source.' },
  candidateDll:{ path:changedRootEntry?.path || null, sha256:changedRootEntry?.candidateSha256 || null, bytes:changedRootEntry?.candidateBytes || null, sourceLinkage:'unverified' },
  preparedSourceReference:preparedWineSource ? { ...preparedWineSource, applicabilityToCandidateDll:'unverified' } : null,
  sourceOffer:{ complete:false, status:'incomplete', reason:'No candidate Wine build receipt tying the modified DLL to the prepared source and exact build configuration was found. Do not describe the candidate as having complete corresponding source or deploy it as a source-complete release.' },
};
let wineCandidate = null;
let wineNotice = (await regular(resolve(demo, 'public/original-game/WINE-NOTICES.txt'))).toString('utf8');
if (process.argv[8]) {
  const selected = JSON.parse((await regular(resolve(process.argv[8]))).toString('utf8'));
  if (selected.sourceCommit !== 'db11d0fe6a169c457e23d007e20404643d067aa8' || selected.baseRootSha256 !== 'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5' || selected.rootSha256 !== WINE_ROOT.sha256 || selected.rootBytes !== WINE_ROOT.bytes || !/^[0-9a-f]{64}$/.test(selected.dllSha256 || '') || !Number.isSafeInteger(selected.dllBytes) || selected.dllBytes <= 0 || !Array.isArray(selected.commands) || !selected.commands.length || selected.commands.some(command => typeof command !== 'string' || !command.trim())) throw new Error('Require exact candidate Wine provenance and build commands.');
  for (const name of ['sourceDirectory', 'appliedPatches', 'dllPath', 'rootArchive']) if (typeof selected[name] !== 'string' || !selected[name].startsWith('/')) throw new Error('Require explicit absolute Wine input paths.');
  if (!(await lstat(selected.sourceDirectory)).isDirectory()) throw new Error('Require patched Wine source directory.');
  const dll = await regular(selected.dllPath), archive = await regular(selected.rootArchive);
  if (dll.length !== selected.dllBytes || sha(dll) !== selected.dllSha256 || archive.length !== selected.rootBytes || sha(archive) !== selected.rootSha256) throw new Error('Candidate Wine binary/root identity changed.');
  const applied = JSON.parse((await regular(selected.appliedPatches)).toString('utf8'));
  if (!Array.isArray(applied) || !applied.length) throw new Error('Require actual applied patch receipts.');
  for (const patch of applied) if (typeof patch.path !== 'string' || !/^[0-9a-f]{64}$/.test(patch.candidate_sha256 || '') || sha(await regular(patch.path)) !== patch.candidate_sha256) throw new Error('Applied Wine patch changed.');
  const boundary = wineNotice.indexOf('Wine copyright');
  if (boundary < 0) throw new Error('Unexpected Wine notice template.');
  if (!changedRootEntry || selected.dllSha256 !== changedRootEntry.candidateSha256 || selected.dllBytes !== changedRootEntry.candidateBytes || selected.baseRootSha256 !== upstreamProfile.sha256) throw new Error('Candidate Wine receipt disagrees with the verified archive delta.');
  wineCandidate = { selected, applied };
  wineRootProvenance.sourceOffer = { complete:true, status:'source-and-build-receipts-supplied', reason:'Candidate receipt, source tree, applied patch bytes and commands are included; the supplied records are not an independent bit-identical-build proof.' };
  wineRootProvenance.candidateDll.sourceLinkage = 'claimed_by_supplied_build_receipt';
  wineNotice = `Wine runtime provenance and source receipt\n\nLocally modified candidate guest filesystem; this is not the unchanged official archive.\nBase official archive SHA256: ${selected.baseRootSha256}\nCandidate archive SHA256: ${selected.rootSha256}; bytes: ${selected.rootBytes}\nChanged entry (verified full archive comparison): ${changedRootEntry.path}\nCandidate DLL SHA256: ${selected.dllSha256}; bytes: ${selected.dllBytes}\nThe adjacent package includes the supplied patched Wine source, patch hashes, and build commands. These records do not prove a bit-identical rebuild. See wine-root-provenance.json for verified archive facts and remaining evidence limits.\nOriginal game files and user saves are excluded.\n\n` + wineNotice.slice(boundary);
} else if (changedRootEntry) {
  wineNotice = `Wine runtime provenance and source status\n\nThis locally modified candidate guest filesystem differs from the official base archive only at ${changedRootEntry.path}, verified by comparing every archive entry.\nBase official archive SHA256: ${upstreamProfile.sha256}\nCandidate archive SHA256: ${WINE_ROOT.sha256}; bytes: ${WINE_ROOT.bytes}\nChanged DLL SHA256: ${changedRootEntry.candidateSha256}; bytes: ${changedRootEntry.candidateBytes}\nThe upstream Wine 11 source pin is ${upstreamWinePin.wine_commit}. The exact source/build linkage for this candidate DLL is not verified and a complete Wine source offer is not prepared. See wine-root-provenance.json; this release must not be treated as source-complete.\nOriginal game files and user saves are excluded.\n\n` + wineNotice.slice(wineNotice.indexOf('Wine copyright'));
}
if (changedRootEntry) {
  wineNotice = wineNotice
    .replace('The complete modified source patches and build details are retained with the project.', 'A prepared patched Wine source tree and patch manifest are retained with the project, but their linkage to the candidate DLL is unverified; see wine-root-provenance.json.')
    .replace('Our modified BoxedWine emulator and gl4es source package is separate from this unchanged guest filesystem.', 'The modified BoxedWine emulator and gl4es source package is distinct from this locally modified candidate guest filesystem.');
}
const configs = {};
for (const name of ['.compile-config.json', '.link-config.json', '.gl-translation-config.json']) {
  const bytes = await regular(resolve(build, name));
  const text = bytes.toString('utf8');
  if (/--(?:embed|preload)-file|OWNED_ASSET_LINK_FLAGS|embedded-files\.rsp/.test(text)) throw new Error('Refuse a potentially private embedded-asset build.');
  const config = JSON.parse(text);
  for (const compiler of Object.values(config.compilers || {})) delete compiler.path;
  configs[name] = { original_sha256:sha(bytes), configuration:JSON.parse(JSON.stringify(config).replaceAll(frozen, '<SOURCE_ROOT>')) };
}
if (configs['.gl-translation-config.json'].configuration.use_gl4es !== '1') throw new Error('Require the reviewed gl4es production configuration.');

// Conservative source-only trees. No artifacts/, private/, generated builds,
// game archives, executable payloads, guest logs, caches or Git metadata.
const skippedDirectories = new Set(['.git', 'Build', 'build', 'CMakeFiles', '__pycache__', 'private', 'saves', 'profiles', 'jit-cache', 'tmp-jit-modules', 'node_modules']);
const skippedFiles = /\.(?:o|a|so(?:\.\d+)*|dll|exe|wasm|zip|gz|xz|7z|sav|bif|key|tlk|log|pyc|d)$/i;
await mkdir(dirname(output), { recursive:true });
const temporary = await mkdtemp(resolve(dirname(output), '.source-package-'));
const source = resolve(temporary, 'corresponding-source');
const publication = resolve(temporary, 'publication');
const files = {};
async function put(destination, bytes, executable = false) {
  await mkdir(dirname(destination), { recursive:true, mode:0o755 });
  await writeFile(destination, bytes, { mode:executable ? 0o755 : 0o644 });
  await chmod(destination, executable ? 0o755 : 0o644);
}
async function copyTree(path, destination) {
  const stat = await lstat(path);
  if (stat.isSymbolicLink()) throw new Error('Refuse source symlink: ' + path);
  if (stat.isDirectory()) {
    await mkdir(destination, { recursive:true, mode:0o755 });
    for (const name of (await readdir(path)).sort()) {
      if (skippedDirectories.has(name) || skippedFiles.test(name)) continue;
      await copyTree(resolve(path, name), resolve(destination, name));
    }
  } else if (stat.isFile()) {
    const bytes = await readFile(path);
    await put(destination, bytes, !!(stat.mode & 0o111));
    files[relative(source, destination).split(sep).join('/')] = sha(bytes);
  } else throw new Error('Refuse special source file: ' + path);
}
try {
  for (const tree of ['source', 'include', 'platform', 'lib', 'dependencies/gl4es', 'project/emscripten']) await copyTree(resolve(frozen, tree), resolve(source, tree));
  // Capture optional top-level build/license inputs, without private archives.
  for (const name of (await readdir(frozen)).sort()) {
    if (!/^(?:CMakeLists\.txt|makefile|Makefile|README.*|LICENSE.*|COPYING.*|combined-batch\.json|source-snapshots\.json)$/.test(name)) continue;
    if ((await lstat(resolve(frozen, name))).isFile()) await copyTree(resolve(frozen, name), resolve(source, name));
  }
  for (const name of ['stage-original-runtime.mjs', 'wine-root-contract.mjs', 'package-original-source.mjs', 'prepare-original-publication.mjs']) await copyTree(resolve(demo, 'tools', name), resolve(source, 'prototype/tools', name));
  for (const name of ['package.json', 'package-lock.json']) await copyTree(resolve(demo, name), resolve(source, 'prototype', name));
  await copyTree(resolve(demo, 'worker/wine-root.mjs'), resolve(source, 'prototype/worker/wine-root.mjs'));
  // The frozen engine tree can omit upstream Wine build tooling. Include the
  // separately selected distributor recipes without substituting its engine.
  const wineRecipes = resolve(workspace, 'upstream/tools');
  const winePin = JSON.parse((await regular(resolve(wineRecipes, 'buildWine/webgl_filesystems_v13.json'))).toString('utf8'));
  if (winePin.profiles?.['web-v13']?.sha256 !== 'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5') throw new Error('Wine distributor recipe does not identify the reviewed root.');
  for (const tree of ['buildWine', 'wineTests', 'cnc-ddraw', 'psVoodoo', 'x11', 'opengl', 'opengles']) await copyTree(resolve(wineRecipes, tree), resolve(source, 'wine-distributor/tools', tree));
  for (const [name, bytes] of runtimeFiles) {
    if (name === 'boxedwine.wasm') continue;
    const destination = resolve(source, 'published-runtime', name);
    await put(destination, bytes);
    files[relative(source, destination).split(sep).join('/')] = sha(bytes);
  }
  if (wineCandidate) {
    const { selected, applied } = wineCandidate;
    await copyTree(selected.sourceDirectory, resolve(source, 'wine-candidate/source'));
    const sanitized = { ...selected, sourceDirectory:'wine-candidate/source', appliedPatches:'wine-candidate/applied-patches.json', dllPath:'home/username/.wine/drive_c/webgl/wined3d.dll', rootArchive:'separately distributed guest archive' };
    sanitized.commands = selected.commands.map(command => command.replaceAll(selected.sourceDirectory, '<WINE_SOURCE>').replaceAll(dirname(selected.sourceDirectory), '<WINE_WORK>'));
    const receipts = [];
    for (let index = 0; index < applied.length; index++) {
      const patch = applied[index];
      const name = String(index).padStart(3, '0') + '.patch';
      await copyTree(patch.path, resolve(source, 'wine-candidate/patches', name));
      receipts.push({ ...patch, path:'patches/' + name });
    }
    await put(resolve(source, 'wine-candidate/applied-patches.json'), Buffer.from(JSON.stringify(receipts, null, 2) + '\n'));
    await put(resolve(source, 'wine-candidate/build-receipt.json'), Buffer.from(JSON.stringify(sanitized, null, 2) + '\n'));
  }
  await put(resolve(source, 'WINE-NOTICES.txt'), Buffer.from(wineNotice));
  await put(resolve(source, 'wine-root-provenance.json'), Buffer.from(JSON.stringify(wineRootProvenance, null, 2) + '\n'));
  await put(resolve(source, 'COPYING'), license);
  await put(resolve(source, 'build-evidence.json'), Buffer.from(JSON.stringify({ runtime, bundleHashes, configs, files, wineRootProvenance }, null, 2) + '\n'));
  const readme = 'Corresponding source for modified BoxedWine (GPL-2.0-or-later) and gl4es (MIT).\nUpstream BoxedWine revision: 509f6a7545e0827a27ed1b4ebab66ac1da4b1ea7.\nThis complete tree includes local downstream changes; preserve per-file notices and dependency licenses.\nBuild: activate the Emscripten version recorded in build-evidence.json, then from project/emscripten run:\nmake MAKEFLAGS=-j1 PTHREAD_POOL_SIZE=4 USE_GL4ES=1 multiThreadedJit\nExact compiler/link settings and raw output hashes are recorded in build-evidence.json; SOURCE_ROOT denotes the extracted directory. No bit-identical rebuild claim is made.\npublished-runtime contains the exact delivered source forms including transformed adapter, shell, HTML and CSS, bound to the release in build-evidence.json. After rebuilding boxedwine.wasm, pair it with those source forms and the separately acquired Wine root. prototype/tools are separately captured preparation recipes, not a claim that rerunning their current inputs recreates the selected transformations.\nOriginal game files, saves, profiles and JIT caches are excluded. wine-root-provenance.json separates verified archive identity/delta facts from source-to-binary linkage that is still unverified; do not infer source-complete distribution from the archive comparison alone.\n';
  const publicationToolNote = 'prototype/tools/prepare-original-publication.mjs uses the pinned esbuild dependency in prototype/package.json and prototype/package-lock.json. With Node.js 18 or newer, install it reproducibly by running npm ci from prototype/. The preparation recipes still require their original source tree and explicit inputs.\n';
  const recipeBoundary = 'prototype/tools are separately captured preparation recipes, not a claim that rerunning their current inputs recreates the selected transformations.\n';
  if (!readme.includes(recipeBoundary)) throw new Error('Publication source README recipe boundary changed unexpectedly.');
  const readmeWithToolSetup = readme.replace(recipeBoundary, recipeBoundary + publicationToolNote);
  await put(resolve(source, 'README-publication.txt'), Buffer.from(readmeWithToolSetup));
  const archive = resolve(temporary, 'source.tar.gz');
  await new Promise((done, fail) => {
    const child = spawn('tar', ['--sort=name', '--owner=0', '--group=0', '--numeric-owner', '-czf', archive, '-C', temporary, 'corresponding-source'], { stdio:['ignore', 'ignore', 'inherit'] });
    child.on('error', fail); child.on('close', code => code === 0 ? done() : fail(new Error('Source tar failed: ' + code)));
  });
  await mkdir(publication, { mode:0o755 });
  const parts = [];
  const file = await open(archive, 'r');
  try {
    for (let position = 0, index = 0;; index++) {
      const buffer = Buffer.alloc(8 * 1024 * 1024);
      let length = 0;
      while (length < buffer.length) {
        const result = await file.read(buffer, length, buffer.length - length, position + length);
        if (!result.bytesRead) break;
        length += result.bytesRead;
      }
      if (!length) break;
      const bytes = buffer.subarray(0, length);
      const name = 'source.tar.gz.part' + String(index).padStart(5, '0');
      await put(resolve(publication, name), bytes);
      parts.push({ name, bytes:length, sha256:sha(bytes) });
      position += length;
    }
  } finally { await file.close(); }
  await put(resolve(publication, 'COPYING'), license);
  await put(resolve(publication, 'WINE-NOTICES.txt'), Buffer.from(wineNotice));
  await put(resolve(publication, 'wine-root-provenance.json'), Buffer.from(JSON.stringify(wineRootProvenance, null, 2) + '\n'));
  await put(resolve(publication, 'source-manifest.json'), Buffer.from(JSON.stringify({ schema:'telos.corresponding-source/v1', runtime, wineRoot:WINE_ROOT, wineRootProvenance, wineDll:wineCandidate ? { sha256:wineCandidate.selected.dllSha256, bytes:wineCandidate.selected.dllBytes } : null, bundleHashes, parts }, null, 2) + '\n'));
  await put(resolve(publication, 'SOURCE.txt'), Buffer.from(readmeWithToolSetup + '\nDownload every part listed in source-manifest.json and verify SHA256. Reconstruct in filename order:\ncat source.tar.gz.part* > source.tar.gz\ntar -xzf source.tar.gz\nSource is supplied alongside the runtime; see COPYING for the GPL license, included dependency licenses, and wine-root-provenance.json for exact Wine-root evidence limits.\n'));
  const links = parts.map(part => `<li><a href="${part.name}">${part.name}</a> — ${part.bytes} bytes; SHA256 ${part.sha256}</li>`).join('\n');
  await put(resolve(publication, 'index.html'), Buffer.from(`<!doctype html><html lang="en"><meta charset="utf-8"><title>Runtime source and notices</title><h1>Runtime source and notices</h1><p>Modified BoxedWine and gl4es corresponding source. <a href="SOURCE.txt">Build and reconstruction instructions</a>; <a href="COPYING">GPL license</a>; <a href="source-manifest.json">Source manifest</a>; <a href="wine-root-provenance.json">Wine-root evidence receipt</a>.</p><ol>${links}</ol><p>The Wine guest filesystem has separate <a href="WINE-NOTICES.txt">provenance, licenses and source references</a>. Its original notices remain inside the archive and are also available from <a href="/original-game/wine-notices/notice-index.json">the notice index</a>.</p></html>\n`));
  await rename(publication, output);
  console.log('Prepared source parts and notices at ' + output + '. No qualification, promotion or deployment performed.');
} finally { await rm(temporary, { recursive:true, force:true }); }
