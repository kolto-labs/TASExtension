import { ListenerScope } from '../packages/tas-ui/listener-scope.js';
import { exportKeymap, importKeymap } from './keymap-interchange.js';

const SETTINGS_KEY = 'telos.keyboard-controls';

// Native action IDs/defaults, with browser-local overrides. Existing editor
// gestures remain reserved; all actions use the controls' admission state.
const NATIVE_BINDINGS = [
  { id: 'toggle_editor', control: 'b-toggle-timeline', key: 'Control+F6', label: 'Expand or collapse editor' },
  { id: 'play_pause', control: 'b-play', key: 'Control+F7', label: 'Play or pause' },
  { id: 'step', control: 'b-step', key: 'Control+F8', label: 'Step forward' },
  { id: 'rewind', control: 'b-back', key: 'Control+F9', label: 'Step back' },
  { id: 'switch_layout', control: 'b-tas-presentation', key: 'Control+Alt+KeyL', label: 'Switch overlay layout' },
  { id: 'toggle_engagement', action: 'engage', control: 'b-toggle-engage', key: 'Control+Shift+F11', label: 'Turn tool shortcuts on or off' },
  { id: 'toggle_visibility', action: 'visibility', control: 'b-show-workspace', key: 'Control+Shift+F12', label: 'Show or hide workspace' },
];
const defaultBindings = () => Object.fromEntries(NATIVE_BINDINGS.map(item => [item.id, [item.key]]));
const validCode = code => typeof code === 'string' && /^(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown|Insert|Delete|Backspace|Space|Enter|Numpad(?:[0-9]|Add|Subtract|Multiply|Divide|Decimal|Enter)|Backquote|Minus|Equal|BracketLeft|BracketRight|Backslash|Semicolon|Quote|Comma|Period|Slash)$/.test(code);
function validChord(value) {
  if (value === '') return true;
  if (typeof value !== 'string' || value.length > 80) return false;
  const parts = value.split('+'), code = parts.pop();
  return validCode(code) && parts.join('+') === ['Control', 'Alt', 'Shift', 'Meta'].filter(key => parts.includes(key)).join('+');
}
// Legacy strings migrate to arrays in memory; host quick toggles stay separate.
function bindingKeys(value) {
  return [...new Set((Array.isArray(value) ? value : typeof value === 'string' ? [value] : []).filter(Boolean))];
}
function eventChord(event) {
  return [...(event.ctrlKey ? ['Control'] : []), ...(event.altKey ? ['Alt'] : []),
    ...(event.shiftKey ? ['Shift'] : []), ...(event.metaKey ? ['Meta'] : []), event.code].join('+');
}
function chordName(value) { return value ? value.split('+').map(key => key === 'Control' ? 'Ctrl' : keyName(key)).join('+') : 'Not assigned'; }
// Existing editor/navigation commands retain their meaning; reject collisions
// rather than letting a new capture silently steal an established command.
function reservedChord(value) {
  const parts = value.split('+'), code = parts.pop();
  if (!parts.length) return !/^F(?:[1-9]|1[0-9]|2[0-4])$/.test(code);
  if (parts.length === 1 && parts[0] === 'Shift' && !/^F(?:[1-9]|1[0-9]|2[0-4])$/.test(code)) return true;
  return ((parts.includes('Control') || parts.includes('Meta')) && /^(?:Key[ABCFGOPRSTVWXYZ]|Backquote)$/.test(code))
    || value === 'Shift+F10' || value === 'Alt+F4';
}

export function loadKeyboardSettings(storage = localStorage) {
  const defaults = { engageCode: 'F8', visibilityCode: 'F9', pauseOnEngage: true, engaged: true, workspaceHidden: false, actionBindings: defaultBindings() };
  try {
    const stored = JSON.parse(storage.getItem(SETTINGS_KEY) || 'null');
    if (!stored || typeof stored !== 'object') return defaults;
    const engageCode = validCode(stored.engageCode) ? stored.engageCode : defaults.engageCode;
    const fallbackVisibility = engageCode === defaults.visibilityCode ? 'F10' : defaults.visibilityCode;
    const visibilityCode = validCode(stored.visibilityCode) && stored.visibilityCode !== engageCode ? stored.visibilityCode : fallbackVisibility;
    const actionBindings = defaultBindings();
    const used = new Set([engageCode, visibilityCode]);
    for (const item of NATIVE_BINDINGS) {
      const value = stored.actionBindings?.[item.id] ?? item.key;
      // Corrupt or conflicting stored entries cannot become active shortcuts.
      actionBindings[item.id] = bindingKeys(value).filter(chord => {
        if (!validChord(chord) || reservedChord(chord) || used.has(chord)) return false;
        used.add(chord); return true;
      });
    }
    return {
      actionBindings,
      engageCode,
      visibilityCode,
      pauseOnEngage: stored.pauseOnEngage !== false,
      engaged: stored.engaged !== false,
      workspaceHidden: stored.workspaceHidden === true,
    };
  } catch { return defaults; }
}

