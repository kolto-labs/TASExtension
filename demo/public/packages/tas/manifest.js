// Declarative identity for the optional TAS package. The Core host owns
// installation state and lifecycle; this file is metadata, never executable
// authority for third-party code.
export const TAS_MANIFEST = Object.freeze({
  schema: 'telos.extension/v1',
  id: 'telos.tas',
  name: 'TAS pack',
  version: '1.0.0',
  api: Object.freeze({ id: 'telos.core.extension', major: 1, minor: 0 }),
  defaultState: Object.freeze({ installed: false, enabled: false }),
  requires: Object.freeze(['telos.debug']),
  permissions: Object.freeze(['tas.runtime']),
  provides: Object.freeze(['tas.authoring', 'tas.playback', 'tas.input-recording', 'tas.branch-workflow']),
  description: 'Author inputs, record scores, edit the piano roll, and manage run branches.',
});
