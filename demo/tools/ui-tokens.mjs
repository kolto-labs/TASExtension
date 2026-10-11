// Project the packaged, declarative palette into CSS. No application state.
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const source = new URL('../../KPTASInterface/kptas/telos/ui_tokens.json', import.meta.url);
const publicCss = new URL('../public/ui-tokens.css', import.meta.url);

export async function renderUiTokens() {
  const data = JSON.parse(await readFile(source, 'utf8'));
  if (data.schema !== 'stasis-ui-colors/v1') throw new Error('Unsupported Stasis UI color schema');
  const declarations = [];
  const variable = name => {
    if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new Error(`Invalid UI color role: ${name}`);
    return `--${name.replaceAll('_', '-')}`;
  };
  for (const [name, color] of Object.entries(data.colors)) {
    if (!/^#[0-9a-f]{6}$/i.test(color)) throw new Error(`Invalid UI color: ${name}`);
    declarations.push(`  ${variable(name)}: ${color};`);
  }
  for (const [name, tint] of Object.entries(data.tints)) {
    const color = data.colors[tint.color];
    if (!color || !Number.isFinite(tint.alpha) || tint.alpha < 0 || tint.alpha > 1) {
      throw new Error(`Invalid UI tint: ${name}`);
    }
    const rgb = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16));
    declarations.push(`  ${variable(name)}: rgba(${rgb.join(', ')}, ${tint.alpha});`);
  }
  for (const [name, number] of Object.entries(data.metrics || {})) {
    if (!Number.isInteger(number) || number < 1 || number > 128) throw new Error(`Invalid UI metric: ${name}`);
    declarations.push(`  ${variable(name)}: ${number}px;`);
  }
  for (const [name, families] of Object.entries(data.fonts || {})) {
    if (!Array.isArray(families) || !families.length || families.some(family => typeof family !== 'string' || !/^[A-Za-z0-9 -]+$/.test(family))) throw new Error(`Invalid UI font: ${name}`);
    declarations.push(`  ${variable('font_' + name)}: ${families.map(family => `"${family}"`).join(', ')};`);
  }
  return '/* Derived from KPTASInterface/kptas/telos/ui_tokens.json; edit that source. */\n'
    + `:root {\n${declarations.join('\n')}\n}\n`;
}

// Explicit source-asset synchronization for direct public/ development.
// Importing from the publisher renders only its own output asset.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await writeFile(publicCss, await renderUiTokens(), 'utf8');
}