function keyName(code) {
  if (code.startsWith('Key')) return code.slice(3);
  if (code.startsWith('Digit')) return code.slice(5);
  if (code.startsWith('Numpad')) return `Num ${code.slice(6)}`;
  return code;
}

function isUnmodified(event) { return !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey; }

export class KeyboardControls {
  constructor(app, { pause, storage = localStorage, doc = document } = {}) {
    this.events = new ListenerScope();
    this.app = app;
    this.pause = pause;
    this.storage = storage;
    this.doc = doc;
  }

  save() {
    try {
      const { engageCode, visibilityCode, pauseOnEngage, engaged, workspaceHidden, actionBindings } = this.app;
      this.storage.setItem(SETTINGS_KEY, JSON.stringify({ engageCode, visibilityCode, pauseOnEngage, engaged, workspaceHidden, actionBindings }));
    } catch { /* Preferences still apply for this visit when storage is unavailable. */ }
  }

  render() {
    const app = this.app;
    const engageLabel = keyName(app.engageCode);
    const visibilityLabel = keyName(app.visibilityCode);
    const button = id => this.doc.getElementById(id);
    button('b-toggle-engage').textContent = app.engaged ? 'On' : 'Off';
    button('b-toggle-engage').setAttribute('aria-pressed', String(app.engaged));
    button('b-toggle-engage').setAttribute('aria-label', `Turn tool shortcuts ${app.engaged ? 'off' : 'on'}`);
    button('key-label-engage').textContent = engageLabel;
    button('key-label-visibility').textContent = visibilityLabel;
    button('show-key-label').textContent = visibilityLabel;
    button('pause-on-engage').checked = app.pauseOnEngage;
    this.doc.body.classList.toggle('tas-ui-hidden', app.workspaceHidden);
    button('b-show-workspace').hidden = !app.workspaceHidden;
    for (const binding of NATIVE_BINDINGS) {
      const chords = bindingKeys(app.actionBindings[binding.id]);
      const hint = button(`key-label-${binding.id}`);
      if (hint) hint.textContent = chords.map(chordName).join(' / ') || 'Not assigned';
      const control = button(binding.control);
      const alias = binding.action === 'engage' ? app.engageCode : binding.action === 'visibility' ? app.visibilityCode : '';
      const keys = [...chords, alias].filter(Boolean).map(key => key.replace(/Key([A-Z])/g, '$1').replace(/Digit([0-9])/g, '$1'));
      if (control) control.setAttribute('aria-keyshortcuts', keys.join(' '));
    }
  }

  setWorkspaceHidden(hidden) {
    this.app.workspaceHidden = hidden;
    this.save();
    this.render();
  }

  setEngaged(enabled) {
    const app = this.app;
    if (app.engaged === enabled) return;
    app.engaged = enabled;
    if (!enabled || (enabled && app.pauseOnEngage)) this.pause();
    this.save();
    this.render();
  }

  setCapture(action, control) {
    this.app.keyCapture = action;
    for (const button of this.doc.querySelectorAll('#key-settings .key-capture')) {
      const selected = button === control;
      button.setAttribute('aria-pressed', String(selected));
      button.textContent = selected ? 'Press a key…' : button.dataset.idleLabel || 'Change';
    }
    this.doc.getElementById('key-settings-status').textContent = 'Press a shortcut. Escape cancels; Tab leaves the binding unchanged.';
    control.focus();
  }

  cancelCapture() {
    this.app.keyCapture = '';
    for (const control of this.doc.querySelectorAll('#key-settings .key-capture')) {
      control.setAttribute('aria-pressed', 'false');
      control.textContent = control.dataset.idleLabel || 'Change';
    }
    this.doc.getElementById('key-settings-status').textContent = '';
  }

