import { cp, mkdir, rm, writeFile } from 'node:fs/promises';
import { dirname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { renderUiTokens } from './ui-tokens.mjs';

const demoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const publicRoot = resolve(demoRoot, 'public');
const outputRoot = resolve(demoRoot, 'dist');
const appEntry = resolve(publicRoot, 'js/app.js');
const publishedApp = resolve(outputRoot, 'js/app.js');
const uiShellEntry = resolve(publicRoot, 'js/ui-shell.js');
const publishedUiShell = resolve(outputRoot, 'js/ui-shell.js');
// Read the shared source before mutating build output. Publish a fresh CSS
// projection even if the checked-in direct-development asset is out of date.
const uiTokenCss = await renderUiTokens();

await rm(outputRoot, { recursive: true, force: true });
await mkdir(dirname(publishedApp), { recursive: true });

const result = await build({
  absWorkingDir: demoRoot,
  entryPoints: { app: appEntry },
  outdir: outputRoot,
  entryNames: 'js/[name]',
  chunkNames: 'packages/[name]-[hash]',
  bundle: true,
  splitting: true,
  minify: true,
  legalComments: 'none',
  sourcemap: false,
  metafile: true,
  format: 'esm',
  target: 'es2022',
});

const bundledSources = new Set(
  Object.keys(result.metafile.inputs).map((input) => resolve(demoRoot, input)),
);
// Keep presentation separate from app/editor/runtime ownership. Compile out
// the developer loader as well as excluding its source from published output.
await build({
  absWorkingDir: demoRoot,
  entryPoints: [uiShellEntry],
  define: { __TELOS_PRODUCTION__: 'true' },
  outfile: publishedUiShell,
  bundle: true,
  minify: true,
  legalComments: 'none',
  sourcemap: false,
  format: 'esm',
  target: 'es2022',
});
const originalGameSource = resolve(publicRoot, 'js/original-game.js');
if (!bundledSources.has(originalGameSource)) {
  throw new Error('The page bundle did not absorb public/js/original-game.js.');
}

await cp(publicRoot, outputRoot, {
  recursive: true,
  filter(source) {
    const resolvedSource = resolve(source);
    const relativeSource = relative(publicRoot, resolvedSource);
    const isDemoModule = resolvedSource.endsWith('.js')
      && (relativeSource.startsWith(`js${sep}`) || relativeSource.startsWith(`packages${sep}`));
    const isSourceMap = resolvedSource.endsWith('.map');
    return !isDemoModule && !isSourceMap && !bundledSources.has(resolvedSource);
  },
});

await writeFile(resolve(outputRoot, 'ui-tokens.css'), uiTokenCss, 'utf8');

console.log(`Built ${relative(demoRoot, publishedApp).split(sep).join('/')}`);
