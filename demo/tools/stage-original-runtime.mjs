// Stage redistributable runtime only. Never copies a game installation or saves.
import { mkdir, mkdtemp, readFile, writeFile, rm, rename, chmod, lstat, stat, readdir, link } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { LOCAL_WINE_ROOTS } from './wine-root-contract.mjs';
import { execFileSync } from 'node:child_process';
import { resolve, dirname, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const selectLocal = args.includes('--select-local');
const reuseStagedRoot = args.includes('--reuse-staged-root');
const positional = args.filter(arg => arg !== '--select-local' && arg !== '--reuse-staged-root');
let phaseStart = performance.now();
const phaseTimes = [];
function finishPhase(name) {
  const now = performance.now();
  phaseTimes.push(`${name}=${(now - phaseStart).toFixed(1)}ms`);
  phaseStart = now;
}
const workspace = resolve(positional[0] || '/home/brunner56/kptas-browser-game-wasm');
if (!positional[1]) throw new Error('Select the reviewed runtime build explicitly: stage-original-runtime.mjs <workspace> <build-directory> [wine-archive] [--select-local].');
const runtime = resolve(workspace, positional[1]);
const demo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const releases = resolve(demo, 'public/original-game/runtime-releases');
const payloads = new Map();
const wineArchive = resolve(workspace, positional[2] || 'artifacts/TinyCore15Wine11.0-web.zip');
if (positional.length > 3) throw new Error('Unexpected staging arguments.');
const sourceSnapshotPath = process.env.KPTAS_SOURCE_MANIFEST_SNAPSHOT;
const sourceExtensions = new Set(['.c','.h','.cc','.cpp','.hpp','.hxx','.glsl','.frag','.vert','.js','.mjs','.ts','.tsx','.jsx','.css','.html','.py','.sh','.json','.jsonc','.md','.cmake','.txt','.patch','.rs','.toml','.yaml','.yml','.xml','.in','.mk','.am','.ac','.m4','.go','.sql','.lua','.gradle','.properties']);
const sourceName = relative => sourceExtensions.has(extname(relative).toLowerCase()) || ['makefile','gnumakefile','dockerfile','justfile'].includes(relative.split('/').at(-1).toLowerCase());
function forbiddenSourcePath(relative) {
  const parts = relative.split('/');
  return parts.some(part => ['.git','.playwright-cli','private','uploads','owned-game-assets','original-game','player','secrets'].includes(part)) ||
    /^\.env(?:\.|$)/i.test(parts.at(-1)) || /(token|secret|credential|private[-_]?key)/i.test(parts.at(-1)) ||
    /\.(?:png|jpe?g|gif|webp|bmp|ico|zip|7z|rar|wasm|bin|dat|sav|pem|key)$/i.test(parts.at(-1));
}
function git(repo, args) { return execFileSync('git', ['-C', repo, ...args], { encoding:'buffer', maxBuffer:64 * 1024 * 1024 }); }
function nul(buffer) { return buffer.toString('utf8').split('\0').filter(Boolean); }
async function captureCheckout(repo) {
  const hash = bytes => createHash('sha256').update(bytes).digest('hex');
  const head = git(repo, ['rev-parse','HEAD']).toString().trim();
  const tree = git(repo, ['rev-parse','HEAD^{tree}']).toString().trim();
  const status = git(repo, ['status','--porcelain=v2','-z','--untracked-files=all']);
  const changed = new Set(nul(git(repo, ['diff','--cached','--name-only','-z','--diff-filter=ACDMRTUXB','HEAD','--'])));
  for (const file of nul(git(repo, ['diff','--name-only','-z','--diff-filter=ACDMRTUXB','--']))) changed.add(file);
  for (const file of nul(git(repo, ['ls-files','--others','--exclude-standard','-z']))) changed.add(file);
  const headFiles = new Map();
  for (const record of nul(git(repo, ['ls-tree','-rz','--full-tree','HEAD']))) {
    const tab = record.indexOf('\t');
    if (tab >= 0) headFiles.set(record.slice(tab + 1), record.slice(0, tab).split(' ')[2]);
  }
  const indexFiles = new Map();
  for (const record of nul(git(repo, ['ls-files','--stage','-z']))) {
    const tab = record.indexOf('\t');
    if (tab < 0) continue;
    const [mode, blob, stage] = record.slice(0, tab).split(' ');
    const entries = indexFiles.get(record.slice(tab + 1)) || [];
    entries.push({ stage:Number(stage), mode, blob });
    indexFiles.set(record.slice(tab + 1), entries);
  }
  const files = [];
  for (const relative of [...changed].sort()) {
    if (!sourceName(relative) || forbiddenSourcePath(relative)) continue;
    const full = resolve(repo, relative);
    let stat;
    try { stat = await lstat(full); } catch (error) { if (error.code === 'ENOENT') stat = null; else throw error; }
    if (stat?.isSymbolicLink()) continue;
    const headBlob = headFiles.get(relative) || null;
    const indexBlobs = (indexFiles.get(relative) || []).sort((a,b) => a.stage - b.stage);
    if (!stat) files.push({ path:relative, status:'deleted', headBlob, indexBlobs, workingTreeSha256:null, bytes:null });
    else if (stat.isFile()) {
      const bytes = await readFile(full);
      files.push({ path:relative, status:headBlob ? 'tracked-changed' : 'untracked', headBlob, indexBlobs, workingTreeSha256:hash(bytes), bytes:bytes.length });
    }
  }
  return { checkout:resolve(repo), head, tree, rawPorcelainV2Sha256:hash(status), files };
}
async function captureExternalTree(root) {
  const hash = bytes => createHash('sha256').update(bytes).digest('hex');
  const files = [];
  async function walk(dir, base = '') {
    for (const entry of (await readdir(dir, { withFileTypes:true })).sort((a,b) => a.name.localeCompare(b.name, 'en'))) {
      const relative = base ? `${base}/${entry.name}` : entry.name;
      if (forbiddenSourcePath(relative)) continue;
      const full = resolve(dir, entry.name);
      if (entry.isDirectory()) await walk(full, relative);
      else if (entry.isFile() && (sourceName(relative) || entry.name === 'source.json')) {
        const bytes = await readFile(full);
        files.push({ path:relative, sha256:hash(bytes), bytes:bytes.length });
      }
    }
  }
  await walk(root);
  return { directory:resolve(root), files };
}
let sourceSnapshot;
if (sourceSnapshotPath) {
  const snapshotBytes = await readFile(sourceSnapshotPath);
  sourceSnapshot = JSON.parse(snapshotBytes.toString('utf8'));
  if (sourceSnapshot.schema !== 'telos.original-runtime-source-build/v1') throw new Error('Unsupported source/build snapshot schema.');
  const currentSources = {
    sourceCheckouts:await Promise.all([captureCheckout(resolve(workspace, 'upstream')), captureCheckout(resolve(demo, '..'))]),
    externalSources:[await captureExternalTree(resolve(workspace, 'dependencies/gl4es'))]
  };
  if (JSON.stringify(currentSources.sourceCheckouts) !== JSON.stringify(sourceSnapshot.sourceCheckouts) ||
      JSON.stringify(currentSources.externalSources) !== JSON.stringify(sourceSnapshot.externalSources)) {
    throw new Error(`Source snapshot mismatch; staged output cannot be attributed to ${sourceSnapshotPath}. The build receipt and snapshot were retained.`);
  }
  sourceSnapshot.snapshotSha256 = createHash('sha256').update(snapshotBytes).digest('hex');
}
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
let rootBytes = null;
let WINE_ROOT;
if (reuseStagedRoot) {
  const currentDescriptor = JSON.parse(await readFile(resolve(demo, 'public/original-game/runtime-local.json'), 'utf8'));
  WINE_ROOT = LOCAL_WINE_ROOTS.find(root => root.sha256 === currentDescriptor.root?.sha256 && root.bytes === currentDescriptor.root?.bytes);
  if (!WINE_ROOT) throw new Error('The selected local runtime does not reference a reviewed reusable Wine root.');
  const archiveMetadata = await stat(wineArchive);
  if (archiveMetadata.size !== WINE_ROOT.bytes) throw new Error('Wine archive size does not match the selected staged root.');
} else {
  rootBytes = await readFile(wineArchive);
  WINE_ROOT = LOCAL_WINE_ROOTS.find(root => root.bytes === rootBytes.length && root.sha256 === hash(rootBytes));
  if (!WINE_ROOT) throw new Error('Wine root does not match a reviewed archive digest and size.');
}
finishPhase('wine-archive-read-hash');
const rootBase = `/original-game/wine-roots/${WINE_ROOT.sha256}/`;
const root = { ...WINE_ROOT, base:rootBase, localFile:'.local-root.zip', publicFile:'wine-root.zip' };
const sourceBundle = {};
function replaceOnce(source, before, after, label) {
  if (source.split(before).length !== 2) throw new Error(`Unsupported BoxedWine ${label}.`);
  return source.replace(before, after);
}
for (const name of ['boxedwine.wasm', 'boxedwine.css']) {
  const bytes = await readFile(resolve(runtime, name));
  payloads.set(name, bytes);
  sourceBundle[name] = hash(bytes);
}
let runtimeJs = await readFile(resolve(runtime, 'boxedwine.js'), 'utf8');
sourceBundle['boxedwine.js'] = hash(Buffer.from(runtimeJs));
const mmapAllocator = 'var mmapAlloc=size=>{size=alignMemory(size,65536);var ptr=_emscripten_builtin_memalign(65536,size);if(ptr)zeroMemory(ptr,size);return ptr};';
if (runtimeJs.split(mmapAllocator).length !== 2) throw new Error('Unsupported Emscripten mmap allocator in BoxedWine runtime.');
runtimeJs = runtimeJs.replace(mmapAllocator, mmapAllocator + 'Module.telosMmapAlloc=mmapAlloc;');
const workerFailure = '__emscripten_thread_crashed();throw ex';
const workerEventFailure = 'worker.onerror=e=>{var message="worker sent an error!";err(`${message} ${e.filename}:${e.lineno}: ${e.message}`);throw e}';
const hasWorkerCrashPath = runtimeJs.includes(workerFailure);
const hasWorkerErrorPath = runtimeJs.includes(workerEventFailure);
if (hasWorkerCrashPath !== hasWorkerErrorPath) throw new Error('Incomplete BoxedWine worker error hooks.');
// The single-threaded JIT has no Emscripten worker error hooks. Keep the same
// staging pipeline usable for both runtime variants; when workers exist, retain
// the detailed worker diagnostics for both failure paths.
if (hasWorkerCrashPath) {
  runtimeJs = replaceOnce(runtimeJs, workerFailure, 'var guestFailureText="Guest worker failure: "+(ex&&[ex.name,ex.message,ex.stack,ex.filename,ex.lineno,ex.colno].filter(Boolean).join(" | ")||String(ex));err(guestFailureText);try{fetch("/private/diagnostics",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:"worker-fatal",detail:guestFailureText.slice(0,6000)})}).catch(function(){});}catch(ignored){}__emscripten_thread_crashed();throw ex', 'guest worker stack diagnostics');
  runtimeJs = replaceOnce(runtimeJs, workerEventFailure, 'worker.onerror=e=>{var details=[e&&e.name,e&&e.message,e&&e.filename,e&&e.lineno,e&&e.colno,e&&e.error&&e.error.stack,String(e)].filter(Boolean).join(" | ");var message="worker sent an error! "+details;err(message);try{fetch("/private/diagnostics",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:"worker-fatal",detail:message.slice(0,6000)})}).catch(function(){});}catch(ignored){}throw e}', 'worker error event diagnostics');
}
// The immediate-mode emulation hook only owns texture capabilities. Emscripten
// still forwards every capability to WebGL after this hook; touching the active
// texture unit for unrelated state (lighting, blending, depth, etc.) can crash
// before the first frame when the texture table is not initialized yet.
const hookEnable = 'hook_enable:cap=>{var cur=getCurTexUnit();switch(cap){case GL_TEXTURE_1D:';
const hookDisable = 'hook_disable:cap=>{var cur=getCurTexUnit();switch(cap){case GL_TEXTURE_1D:';
const hasEnableHook = runtimeJs.includes(hookEnable);
const hasDisableHook = runtimeJs.includes(hookDisable);
if (hasEnableHook !== hasDisableHook) throw new Error('Incomplete Emscripten GL texture hooks in BoxedWine runtime.');
if (hasEnableHook) {
  runtimeJs = replaceOnce(runtimeJs, hookEnable, 'hook_enable:cap=>{if(cap!==GL_TEXTURE_1D&&cap!==GL_TEXTURE_2D&&cap!==GL_TEXTURE_3D&&cap!==GL_TEXTURE_CUBE_MAP)return;var cur=getCurTexUnit();switch(cap){case GL_TEXTURE_1D:', 'GL enable texture-capability guard');
  runtimeJs = replaceOnce(runtimeJs, hookDisable, 'hook_disable:cap=>{if(cap!==GL_TEXTURE_1D&&cap!==GL_TEXTURE_2D&&cap!==GL_TEXTURE_3D&&cap!==GL_TEXTURE_CUBE_MAP)return;var cur=getCurTexUnit();switch(cap){case GL_TEXTURE_1D:', 'GL disable texture-capability guard');
}
// Emscripten's window keyboard callbacks also see the embedded recorder and
// diagnostics controls. Keep their key presses and unowned releases in the
// browser UI; deliver releases for keys pressed in the guest across focus changes.
// Do not stop DOM propagation or prevent the control's normal activation.
runtimeJs = replaceOnce(runtimeJs,
  'var keyEventHandlerFunc=e=>{var keyEventData=',
  `var keyEventHandlerFunc=e=>{
    var guestKey=e.code||e.keyCode;
    var guestKeys=JSEvents.telosGuestPressedKeys||(JSEvents.telosGuestPressedKeys=new Set);
    var uiKeyTarget=e.target instanceof Element&&e.target.closest("button,summary,input,textarea,select,a[href],[contenteditable]:not([contenteditable=false]),[role=button]");
    if(uiKeyTarget&&(eventTypeString!=="keyup"||!guestKeys.has(guestKey)))return;
    if(eventTypeString==="keydown")guestKeys.add(guestKey);
    else if(eventTypeString==="keyup")guestKeys.delete(guestKey);
    var keyEventData=`,
  'embedded browser control keyboard ownership');
payloads.set('boxedwine.js', Buffer.from(runtimeJs));
// Optional redistributable runtime function-index metadata; no guest assets.
try { payloads.set('boxedwine.symbols', await readFile(resolve(runtime, 'boxedwine.html.symbols'))); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
let html = await readFile(resolve(runtime, 'boxedwine.html'), 'utf8');
if (!html.includes('<script src=boxedwine-shell.js>')) throw new Error('Unsupported BoxedWine HTML bootstrap.');
html = html.replace('<script src=boxedwine-shell.js>', '<script src=owned-install.js></script><script src=boxedwine-shell.js>');
html = replaceOnce(html, '<title>BoxedWine</title>', '<title>KOTOR</title>', 'game title');
html = html.replace('content="boxedwine emulator"name=description', 'content="Play KOTOR in your browser"name=description');
html = html.replace('content="emulator,wine,windows,x86,PC,emscripten,web assembly,wasm,boxedwine"name=keywords', 'content="KOTOR,game"name=keywords');
// Runtime console is available on demand; the embedded game remains the focus.
html = replaceOnce(html, '<div id=console><textarea', '<details id=console><summary>Diagnostics</summary><textarea', 'console opening');
html = replaceOnce(html, '</textarea></div><div id=openModal', '</textarea></details><div id=openModal', 'console closing');
html = html.replace('<script src=owned-install.js>', '<link rel=stylesheet href=embedded.css><script src=owned-install.js>');
html = replaceOnce(html, '</body>', '<script src=game-recording.js></script></body>', 'local game recording controls');
payloads.set('game-runtime.html', Buffer.from(html));
let shell = await readFile(resolve(runtime, 'boxedwine-shell.js'), 'utf8');
shell = replaceOnce(shell, 'params.push("/bin/wine");', 'params.push("-title", "KOTOR");\n            params.push("/bin/wine");', 'game window title');
const profileFailureHook = "console.log('unable to sync folder: ' + ROOT);";
if (shell.split(profileFailureHook).length !== 2) throw new Error('Unsupported BoxedWine profile restoration interface.');
shell = shell.replace(profileFailureHook, profileFailureHook + "\nnotifyOwnedGameStatus('error', 'The browser profile could not be opened. Keep this profile and check browser storage before reopening the game.');");
const hook = 'function buildAppFileSystem(callback) {';
if (!shell.includes(hook) || !shell.includes('params.push(Config.appZipFile);')) throw new Error('Unsupported BoxedWine original-asset bootstrap.');
shell = shell.replace(hook, hook + '\nmountOwnedInstall().then(callback).catch(error => { console.error(error); notifyOwnedGameStatus("error", error.message); }); return;');
shell = shell.replace('params.push(Config.appZipFile);', 'params.push(Config.ownedInstallDirectory || Config.appZipFile);');
shell = shell.replace('Config.envProp = getEnvProp();', 'Config.envProp = "WINEDEBUG=-all,err+all";');
shell = replaceOnce(shell, 'params.push(Config.envProp);', 'params.push(Config.envProp);\n                params.push("-env");\n                params.push("WINEDLLOVERRIDES=mscoree=");', 'KOTOR Mono-dialog override');
shell = replaceOnce(shell, 'if(Config.cpu.length > 0){\n                params.push("-" + Config.cpu);\n            }', 'if(Config.cpu.length > 0){\n                params.push("-" + Config.cpu);\n            }\n            if (new URLSearchParams(window.location.search).get("cpuAffinity") === "1") {\n                params.push("-cpuAffinity", "1");\n            }', 'loopback CPU affinity experiment');
// The preserved menu candidate has the earlier nested fetch callback.
if (!shell.includes('}).then(function(buffer) {\n                callback(new Uint8Array(buffer));')) {
  const start = shell.indexOf('function loadFile(pathPrefix, filename, callback) {');
  const end = shell.indexOf('function buildAppFileSystem(callback) {', start);
  if (start < 0 || end <= start) throw new Error('Unsupported preserved loadFile interface.');
  shell = shell.slice(0, start) + `function loadFile(pathPrefix, filename, callback) {
            fetch(pathPrefix + filename).then(response => {
                if (!response.ok) throw new Error('Runtime archive unavailable: ' + response.status);
                return response.arrayBuffer();
            }).then(function(buffer) {
                callback(new Uint8Array(buffer));
            }).catch(error => notifyOwnedGameStatus('error', error.message));
        }
        ` + shell.slice(end);
}
shell = replaceOnce(shell, '}).then(function(buffer) {\n                callback(new Uint8Array(buffer));', `}).then(async function(buffer) {
                if (filename === Config.rootZipFile) {
                    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', buffer))].map(n => n.toString(16).padStart(2, '0')).join('');
                    if (buffer.byteLength !== ${WINE_ROOT.bytes} || digest !== '${WINE_ROOT.sha256}') throw new Error('Wine root integrity check failed.');
                }
                callback(new Uint8Array(buffer));`, 'root archive integrity hook');
payloads.set('boxedwine-shell.js', Buffer.from(shell));
let mount = await readFile(resolve(workspace, 'public/owned-install.js'), 'utf8');
mount = replaceOnce(mount,
  "new URL('/original-game/telos-tas-bridge.js?v=13-parent-origin', location.origin).href",
  "new URL('telos-tas-bridge.js', location.href).href",
  'immutable candidate input bridge');
mount = mount.replace("const response = await fetchOwnedManifest('/private/install.json');", "const localInstall = new URLSearchParams(location.search).get('localInstall') === '1';\n  const session = new URLSearchParams(location.search).get('ownedSession');\n  if (!localInstall && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(session)) throw new Error('Connect your KOTOR installation in the prototype.');\n  const assetPrefix = '/original-game/owned-game-assets/' + session + '/';\n  const response = await fetchOwnedManifest(localInstall ? '/private/install.json' : assetPrefix + 'manifest');");
mount = mount.replace('const localSnapshots = await receiveOwnedFileSnapshots(manifest);', 'const localSnapshots = localInstall ? null : await receiveOwnedFileSnapshots(manifest);');
mount = mount.replace("!/^\\/private\\/install\\/\\d+$/.test(file.url)", "(localInstall ? !/^\\/private\\/install\\/\\d+$/.test(file.url) : (!file.url.startsWith(assetPrefix) || !/^\\d+$/.test(file.url.slice(assetPrefix.length))))");
if (!mount.includes("const localInstall = new URLSearchParams(location.search).get('localInstall') === '1';")) throw new Error('Local installation transport was not staged.');
payloads.set('owned-install.js', Buffer.from(mount));
payloads.set('embedded.css', await readFile(resolve(demo, 'public/original-game/embedded.css')));
for (const name of ['game-recording.js', 'game-recording-writer.js', 'telos-tas-bridge.js']) {
  payloads.set(name, await readFile(resolve(demo, 'public/original-game', name)));
}
const gl4esDependency = resolve(workspace, 'dependencies/gl4es');
const gl4esMetadata = JSON.parse(await readFile(resolve(gl4esDependency, 'source.json'), 'utf8'));
payloads.set('GL4ES-LICENSE.txt', await readFile(resolve(gl4esDependency, gl4esMetadata.source_directory, 'LICENSE')));
const notice = 'BoxedWine: GPL-2.0-or-later, https://github.com/danoon2/Boxedwine, source revision 509f6a7545e0827a27ed1b4ebab66ac1da4b1ea7. gl4es: MIT, https://github.com/ptitSeb/gl4es, source revision ' + gl4esMetadata.commit + '; license retained in GL4ES-LICENSE.txt. Local downstream patches and provenance: kptas-browser-game-wasm/dependencies/gl4es/source.json and artifacts/. Wine root: official TinyCore15 Wine11.0 web package; retain upstream component notices. Local staging is not a published distribution. Original KOTOR files are supplied only by the user and are not included.\n';
const linkConfiguration = JSON.parse(await readFile(resolve(runtime, '.link-config.json'), 'utf8'));
const proxyLifetimeLinked = String(linkConfiguration.settings?.OBJECTS || '').split(/\s+/)
  .some(object => object.endsWith('/boxedwine-proxying-lifetime.o'));
if (proxyLifetimeLinked) {
  const project = resolve(workspace, 'upstream/project/emscripten');
  payloads.set('EMSCRIPTEN-PROXYING-LICENSE.txt', await readFile(resolve(project, 'emscripten-proxying-LICENSE.txt')));
  payloads.set('emscripten-proxying-backport.json', await readFile(resolve(project, 'boxedwine-proxying-lifetime.json')));
}
payloads.set('NOTICE.txt', Buffer.from(notice + (proxyLifetimeLinked ? 'Emscripten4.0.14 proxying lifetime backport: upstream PR26582. License retained in EMSCRIPTEN-PROXYING-LICENSE.txt; source hashes in emscripten-proxying-backport.json. Runtime acceptance remains pending.\n' : '')));
// Hash the entire generated bootstrap, not merely the JS/WASM pair. Every
// companion asset uses this immutable directory throughout a guest's lifetime.
const digest = createHash('sha256');
digest.update(JSON.stringify(root) + '\0');
const assets = {};
for (const [name, bytes] of [...payloads].sort(([a], [b]) => a.localeCompare(b, 'en'))) {
  digest.update(name + '\0' + bytes.length + '\0').update(bytes);
  assets[name] = createHash('sha256').update(bytes).digest('hex');
}
const release = digest.digest('hex');
finishPhase('runtime-payload-transform-hash');
const buildOutputs = [];
if (sourceSnapshot) {
  for (const name of ['boxedwine.js','boxedwine.wasm','boxedwine.css','.compile-config.json','.link-config.json','gl4es/CMakeCache.txt','.gl-translation-config.json']) {
    const bytes = await readFile(resolve(runtime, name));
    buildOutputs.push({ path:name, sha256:hash(bytes), bytes:bytes.length });
  }
}
await mkdir(releases, { recursive:true, mode:0o755 });
const temporary = await mkdtemp(resolve(releases, '.stage-'));
try {
  for (const [name, bytes] of payloads) {
    await writeFile(resolve(temporary, name), bytes, { mode:0o644 });
    await chmod(resolve(temporary, name), 0o644);
  }
  // mkdtemp defaults to 0700. The local HTTP server must read the release even
  // when staging and serving use separate task-owned users.
  await chmod(temporary, 0o755);
  try { await rename(temporary, resolve(releases, release)); }
  catch (error) {
    if (!['EEXIST','ENOTEMPTY'].includes(error.code)) throw error;
    const releaseTarget = resolve(releases, release);
    if (!(await lstat(releaseTarget)).isDirectory()) throw new Error('Unsafe existing runtime release directory.');
    const existingNames = await readdir(releaseTarget);
    if (existingNames.length !== payloads.size || existingNames.some(name => !payloads.has(name))) throw new Error('Unexpected files in existing runtime release.');
    for (const [name, bytes] of payloads) {
      const path = resolve(releaseTarget, name);
      const metadata = await lstat(path);
      if (!metadata.isFile() || metadata.size !== bytes.length || hash(await readFile(path)) !== hash(bytes)) throw new Error('Unsafe existing runtime release file: ' + name);
    }
  }
} finally { await rm(temporary, { recursive:true, force:true }); }
finishPhase('runtime-release-write-verify');
// Preserve the previous local symlink, but never reuse it for a qualified
// release. Verified snapshots/chunks are regular immutable files.
const roots = resolve(demo, 'public/original-game/wine-roots');
await mkdir(roots, { recursive:true, mode:0o755 });
const rootFiles = new Map();
const chunks = [];
if (reuseStagedRoot) {
  const rootTarget = resolve(roots, WINE_ROOT.sha256);
  const manifestPath = resolve(rootTarget, 'chunks.json');
  const manifestBytes = await readFile(manifestPath);
  const manifest = JSON.parse(manifestBytes.toString('utf8'));
  if (manifest.sha256 !== WINE_ROOT.sha256 || manifest.bytes !== WINE_ROOT.bytes || manifest.chunkBytes !== WINE_ROOT.chunkBytes || !Array.isArray(manifest.chunks)) {
    throw new Error('Existing staged Wine root has an invalid chunk manifest.');
  }
  const expectedCount = Math.ceil(WINE_ROOT.bytes / WINE_ROOT.chunkBytes);
  if (manifest.chunks.length !== expectedCount) throw new Error('Existing staged Wine root has an incomplete chunk manifest.');
  rootFiles.set('.local-root.zip', { bytes:WINE_ROOT.bytes, sha256:WINE_ROOT.sha256 });
  rootFiles.set('chunks.json', { bytes:manifestBytes.length, sha256:hash(manifestBytes) });
  for (let index = 0; index < manifest.chunks.length; index++) {
    const chunk = manifest.chunks[index];
    const expectedBytes = Math.min(WINE_ROOT.chunkBytes, WINE_ROOT.bytes - index * WINE_ROOT.chunkBytes);
    if (chunk.name !== `${index}-${chunk.sha256}.bin` || !/^[0-9a-f]{64}$/.test(chunk.sha256) || chunk.bytes !== expectedBytes) {
      throw new Error(`Existing staged Wine root has an invalid chunk entry at index ${index}.`);
    }
    chunks.push(chunk);
    rootFiles.set(chunk.name, { bytes:chunk.bytes, sha256:null });
  }
  if (JSON.stringify(manifest) !== JSON.stringify({ ...WINE_ROOT, chunks })) throw new Error('Existing staged Wine root manifest does not match its canonical form.');
} else {
  rootFiles.set('.local-root.zip', rootBytes);
  for (let start = 0; start < rootBytes.length; start += WINE_ROOT.chunkBytes) {
    const bytes = rootBytes.subarray(start, start + WINE_ROOT.chunkBytes);
    const sha256 = hash(bytes);
    const name = `${chunks.length}-${sha256}.bin`;
    chunks.push({ name, bytes:bytes.length, sha256 });
    rootFiles.set(name, bytes);
  }
  rootFiles.set('chunks.json', Buffer.from(JSON.stringify({ ...WINE_ROOT, chunks }) + '\n'));
}
const rootTarget = resolve(roots, WINE_ROOT.sha256);
async function verifyExistingRoot() {
  if (!(await lstat(rootTarget)).isDirectory()) throw new Error('Unsafe existing Wine root directory.');
  const existingNames = await readdir(rootTarget);
  const preservedOriginals = existingNames.filter(name => /^\.local-root\.zip\.mybook-original-\d{8}$/.test(name));
  const unexpectedNames = existingNames.filter(name => !rootFiles.has(name) && !preservedOriginals.includes(name));
  if (unexpectedNames.length || existingNames.length !== rootFiles.size + preservedOriginals.length) throw new Error('Unexpected files in existing Wine root.');
  for (const [name, bytes] of rootFiles) {
    const path = resolve(rootTarget, name);
    // The local archive is intentionally a symlink into the private workspace.
    // Follow it only for this exact entry, then verify its content hash below.
    const metadata = name === '.local-root.zip' ? await stat(path) : await lstat(path);
    const expectedBytes = Buffer.isBuffer(bytes) ? bytes.length : bytes.bytes;
    if (!metadata.isFile() || metadata.size !== expectedBytes) throw new Error('Unsafe existing Wine root snapshot or chunk: ' + name);
    if (Buffer.isBuffer(bytes) && hash(await readFile(path)) !== hash(bytes)) throw new Error('Unsafe existing Wine root snapshot or chunk: ' + name);
    if (!Buffer.isBuffer(bytes) && bytes.sha256 && hash(await readFile(path)) !== bytes.sha256) throw new Error('Unsafe existing Wine root manifest: ' + name);
  }
  // Preserve dated archival originals in place, but do not reread them on every
  // runtime stage. They are archival backups only: the content-addressed root
  // and its chunk manifest above are the runtime inputs and remain verified.
  // Rehashing a large removable-disk backup here made each local iteration
  // block on external-volume I/O without changing the selected runtime root.
  for (const name of preservedOriginals) {
    const path = resolve(rootTarget, name);
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.size !== WINE_ROOT.bytes) {
      throw new Error('Invalid preserved Wine-root original: ' + name);
    }
  }
}
let rootExists = true;
try { await lstat(rootTarget); }
catch (error) {
  if (error.code !== 'ENOENT') throw error;
  rootExists = false;
}
if (rootExists) {
  // Reuse only after admission. The explicit --reuse-staged-root path checks
  // the previously admitted content-addressed manifest and all file sizes,
  // while the full default path also reads and hashes every stored byte.
  await verifyExistingRoot();
} else {
  if (reuseStagedRoot) throw new Error('Cannot reuse a staged Wine root that is missing from the local server.');
  const rootTemporary = await mkdtemp(resolve(roots, '.stage-'));
  try {
    for (const [name, bytes] of rootFiles) {
      await writeFile(resolve(rootTemporary, name), bytes, { mode:0o644 });
      await chmod(resolve(rootTemporary, name), 0o644);
    }
    await chmod(rootTemporary, 0o755);
    try { await rename(rootTemporary, rootTarget); }
    catch (error) {
      if (!['EEXIST','ENOTEMPTY'].includes(error.code)) throw error;
      // A concurrent stage may have published the same root after our check.
      await verifyExistingRoot();
    }
  } finally { await rm(rootTemporary, { recursive:true, force:true }); }
}
finishPhase('wine-root-chunk-verify-write');
let sourceManifestReceipt = '';
if (sourceSnapshot) {
  const manifest = {
    schema:'telos.original-runtime-source-build/v1',
    capturedAt:sourceSnapshot.capturedAt,
    stagedAt:new Date().toISOString(),
    sourceSnapshotSha256:sourceSnapshot.snapshotSha256,
    sourceCheckouts:sourceSnapshot.sourceCheckouts,
    externalSources:sourceSnapshot.externalSources,
    buildInputs:sourceSnapshot.buildInputs,
    buildSettings:sourceSnapshot.buildSettings,
    compiler:sourceSnapshot.compiler,
    buildOutputs,
    sourceBundle,
    rootArchive:{ sha256:root.sha256, bytes:root.bytes },
    release,
    finalPayloads:Object.fromEntries([...payloads].map(([name, bytes]) => [name, { sha256:assets[name], bytes:bytes.length }]).sort(([a],[b]) => a.localeCompare(b, 'en')))
  };
  const manifestBytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
  const manifestSha256 = hash(manifestBytes);
  const manifestRoot = resolve(workspace, 'artifacts/runtime-manifests');
  const manifestDirectory = resolve(manifestRoot, release);
  await mkdir(manifestRoot, { recursive:true, mode:0o700 });
  await chmod(manifestRoot, 0o700);
  await mkdir(manifestDirectory, { recursive:true, mode:0o700 });
  await chmod(manifestDirectory, 0o700);
  const manifestPath = resolve(manifestDirectory, `${manifestSha256}.json`);
  const temporaryManifest = resolve(manifestDirectory, `.manifest-${process.pid}.tmp`);
  try {
    await writeFile(temporaryManifest, manifestBytes, { flag:'wx', mode:0o600 });
    try { await link(temporaryManifest, manifestPath); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (hash(await readFile(manifestPath)) !== manifestSha256) throw new Error('Existing source/build manifest failed its content-addressed check.');
    }
  } finally { await rm(temporaryManifest, { force:true }); }
  sourceManifestReceipt = ` Source/build manifest ${manifestPath} sha256=${manifestSha256}.`;
}
// Staging only creates an immutable candidate. Local selection is a separate,
// explicit action after live acceptance; public qualification stays separate.
const descriptor = { schema:'telos.original-runtime/v1', release, entry:`/original-game/runtime-releases/${release}/game-runtime.html`, qualified:false, assets, sourceBundle, root };
const descriptorBytes = JSON.stringify(descriptor, null, 2) + '\n';
const candidateDirectory = resolve(demo, 'public/original-game/runtime-candidates');
await mkdir(candidateDirectory, { recursive:true });
const candidatePath = resolve(candidateDirectory, `${release}.json`);
const candidateTemporary = resolve(candidateDirectory, `.candidate-${process.pid}.tmp`);
let candidateTemporaryCreated = false;
try {
  await writeFile(candidateTemporary, descriptorBytes, { flag:'wx', mode:0o644 });
  candidateTemporaryCreated = true;
  try { await link(candidateTemporary, candidatePath); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (await readFile(candidatePath, 'utf8') !== descriptorBytes) throw new Error('Existing runtime candidate descriptor differs; preserving it.');
  }
} finally { if (candidateTemporaryCreated) await rm(candidateTemporary, { force:true }); }
if (selectLocal) {
  const descriptorTemporary = resolve(demo, 'public/original-game/.runtime-local-' + process.pid + '.json');
  try {
    await writeFile(descriptorTemporary, descriptorBytes, { mode:0o644 });
    await rename(descriptorTemporary, resolve(demo, 'public/original-game/runtime-local.json'));
  } finally { await rm(descriptorTemporary, { force:true }); }
}
finishPhase('local-descriptor');
console.log(`Staged local original-game release ${release}.${sourceManifestReceipt} ${selectLocal ? 'Selected locally; qualification and publication remain pending.' : 'Local selection unchanged; pass --select-local after acceptance to select it.'} No installation assets were copied or uploaded. Stage phases: ${phaseTimes.join(' ')} total=${phaseTimes.reduce((sum, entry) => sum + Number(entry.split('=')[1].replace('ms', '')), 0).toFixed(1)}ms.`);
