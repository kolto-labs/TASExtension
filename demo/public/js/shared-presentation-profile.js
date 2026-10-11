export const SHARED_PRESENTATION_FORMAT = 'telos-shared-presentation/v3';
export const PREVIOUS_SHARED_PRESENTATION_FORMAT = 'telos-shared-presentation/v2';
export const LEGACY_SHARED_PRESENTATION_FORMAT = 'telos-shared-presentation/v1';
export const SHARED_PROFILE_STORAGE = 'telos.shared-presentation-profile.v1';
export function validateSharedProfile(value) {
  const object = item => item && typeof item === 'object' && !Array.isArray(item);
  const exact = (item, keys, label) => {
    if (!object(item) || Object.keys(item).some(key => !keys.includes(key))) throw new Error(`Unknown or invalid ${label} fields.`);
  };
  exact(value, ['format', 'presentation', 'panels', 'hostProfiles'], 'profile');
  if (![SHARED_PRESENTATION_FORMAT, PREVIOUS_SHARED_PRESENTATION_FORMAT, LEGACY_SHARED_PRESENTATION_FORMAT].includes(value.format) || !['overlay', 'surround'].includes(value.presentation)) throw new Error('Unsupported shared presentation profile.');
  const legacy = value.format === LEGACY_SHARED_PRESENTATION_FORMAT;
  const names = value.format === SHARED_PRESENTATION_FORMAT ? ['game', 'timeline'] : ['game'];
  exact(value.panels, names, 'panels');
  for (const name of names) {
    const panel = value.panels[name];
    if (name === 'timeline') {
      exact(panel, ['opacity'], 'timeline panel');
      if (!Number.isFinite(panel.opacity) || panel.opacity < 0.55 || panel.opacity > 1) throw new Error('Timeline opacity must be 0.55–1.');
      continue;
    }
    exact(panel, legacy ? ['visible', 'opacity'] : ['visible', 'opacity', 'collapsed'], `${name} panel`);
    if (typeof panel.visible !== 'boolean' || !Number.isFinite(panel.opacity)
      || panel.opacity < 0.55 || panel.opacity > 1) throw new Error(`${name} visibility must be boolean and opacity must be 0.55–1.`);
    if (!legacy && typeof panel.collapsed !== 'boolean') throw new Error(`${name} collapsed state must be boolean.`);
  }
  if (!object(value.hostProfiles) || Object.keys(value.hostProfiles).some(key => !/^[a-z][a-z0-9-]{0,47}$/.test(key))) throw new Error('Invalid host profile identifiers.');
  if (new TextEncoder().encode(JSON.stringify(value)).length > 65536) throw new Error('Shared profiles must be 64 KiB or smaller.');
  return JSON.parse(JSON.stringify(value));
}
export function retainedHostProfiles() {
  try { return validateSharedProfile(JSON.parse(localStorage.getItem(SHARED_PROFILE_STORAGE))).hostProfiles; }
  catch { return {}; }
}
