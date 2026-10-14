export const DEBUG_MANIFEST = Object.freeze({
  schema: 'telos.extension/v1', id: 'telos.debug', name: 'Debug', version: '1.0.0',
  api: Object.freeze({ id: 'telos.core.extension', major: 1, minor: 0 }),
  defaultState: Object.freeze({ installed: false, enabled: false }),
  requires: Object.freeze([]), permissions: Object.freeze(['debug.runtime']),
  provides: Object.freeze(['debug.inspection', 'debug.expressions', 'debug.analysis']),
  description: 'Inspect memory, execution, symbols, and binary analysis through the selected debugger adapter.',
});

// Widget providers consume these operations. Metadata grants no execution access.
export const DEBUG_WIDGET_OPERATIONS = Object.freeze({
  disassembly: ['code.disassemble'], decompiler: ['code.decompile'],
  registers: ['registers.read'], memory: ['memory.read'], memoryMap: ['memory.map'],
  breakpoints: ['breakpoints.list', 'breakpoints.add', 'breakpoints.remove'],
  stack: ['stack.read'], threads: ['threads.list'], modules: ['modules.list'], addressMapping: ['memory.mapping'],
  watches: ['expressions.evaluate'], symbols: ['symbols.list'],
  references: ['code.references'], functions: ['code.functions'],
  strings: ['binary.strings'], patches: ['memory.write'],
  trace: ['trace.read'], events: ['events.read'], inputTimeline: ['input.timeline'],
  fileHeaders: ['binary.identity'], fileSections: ['binary.sections'], fileDependencies: ['binary.dependencies'],
  fileBytes: ['binary.bytes'],
  search: ['memory.search'], processes: ['processes.list'],
});
