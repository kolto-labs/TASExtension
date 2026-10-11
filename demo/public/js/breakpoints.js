import { compileWatch } from './memory.js';

const ENABLED_KEY = 'telos.memory.pause-on-change';
const EXPRESSION_KEY = 'telos.memory.pause-on-change-expression';

export function loadBreakpoints(storage = localStorage) {
  let expression = '';
  let enabled = false;
  try {
    const stored = storage.getItem(EXPRESSION_KEY) || '';
    expression = stored.length <= 80 ? stored.trim() : '';
    enabled = storage.getItem(ENABLED_KEY) === 'true';
  } catch { /* State breakpoints remain editable for this visit. */ }
  const watch = expression ? compileBreakpoint(expression) : null;
  if (!expression || watch?.w.error) enabled = false;
  return { watchBreakExpr: expression, watchBreak: watch, pauseWatchChange: enabled };
}

export function compileBreakpoint(expression) {
  const watch = compileWatch(expression);
  if (!watch.error && watch.steps[0] === 'input') return { expr: expression, w: { error: 'Breakpoints need a state expression such as pc.hp or rng.' } };
  return { expr: expression, w: watch };
}

export function saveBreakpointEnabled(enabled, storage = localStorage) {
  try { storage.setItem(ENABLED_KEY, String(enabled)); } catch { /* Storage may be disabled. */ }
}

export function saveBreakpointExpression(expression, storage = localStorage) {
  try { storage.setItem(EXPRESSION_KEY, expression); } catch { /* Storage may be disabled. */ }
}