  handle(event, targetIsTyping, dialogOpen) {
    const app = this.app;
    const status = this.doc.getElementById('key-settings-status');
    if (dialogOpen || event.isComposing || event.keyCode === 229) return false;
    if (app.keyCapture) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.cancelCapture(); return true; }
      if (event.key === 'Tab') { this.cancelCapture(); return false; }
      event.preventDefault(); event.stopPropagation();
      if (event.repeat || ['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) return true;
      const adding = app.keyCapture.startsWith('alias:');
      const action = adding ? app.keyCapture.slice(6) : app.keyCapture.startsWith('action:') ? app.keyCapture.slice(7) : null;
      const chord = eventChord(event);
      if (!validChord(chord) || reservedChord(chord)) {
        status.textContent = 'Choose a function key or a modifier shortcut that is not used by the editor or browser.';
        return true;
      }
      if (!action && !isUnmodified(event)) { status.textContent = 'Choose a single function key for this quick toggle.'; return true; }
      const conflict = NATIVE_BINDINGS.find(item => item.id !== action && bindingKeys(app.actionBindings[item.id]).includes(chord));
      if (conflict || (app.keyCapture !== 'engage' && chord === app.engageCode)
        || (app.keyCapture !== 'visibility' && chord === app.visibilityCode)) {
        status.textContent = conflict ? `Already assigned to ${conflict.label.toLowerCase()}. Clear it first or choose another shortcut.` : 'Already assigned to a quick toggle. Choose another shortcut.';
        return true;
      }
      if (action) app.actionBindings[action] = adding ? [...new Set([...bindingKeys(app.actionBindings[action]), chord])] : [chord];
      else app[app.keyCapture === 'engage' ? 'engageCode' : 'visibilityCode'] = event.code;
      this.cancelCapture(); this.save(); this.render();
      status.textContent = `Shortcut saved: ${chordName(chord)}.`;
      return true;
    }
    if (targetIsTyping || event.defaultPrevented) return false;
    const binding = NATIVE_BINDINGS.find(item => bindingKeys(app.actionBindings[item.id]).includes(eventChord(event)));
    if (binding) {
      event.preventDefault(); event.stopPropagation();
      if (!event.repeat) {
        if (binding.action === 'visibility') this.setWorkspaceHidden(!app.workspaceHidden);
        else if (binding.action === 'engage') this.setEngaged(!app.engaged);
        else if (app.engaged) {
          const control = this.doc.getElementById(binding.control);
          if (control && !control.disabled && control.getAttribute('aria-disabled') !== 'true') control.click();
        }
      }
      return true;
    }
    if (!isUnmodified(event)) return false;
    if (event.code === app.visibilityCode) {
      event.preventDefault(); event.stopPropagation();
      if (!event.repeat) this.setWorkspaceHidden(!app.workspaceHidden);
      return true;
    }
    if (event.code === app.engageCode) {
      event.preventDefault(); event.stopPropagation();
      if (!event.repeat) this.setEngaged(!app.engaged);
      return true;
    }
    return false;
  }

  suspend() { this.cancelCapture(); this.events.suspend(); }
  resume() { this.events.resume(); this.render(); }

