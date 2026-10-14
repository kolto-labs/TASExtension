// Native input_edits.set_lanes/autofire semantics for score authoring only.
// This module never samples, dispatches or advances the original game.
import { KEY_ORDER, BUTTONS, setKey } from './score.js';
import { editTicks } from './edit.js';
import { MAX_TICKS } from './holo.js';

export const MAX_LANE_PATTERN_TICKS = 20000;
const LANES = new Set([...KEY_ORDER, ...BUTTONS]);

export function applyLanePattern(text, prog, model, {
  low, high, lanes, mode = 'autofire', period = 2, phase = 0,
} = {}) {
  if (!Number.isSafeInteger(low) || !Number.isSafeInteger(high) || low < 0 || high < low) {
    throw new Error('Choose a range of whole score ticks.');
  }
  if (!Array.isArray(model?.rows) || !Array.isArray(lanes) || !lanes.length
    || lanes.length > LANES.size || new Set(lanes).size !== lanes.length
    || lanes.some(lane => typeof lane !== 'string' || !LANES.has(lane))) {
    throw new Error('Choose supported key or button lanes.');
  }
  if (!['hold', 'autofire'].includes(mode)) throw new Error('Choose Hold or Autofire.');
  if (mode === 'autofire' && (!Number.isSafeInteger(period) || period < 2
    || period > MAX_LANE_PATTERN_TICKS || !Number.isSafeInteger(phase)
    || phase < 0 || phase >= period)) {
    throw new Error('Use a period from 2 to 20000 score ticks and a phase from 0 to period minus 1.');
  }
  // Tick zero is the boot row. Bound edits to existing displayed score rows
  // and the host's authoring limit; tail rows retain normal editTicks behavior.
  const first = Math.max(1, low);
  const last = Math.min(high, MAX_TICKS, model.rows.length - 1);
  if (last < first) return { text, same:true, note:'The range contains no editable score ticks.' };
  if (last - first + 1 > MAX_LANE_PATTERN_TICKS) {
    throw new Error('Choose at most 20000 score ticks for one lane edit.');
  }
  const on = Math.max(1, Math.floor(period / 2));
  const changes = new Map();
  for (let tick = first; tick <= last; tick++) {
    const pressed = mode === 'hold' || (tick - first + phase) % period < on;
    changes.set(tick, input => lanes.reduce((value, lane) => setKey(value, lane, pressed), input));
  }
  // Existing authoring owner retains repeat-unroll admission, sync-row refusal,
  // literal score rewrite, aliases, mouse coordinates and unrelated lane bits.
  return editTicks(text, prog, model, changes);
}