  bind() {
    const button = id => this.doc.getElementById(id);
    if (!button('transport-shortcut-help')) {
      const help = this.doc.createElement('details');
      help.id = 'transport-shortcut-help';
      const summary = this.doc.createElement('summary');
      summary.textContent = 'Transport and layout shortcuts';
      help.append(summary);
      const scope = this.doc.createElement('p');
      scope.textContent = 'Aliases work in the interface window. In native Stasis, only the primary shortcut can also work while the game has focus.';
      help.append(scope);
      for (const binding of NATIVE_BINDINGS) {
        const row = this.doc.createElement('div'); row.className = 'key-setting-row';
        const label = this.doc.createElement('span'); label.textContent = binding.label;
        const key = this.doc.createElement('kbd'); key.id = `key-label-${binding.id}`;
        key.className = 'key-setting-hint';
        const change = this.doc.createElement('button'); change.type = 'button';
        change.className = 'btn small key-capture'; change.textContent = 'Change';
        change.setAttribute('aria-label', `Change shortcut: ${binding.label}`);
        change.setAttribute('aria-describedby', key.id);
        this.events.listen(change, 'click', () => this.setCapture(`action:${binding.id}`, change));
        const clear = this.doc.createElement('button'); clear.type = 'button';
        clear.className = 'btn small'; clear.textContent = 'Clear';
        clear.setAttribute('aria-label', `Clear shortcut: ${binding.label}`);
        this.events.listen(clear, 'click', () => {
          this.cancelCapture(); this.app.actionBindings[binding.id] = []; this.save(); this.render();
          button('key-settings-status').textContent = `Shortcut cleared: ${binding.label}.`;
        });
        const add = this.doc.createElement('button'); add.type = 'button';
        add.className = 'btn small key-capture'; add.textContent = 'Add alias';
        add.dataset.idleLabel = 'Add alias';
        add.setAttribute('aria-label', `Add shortcut alias: ${binding.label}`);
        this.events.listen(add, 'click', () => this.setCapture(`alias:${binding.id}`, add));
        row.append(label, key, change, add, clear); help.append(row);
      }
      const reset = this.doc.createElement('button'); reset.type = 'button';
      reset.className = 'btn small'; reset.textContent = 'Reset all shortcuts';
      this.events.listen(reset, 'click', () => {
        this.cancelCapture();
        Object.assign(this.app, { engageCode: 'F8', visibilityCode: 'F9', actionBindings: defaultBindings() });
        this.save(); this.render(); button('key-settings-status').textContent = 'Default shortcuts restored.';
      });
      help.append(reset);
      const exportKeys = this.doc.createElement('button'); exportKeys.type = 'button';
      exportKeys.className = 'btn small'; exportKeys.textContent = 'Export shortcuts';
      this.events.listen(exportKeys, 'click', () => {
        this.cancelCapture();
        let url;
        try {
          const text = exportKeymap(this.app.actionBindings, NATIVE_BINDINGS.map(item => item.id));
          url = URL.createObjectURL(new Blob([text], { type: 'application/toml' }));
          const link = this.doc.createElement('a'); link.href = url; link.download = 'telos-keymap.toml';
          link.hidden = true; help.append(link); link.click(); link.remove();
          const savedUrl = url; setTimeout(() => URL.revokeObjectURL(savedUrl), 1000);
          button('key-settings-status').textContent = 'Shared action shortcuts exported.';
        } catch (error) {
          if (url) URL.revokeObjectURL(url);
          button('key-settings-status').textContent = error.message;
        }
      });
      const importKeys = this.doc.createElement('button'); importKeys.type = 'button';
      importKeys.className = 'btn small'; importKeys.textContent = 'Import shortcuts';
      const file = this.doc.createElement('input'); file.type = 'file'; file.accept = '.toml'; file.hidden = true;
      this.events.listen(importKeys, 'click', () => { this.cancelCapture(); file.click(); });
      this.events.listen(file, 'change', async () => {
        const selected = file.files?.[0]; file.value = '';
        if (!selected) return;
        importKeys.disabled = true;
        const lifecycle = this.events.controller.signal;
        try {
          if (selected.size > 65536) throw new Error('Shortcut files must be 64 KiB or smaller.');
          const text = await selected.text();
          if (lifecycle.aborted) return;
          const imported = importKeymap(text, NATIVE_BINDINGS.map(item => item.id));
          const candidate = { ...this.app.actionBindings, ...imported.bindings };
          const used = new Set([this.app.engageCode, this.app.visibilityCode]);
          for (const item of NATIVE_BINDINGS) {
            candidate[item.id] = bindingKeys(candidate[item.id]);
            for (const chord of candidate[item.id]) {
              if (!validChord(chord) || reservedChord(chord) || used.has(chord)) throw new Error(`Conflicting or reserved shortcut for ${item.label.toLowerCase()}. No shortcuts changed.`);
              used.add(chord);
            }
          }
          this.cancelCapture(); this.app.actionBindings = candidate; this.save(); this.render();
          button('key-settings-status').textContent = `${Object.keys(imported.bindings).length} shared shortcuts imported.${imported.skipped ? ` ${imported.skipped} other actions were left unchanged.` : ''}`;
        } catch (error) { button('key-settings-status').textContent = error.message; }
        finally { importKeys.disabled = false; }
      });
      help.append(exportKeys, importKeys, file);
      this.events.listen(help, 'toggle', () => { if (!help.open && /^(action|alias):/.test(this.app.keyCapture || '')) this.cancelCapture(); });
      button('key-settings').querySelector('.key-settings-body').append(help);
    }
    this.events.listen(button('key-settings'), 'toggle', () => {
      if (!button('key-settings').open && this.app.keyCapture) this.cancelCapture();
    });
    this.events.listen(this.doc, 'focusin', event => {
      if (this.app.keyCapture && !button('key-settings').contains(event.target)) this.cancelCapture();
    });
    this.render();
    this.events.listen(button('b-toggle-engage'), 'click', () => this.setEngaged(!this.app.engaged));
    this.events.listen(button('b-show-workspace'), 'click', () => this.setWorkspaceHidden(false));
    this.events.listen(button('b-bind-engage'), 'click', event => this.setCapture('engage', event.currentTarget));
    this.events.listen(button('b-bind-visibility'), 'click', event => this.setCapture('visibility', event.currentTarget));
    this.events.listen(button('pause-on-engage'), 'change', event => {
      this.app.pauseOnEngage = event.currentTarget.checked;
      this.save();
    });
  }
}
