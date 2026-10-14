// Inspect executable headers and bounded section, segment and dependency tables. File metadata does not
// establish that a runtime can load or execute the binary.
const SCHEMA = 'telos.binary-identity/v1';
const MAX_HEADER_OFFSET = 1024 * 1024;
const MAX_FAT_ARCHES = 16;
const MAX_READ = 64 * 1024;
const MAX_METADATA_BYTES = 1024 * 1024;
const MAX_TABLE_ENTRIES = 256;
const MAX_NAME_BYTES = 512;

const machines = new Map([
  [0x014c, 'x86'], [0x8664, 'x86_64'], [0x01c0, 'arm'], [0x01c4, 'armv7'],
  [0xaa64, 'arm64'], [0x0200, 'ia64'], [0x5032, 'riscv32'], [0x5064, 'riscv64'],
  [0x5128, 'riscv128'],
]);
const elfMachines = new Map([
  [3, 'x86'], [8, 'mips'], [20, 'powerpc'], [21, 'powerpc64'], [22, 's390'],
  [40, 'arm'], [42, 'superh'], [62, 'x86_64'], [183, 'arm64'], [243, 'riscv'],
  [258, 'loongarch'],
]);
const machMachines = new Map([
  [7, 'x86'], [0x01000007, 'x86_64'], [12, 'arm'], [0x0100000c, 'arm64'],
  [18, 'powerpc'], [0x01000012, 'powerpc64'],
]);
const pe64Machines = new Set([0x8664, 0xaa64, 0x0200, 0x5064, 0x5128]);

function result(status, format = null, architecture = null, abi = null, reason = 'unrecognized', extra = {}) {
  return { schema:SCHEMA, status, format, architecture, abi, byteOrder:null, reason, architectures:[], ...extra };
}

function makeReader(input) {
  if (typeof Blob !== 'undefined' && input instanceof Blob) {
    const size = input.size;
    return {
      size,
      async read(offset, length) {
        if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || length > MAX_READ || offset > size || length > size - offset) return null;
        return new Uint8Array(await input.slice(offset, offset + length).arrayBuffer());
      },
    };
  }
  let bytes;
  if (input instanceof ArrayBuffer) bytes = new Uint8Array(input);
  else if (ArrayBuffer.isView(input)) bytes = new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  else return null;
  return {
    size:bytes.byteLength,
    async read(offset, length) {
      if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || length > MAX_READ || offset > bytes.byteLength || length > bytes.byteLength - offset) return null;
      return bytes.subarray(offset, offset + length);
    },
  };
}

function u16(bytes, offset, littleEndian) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint16(offset, littleEndian);
}
function u32(bytes, offset, littleEndian) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(offset, littleEndian);
}
function u64(bytes, offset, littleEndian) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getBigUint64(offset, littleEndian);
}
function has(bytes, signature) { return signature.every((byte, index) => bytes[index] === byte); }
function hexMagic(bytes) { return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''); }

// Bound both individual reads and total work, including tables in very large files.
function metadataReader(reader) {
  let remaining = MAX_METADATA_BYTES;
  return async (offset, length) => {
    if (length > remaining) throw new Error('Inspection byte limit reached');
    const bytes = await reader.read(offset, length);
    if (!bytes || bytes.length !== length) throw new Error('Metadata range is outside the file or inspection limits');
    remaining -= length;
    return bytes;
  };
}
function safeOffset(value) {
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('File offset exceeds safe integer range');
  return Number(value);
}
function address(value) { return `0x${value.toString(16)}`; }
async function dependencyName(read, offset, available) {
  const bytes = await read(offset, Math.min(available, MAX_NAME_BYTES));
  const end = bytes.indexOf(0);
  if (end <= 0) throw new Error('Dependency name is empty, unterminated or exceeds 511 bytes');
  // Escape control and non-ASCII bytes instead of interpreting executable-supplied text.
  return Array.from(bytes.subarray(0, end), byte => byte >= 32 && byte < 127
    ? String.fromCharCode(byte) : `\\x${byte.toString(16).padStart(2, '0')}`).join('');
}
function mappedRange(ranges, value, length) {
  const matches = ranges.filter(range => value >= range.address && value - range.address < range.size &&
    BigInt(length) <= range.size - (value - range.address));
  if (matches.length !== 1) throw new Error('Address has no unique file-backed mapping');
  const range = matches[0], delta = value - range.address;
  return { offset:safeOffset(range.offset + delta), available:safeOffset(range.size - delta) };
}
// File layout is independent of import parsing and never describes live mappings.
async function fileLayout(reader, header, is64, peOffset = null) {
  const layout = { status:'unavailable', entries:[], reason:null,
    scope:peOffset !== null
      ? 'PE section headers; addresses are relative virtual addresses, not captured runtime mappings.'
      : 'ELF load segments and section headers; declared virtual addresses are not captured runtime mappings.' };
  const read = metadataReader(reader);
  const little = peOffset !== null || header[5] === 1;
  const word = (bytes, at) => is64 ? u64(bytes, at, little) : BigInt(u32(bytes, at, little));
  const nameOf = bytes => Array.from(bytes.subarray(0, bytes.indexOf(0) < 0 ? bytes.length : bytes.indexOf(0)),
    byte => byte >= 32 && byte < 127 ? String.fromCharCode(byte) : `\\x${byte.toString(16).padStart(2, '0')}`).join('') || '(unnamed)';
  const add = (name, kind, virtualAddress, offset, fileSize, memorySize, permissions) => {
    if (fileSize > 0n && (offset > BigInt(reader.size) || fileSize > BigInt(reader.size) - offset)) throw new Error('Declared file range is truncated or outside the file');
    layout.entries.push({ name, kind, address:address(virtualAddress), offset:address(offset), fileSize:fileSize.toString(), memorySize:memorySize.toString(), permissions });
  };
  try {
    if (peOffset !== null) {
      const count = u16(header, 6, true), optionalSize = u16(header, 20, true);
      if (!count || count > MAX_TABLE_ENTRIES) throw new Error('PE section count is zero or exceeds 256');
      if (optionalSize < (is64 ? 112 : 96)) throw new Error('PE optional header is too short to establish the section table');
      const table = await read(peOffset + 24 + optionalSize, count * 40);
      for (let i = 0; i < count; i++) {
        const at = i * 40, flags = u32(table, at + 36, true);
        add(nameOf(table.subarray(at, at + 8)), 'PE section · RVA', BigInt(u32(table, at + 12, true)),
          BigInt(u32(table, at + 20, true)), BigInt(u32(table, at + 16, true)), BigInt(u32(table, at + 8, true)),
          `${flags & 0x40000000 ? 'R' : '-'}${flags & 0x80000000 ? 'W' : '-'}${flags & 0x20000000 ? 'X' : '-'}`);
      }
    } else {
      const programCount = u16(header, is64 ? 56 : 44, little), programStride = u16(header, is64 ? 54 : 42, little);
      if (programCount > MAX_TABLE_ENTRIES) throw new Error('ELF program-header count exceeds 256 or uses extended numbering');
      if (programCount) {
        const offset = safeOffset(word(header, is64 ? 32 : 28));
        if (!offset || programStride < (is64 ? 56 : 32) || programStride > 256) throw new Error('Invalid ELF program-header table');
        const table = await read(offset, programCount * programStride);
        for (let i = 0; i < programCount; i++) {
          const at = i * programStride;
          if (u32(table, at, little) !== 1) continue;
          const flags = u32(table, at + (is64 ? 4 : 24), little);
          const fileSize = word(table, at + (is64 ? 32 : 16)), memorySize = word(table, at + (is64 ? 40 : 20));
          if (fileSize > memorySize) throw new Error('ELF load segment has file size larger than memory size');
          add(`PT_LOAD #${i}`, 'ELF segment · VA', word(table, at + (is64 ? 16 : 8)), word(table, at + (is64 ? 8 : 4)), fileSize, memorySize,
            `${flags & 4 ? 'R' : '-'}${flags & 2 ? 'W' : '-'}${flags & 1 ? 'X' : '-'}`);
        }
      }
      const offset = safeOffset(word(header, is64 ? 40 : 32));
      const count = u16(header, is64 ? 60 : 48, little), stride = u16(header, is64 ? 58 : 46, little);
      const stringIndex = u16(header, is64 ? 62 : 50, little);
      if ((!count && offset) || count > MAX_TABLE_ENTRIES || stringIndex === 0xffff) throw new Error('ELF section count exceeds 256 or uses extended numbering');
      if (!count && stringIndex) throw new Error('ELF section-name table index has no section table');
      if (count) {
        if (!offset || stride < (is64 ? 64 : 40) || stride > 256 || stringIndex >= count) throw new Error('Invalid ELF section-header table');
        const table = await read(offset, count * stride);
        let strings = null;
        if (stringIndex) {
          const at = stringIndex * stride;
          if (u32(table, at + 4, little) !== 3) throw new Error('ELF section-name table is not a string table');
          strings = await read(safeOffset(word(table, at + (is64 ? 24 : 16))), safeOffset(word(table, at + (is64 ? 32 : 20))));
        }
        for (let i = 1; i < count; i++) {
          const at = i * stride, type = u32(table, at + 4, little), nameIndex = u32(table, at, little);
          const flags = word(table, at + 8), size = word(table, at + (is64 ? 32 : 20));
          let name = '(unnamed)';
          if (nameIndex) {
            if (!strings || nameIndex >= strings.length) throw new Error('ELF section name is outside the string table');
            const bytes = strings.subarray(nameIndex, nameIndex + MAX_NAME_BYTES);
            if (bytes.indexOf(0) < 0) throw new Error('ELF section name is unterminated or exceeds 511 bytes');
            name = nameOf(bytes);
          }
          add(name, type === 8 ? 'ELF section · VA · NOBITS' : 'ELF section · VA', word(table, at + (is64 ? 16 : 12)),
            word(table, at + (is64 ? 24 : 16)), type === 8 ? 0n : size, size,
            `${flags & 1n ? 'W' : '-'}${flags & 4n ? 'X' : '-'}${flags & 2n ? 'A' : '-'}`);
        }
      }
    }
    layout.status = 'available';
  } catch (error) {
    layout.status = layout.entries.length ? 'partial' : 'unavailable';
    layout.reason = error.message;
  }
  return layout;
}

async function peMetadata(reader, peOffset, coff, is64) {
  const metadata = { status:'unavailable', entryPoint:null, dependencies:[], scope:'PE import-directory library names only; delay imports and imported symbols are not inspected.' };
  const read = metadataReader(reader);
  try {
    const optionalSize = u16(coff, 20, true), directory = is64 ? 112 : 96;
    if (optionalSize < directory) throw new Error('Optional header is too short');
    const optional = await read(peOffset + 24, optionalSize);
    const entry = u32(optional, 16, true);
    metadata.entryPoint = { kind:'RVA', address:address(entry), imageBase:address(is64 ? u64(optional, 24, true) : u32(optional, 28, true)), note:entry === 0 ? 'No entry point declared' : 'Declared address; execution is not verified' };
    const directories = u32(optional, directory - 4, true);
    if (directories > Math.floor((optionalSize - directory) / 8)) throw new Error('Data directories exceed optional header');
    if (directories < 2) { metadata.status = 'available'; return metadata; }
    const importRva = u32(optional, directory + 8, true), importSize = u32(optional, directory + 12, true);
    if (!importRva && !importSize) { metadata.status = 'available'; return metadata; }
    if (!importRva || importSize < 20) throw new Error('Invalid import directory');
    const count = u16(coff, 6, true);
    if (!count || count > MAX_TABLE_ENTRIES) throw new Error('Section count exceeds inspection limits');
    const sections = await read(peOffset + 24 + optionalSize, count * 40);
    const ranges = [{ address:0n, offset:0n, size:BigInt(u32(optional, 60, true)) }];
    for (let i = 0; i < count; i++) {
      const at = i * 40;
      ranges.push({ address:BigInt(u32(sections, at + 12, true)), size:BigInt(u32(sections, at + 16, true)), offset:BigInt(u32(sections, at + 20, true)) });
    }
    for (const range of ranges) if (range.offset + range.size > BigInt(reader.size)) throw new Error('Section data is outside the file');
    const table = mappedRange(ranges, BigInt(importRva), importSize);
    const entries = Math.min(Math.floor(importSize / 20), MAX_TABLE_ENTRIES + 1);
    const descriptors = await read(table.offset, entries * 20);
    for (let i = 0; i < entries; i++) {
      const at = i * 20;
      if (descriptors.subarray(at, at + 20).every(byte => byte === 0)) { metadata.status = 'available'; return metadata; }
      if (i === MAX_TABLE_ENTRIES) throw new Error('Import count exceeds 256 libraries');
      const nameRva = u32(descriptors, at + 12, true);
      if (!nameRva) throw new Error('Import descriptor has no library name');
      const name = mappedRange(ranges, BigInt(nameRva), 1);
      metadata.dependencies.push(await dependencyName(read, name.offset, name.available));
    }
    throw new Error('Import directory has no terminator');
  } catch (error) {
    metadata.status = metadata.dependencies.length ? 'partial' : 'unavailable';
    metadata.reason = error.message;
    return metadata;
  }
}
async function elfMetadata(reader, header) {
  const is64 = header[4] === 2, little = header[5] === 1;
  const word = (bytes, at) => is64 ? u64(bytes, at, little) : BigInt(u32(bytes, at, little));
  const elfType = u16(header, 16, little);
  const entryKinds = {
    0:'ELF e_entry',
    1:'Section-relative value',
    2:'Virtual address',
    3:'Virtual address in load-biased image',
    4:'Core-file entry value',
  };
  const entry = word(header, 24);
  const metadata = { status:'unavailable', entryPoint:{ kind:entryKinds[elfType] || 'ELF e_entry', address:address(entry), note:entry === 0n ? 'No entry point declared' : 'Declared value; runtime mapping and execution are not verified' }, dependencies:[], scope:'ELF DT_NEEDED library names only; symbols and libraries loaded at runtime are not inspected.' };
  const read = metadataReader(reader);
  try {
    const offset = safeOffset(word(header, is64 ? 32 : 28));
    const count = u16(header, is64 ? 56 : 44, little), stride = u16(header, is64 ? 54 : 42, little);
    if (count > MAX_TABLE_ENTRIES) throw new Error('Program-header count exceeds 256 or uses extended numbering');
    if (!count) { metadata.status = 'available'; return metadata; }
    if (stride < (is64 ? 56 : 32) || stride > 256 || !offset) throw new Error('Invalid program-header table');
    const table = await read(offset, count * stride), loads = [], dynamics = [];
    for (let i = 0; i < count; i++) {
      const at = i * stride, type = u32(table, at, little);
      if (type !== 1 && type !== 2) continue;
      const range = { offset:word(table, at + (is64 ? 8 : 4)), address:word(table, at + (is64 ? 16 : 8)), size:word(table, at + (is64 ? 32 : 16)) };
      if (range.offset + range.size > BigInt(reader.size)) throw new Error('Program segment is outside the file');
      (type === 1 ? loads : dynamics).push(range);
    }
    if (!dynamics.length) { metadata.status = 'available'; return metadata; }
    if (dynamics.length !== 1) throw new Error('Multiple dynamic segments are ambiguous');
    const dynamic = dynamics[0], size = is64 ? 16 : 8;
    if (!dynamic.size || dynamic.size % BigInt(size)) throw new Error('Invalid dynamic-segment size');
    const entries = Math.min(safeOffset(dynamic.size / BigInt(size)), 4096);
    const bytes = await read(safeOffset(dynamic.offset), entries * size);
    let stringTable = null, stringSize = null, terminated = false;
    const needed = [];
    for (let i = 0; i < entries; i++) {
      const tag = word(bytes, i * size), value = word(bytes, i * size + size / 2);
      if (tag === 0n) { terminated = true; break; }
      if (tag === 5n) { if (stringTable !== null) throw new Error('Duplicate dynamic string table'); stringTable = value; }
      if (tag === 10n) { if (stringSize !== null) throw new Error('Duplicate dynamic string size'); stringSize = value; }
      if (tag === 1n) {
        if (needed.length === MAX_TABLE_ENTRIES) throw new Error('Dependency count exceeds 256 libraries');
        needed.push(value);
      }
    }
    if (!terminated) throw new Error('Dynamic table is unterminated or exceeds 4096 entries');
    if (needed.length) {
      if (stringTable === null || stringSize === null || !stringSize) throw new Error('Dynamic string table is missing');
      const strings = mappedRange(loads, stringTable, safeOffset(stringSize));
      for (const name of needed) {
        if (name >= stringSize) throw new Error('Dependency string offset is outside the string table');
        metadata.dependencies.push(await dependencyName(read, strings.offset + safeOffset(name), safeOffset(stringSize - name)));
      }
    }
    metadata.status = 'available';
  } catch (error) {
    metadata.status = metadata.dependencies.length ? 'partial' : 'unavailable';
    metadata.reason = error.message;
  }
  return metadata;
}

function parsePe(reader, dos) {
  if (dos.length < 64) return result('unknown', null, null, null, 'truncated-dos-header');
  const peOffset = u32(dos, 0x3c, true);
  if (peOffset < 64 || peOffset > MAX_HEADER_OFFSET || peOffset > reader.size - 26) {
    return result('unknown', null, null, null, 'invalid-pe-header-offset');
  }
  return reader.read(peOffset, 26).then(async bytes => {
    if (!bytes || bytes.length !== 26) return result('unknown', null, null, null, 'truncated-pe-header');
    if (!has(bytes, [0x50, 0x45, 0, 0])) return result('unknown', null, null, null, 'invalid-pe-signature');
    const machine = u16(bytes, 4, true), optionalMagic = u16(bytes, 24, true);
    if (u16(bytes, 20, true) < 2) return result('unknown', null, null, null, 'invalid-pe-optional-header-size');
    const format = optionalMagic === 0x10b ? 'pe32' : optionalMagic === 0x20b ? 'pe32+' : null;
    if (!format) return result('ambiguous', 'pe', machines.get(machine) || null, null, 'unknown-pe-optional-header', { evidence:['PE signature', `COFF machine 0x${machine.toString(16)}`] });
    const architecture = machines.get(machine) || null;
    const widthMismatch = architecture && pe64Machines.has(machine) !== (format === 'pe32+');
    const identified = architecture && !widthMismatch;
    return result(identified ? 'identified' : 'ambiguous', format, architecture, 'windows', widthMismatch ? 'machine-optional-header-mismatch' : architecture ? 'identified' : 'unknown-pe-machine', {
      metadata:await peMetadata(reader, peOffset, bytes, format === 'pe32+'),
      layout:await fileLayout(reader, bytes, format === 'pe32+', peOffset),
      byteOrder:'little', evidence:['DOS/PE signatures', `COFF machine 0x${machine.toString(16)}`, `optional-header 0x${optionalMagic.toString(16)}`],
    });
  });
}

async function parseElf(reader, bytes) {
  if (bytes.length < 20) return result('unknown', null, null, null, 'truncated-elf-header');
  const klass = bytes[4], data = bytes[5];
  if (![1, 2].includes(klass) || ![1, 2].includes(data) || bytes[6] !== 1) return result('unknown', null, null, null, 'invalid-elf-ident');
  const expectedHeaderSize = klass === 1 ? 52 : 64;
  if (bytes.length < expectedHeaderSize) return result('unknown', null, null, null, 'truncated-elf-header');
  const little = data === 1;
  if (u32(bytes, 20, little) !== 1 || u16(bytes, klass === 1 ? 40 : 52, little) < expectedHeaderSize) return result('unknown', null, null, null, 'invalid-elf-header-version-or-size');
  const machine = u16(bytes, 18, little), architecture = elfMachines.get(machine) || null;
  const osabi = ({ 0:'system-v', 1:'hp-ux', 2:'netbsd', 3:'linux-gnu', 6:'solaris', 9:'freebsd', 12:'openbsd' })[bytes[7]] || null;
  return result(architecture ? 'identified' : 'ambiguous', klass === 1 ? 'elf32' : 'elf64', architecture, osabi, architecture ? (osabi ? 'identified' : 'unknown-elf-osabi') : 'unknown-elf-machine', {
    metadata:await elfMetadata(reader, bytes),
    layout:await fileLayout(reader, bytes, klass === 2),
    byteOrder:little ? 'little' : 'big', evidence:['ELF magic', `class ${klass === 1 ? 32 : 64}`, `e_machine ${machine}`, `e_ident OSABI ${bytes[7]}`],
  });
}

async function machMetadata(reader, header, is64, little) {
  const layout = { status:'unavailable', entries:[], reason:null,
    scope:'Thin Mach-O segment and section declarations only; nested filesets and live mappings are not inspected.' };
  const metadata = { status:'unavailable', entryPoint:null, dependencies:[],
    scope:'Mach-O dylib load-command names only; rpaths, symbols and runtime-loaded libraries are not resolved.' };
  const read = metadataReader(reader);
  const word = (bytes, at) => is64 ? u64(bytes, at, little) : BigInt(u32(bytes, at, little));
  const nameOf = bytes => {
    const end = bytes.indexOf(0);
    return Array.from(end < 0 ? bytes : bytes.subarray(0, end), byte => byte >= 32 && byte < 127
      ? String.fromCharCode(byte) : `\\x${byte.toString(16).padStart(2, '0')}`).join('') || '(unnamed)';
  };
  const add = (name, kind, virtualAddress, offset, fileSize, memorySize, permissions) => {
    if (layout.entries.length >= 512) throw new Error('Mach-O layout exceeds 512 entries');
    if (fileSize && (offset > BigInt(reader.size) || fileSize > BigInt(reader.size) - offset)) throw new Error('Mach-O file range is outside the file');
    layout.entries.push({ name, kind, address:address(virtualAddress), offset:address(offset),
      fileSize:fileSize.toString(), memorySize:memorySize.toString(), permissions });
  };
  let layoutError = null, dependencyError = null;
  try {
    const count = u32(header, 16, little), total = u32(header, 20, little), start = is64 ? 32 : 28;
    if (count > MAX_TABLE_ENTRIES || total > MAX_METADATA_BYTES || total > reader.size - start) throw new Error('Mach-O command table exceeds file or inspection limits');
    let offset = start;
    for (let i = 0; i < count; i++) {
      if (start + total - offset < 8) throw new Error('Mach-O command header is truncated');
      const head = await read(offset, 8), cmd = u32(head, 0, little), size = u32(head, 4, little);
      if (size < 8 || size % (is64 ? 8 : 4) || size > start + total - offset) throw new Error('Mach-O command size or alignment is invalid');
      if (cmd === 1 || cmd === 0x19) {
        try {
          if ((cmd === 0x19) !== is64) throw new Error('Mach-O segment width differs from header width');
          const fixed = is64 ? 72 : 56, stride = is64 ? 80 : 68;
          if (size < fixed) throw new Error('Mach-O segment command is truncated');
          const segment = await read(offset, fixed), sections = u32(segment, is64 ? 64 : 48, little);
          if (sections > MAX_TABLE_ENTRIES || sections * stride > size - fixed) throw new Error('Mach-O section table exceeds command or inspection limits');
          const protection = u32(segment, is64 ? 60 : 44, little);
          add(nameOf(segment.subarray(8, 24)), 'Mach-O segment · declared VA', word(segment, 24), word(segment, is64 ? 40 : 32),
            word(segment, is64 ? 48 : 36), word(segment, is64 ? 32 : 28), `init_prot=0x${protection.toString(16)}`);
          for (let section = 0; section < sections; section++) {
            const row = await read(offset + fixed + section * stride, stride), flags = u32(row, is64 ? 64 : 56, little);
            const zeroFill = [1, 0xc, 0x12].includes(flags & 0xff), length = word(row, is64 ? 40 : 36);
            add(`${nameOf(row.subarray(16, 32))}/${nameOf(row.subarray(0, 16))}`,
              zeroFill ? 'Mach-O section · declared VA · zero fill' : 'Mach-O section · declared VA',
              word(row, 32), BigInt(u32(row, is64 ? 48 : 40, little)), zeroFill ? 0n : length, length, `flags=0x${flags.toString(16)}`);
          }
        } catch (error) { layoutError ||= error.message; }
      } else if ([0xc, 0x80000018, 0x8000001f, 0x20, 0x80000023].includes(cmd)) {
        try {
          if (size < 24) throw new Error('Mach-O dylib command is truncated');
          const command = await read(offset, 24), name = u32(command, 8, little);
          if (name < 24 || name >= size || metadata.dependencies.length >= MAX_TABLE_ENTRIES) throw new Error('Mach-O dylib name offset or count is invalid');
          metadata.dependencies.push(await dependencyName(read, offset + name, size - name));
        } catch (error) { dependencyError ||= error.message; }
      }
      offset += size;
    }
    if (offset !== start + total) throw new Error('Mach-O command count does not cover its declared byte span');
    layout.status = layoutError ? layout.entries.length ? 'partial' : 'unavailable' : 'available';
    metadata.status = dependencyError ? metadata.dependencies.length ? 'partial' : 'unavailable' : 'available';
  } catch (error) {
    layoutError ||= error.message; dependencyError ||= error.message;
    layout.status = layout.entries.length ? 'partial' : 'unavailable';
    metadata.status = metadata.dependencies.length ? 'partial' : 'unavailable';
  }
  layout.reason = layoutError; if (dependencyError) metadata.reason = dependencyError;
  return { layout, metadata };
}

function classifyMachO(bytes, magic) {
  const little = magic === 'cefaedfe' || magic === 'cffaedfe';
  const is64 = magic === 'cffaedfe' || magic === 'feedfacf';
  if (bytes.length < (is64 ? 32 : 28)) return result('unknown', null, null, null, 'truncated-mach-o-header');
  const cpu = u32(bytes, 4, little), architecture = machMachines.get(cpu) || null;
  const widthMismatch = architecture && Boolean(cpu & 0x01000000) !== is64;
  return result(architecture && !widthMismatch ? 'identified' : 'ambiguous', is64 ? 'macho64' : 'macho32', architecture, null,
    widthMismatch ? 'cpu-header-width-mismatch' : architecture ? 'abi-not-in-header' : 'unknown-mach-o-cpu', {
    byteOrder:little ? 'little' : 'big', evidence:['Mach-O magic', `CPU type 0x${cpu.toString(16)}`],
  });
}

async function parseMachO(reader, bytes, magic) {
  const identity = classifyMachO(bytes, magic);
  if (!identity.format) return identity;
  const tables = await machMetadata(reader, bytes, identity.format === 'macho64', identity.byteOrder === 'little');
  return { ...identity, ...tables };
}

async function wasmLayout(reader) {
  const layout = { status:'unavailable', entries:[], reason:null,
    scope:'WebAssembly section file spans and bounded custom names only; contents, ordering, imports and executable validity are not validated.' };
  const read = metadataReader(reader);
  const labels = ['Custom', 'Type', 'Import', 'Function', 'Table', 'Memory', 'Global',
    'Export', 'Start', 'Element', 'Code', 'Data', 'Data count', 'Tag'];
  const leb32 = (bytes, offset) => {
    let value = 0n;
    for (let i = 0; i < 5; i++) {
      if (offset + i >= bytes.length) throw new Error('Wasm section integer is truncated');
      const byte = bytes[offset + i];
      value |= BigInt(byte & 0x7f) << BigInt(i * 7);
      if (!(byte & 0x80)) {
        if (value > 0xffffffffn) throw new Error('Wasm section integer exceeds u32');
        return { value:Number(value), size:i + 1 };
      }
    }
    throw new Error('Wasm section integer exceeds five bytes');
  };
  try {
    let offset = 8;
    while (offset < reader.size) {
      if (layout.entries.length >= MAX_TABLE_ENTRIES) throw new Error('Wasm section count exceeds 256');
      const header = await read(offset, Math.min(6, reader.size - offset));
      const id = header[0], length = leb32(header, 1), body = offset + 1 + length.size;
      if (length.value > reader.size - body) throw new Error('Wasm section extends beyond the file');
      let name = labels[id] || `Unknown section id ${id}`;
      if (id === 0) {
        const prefix = await read(body, Math.min(5, length.value)), declaredName = leb32(prefix, 0);
        if (declaredName.value > MAX_NAME_BYTES - 1 || declaredName.value > length.value - declaredName.size) {
          throw new Error('Wasm custom name exceeds its section or inspection limit');
        }
        const bytes = await read(body + declaredName.size, declaredName.value);
        new TextDecoder('utf-8', { fatal:true }).decode(bytes);
        const escaped = Array.from(bytes, byte => byte >= 32 && byte < 127
          ? String.fromCharCode(byte) : `\\x${byte.toString(16).padStart(2, '0')}`).join('');
        name = `Custom · ${escaped || '(empty name)'}`;
      }
      layout.entries.push({ name, kind:`Wasm section id ${id} · file span`, address:'Unavailable',
        offset:address(BigInt(offset)), fileSize:String(1 + length.size + length.value),
        memorySize:'Unavailable', permissions:'Unavailable' });
      offset = body + length.value;
    }
    layout.status = 'available';
  } catch (error) {
    layout.status = layout.entries.length ? 'partial' : 'unavailable'; layout.reason = error.message;
  }
  return layout;
}

async function parseFat(reader, firstFour) {
  const magic = hexMagic(firstFour);
  const little = magic === 'bebafeca' || magic === 'bfbafeca';
  const is64 = magic === 'cafebabf' || magic === 'bfbafeca';
  const head = await reader.read(0, 8);
  if (!head || head.length !== 8) return result('unknown', null, null, null, 'truncated-fat-header');
  const count = u32(head, 4, little);
  if (!count || count > MAX_FAT_ARCHES) return result('ambiguous', 'macho-fat', null, null, 'fat-architecture-count-out-of-range', { byteOrder:little ? 'little' : 'big' });
  const entrySize = is64 ? 32 : 20;
  const table = await reader.read(8, count * entrySize);
  if (!table || table.length !== count * entrySize) return result('unknown', null, null, null, 'truncated-fat-architecture-table');
  const archs = [];
  const ranges = [];
  for (let i = 0; i < count; i++) {
    const at = i * entrySize, cpu = u32(table, at, little);
    const offset = is64 ? u64(table, at + 8, little) : BigInt(u32(table, at + 8, little));
    const size = is64 ? u64(table, at + 16, little) : BigInt(u32(table, at + 12, little));
    const alignment = u32(table, at + (is64 ? 24 : 16), little);
    const tableEnd = BigInt(8 + count * entrySize);
    if (!size || offset < tableEnd) return result('unknown', null, null, null, 'invalid-fat-slice-range');
    if (offset > BigInt(reader.size) || size > BigInt(reader.size) - offset) return result('unknown', null, null, null, 'fat-slice-out-of-bounds');
    if (alignment > 63 || offset % (1n << BigInt(alignment)) !== 0n) return result('unknown', null, null, null, 'invalid-fat-slice-alignment');
    ranges.push({ start:offset, end:offset + size });
    const sliceSize = Number(size < 32n ? size : 32n);
    const sliceHeader = await reader.read(Number(offset), sliceSize);
    if (!sliceHeader || sliceHeader.length !== sliceSize || sliceHeader.length < 4) return result('unknown', null, null, null, 'truncated-fat-slice-header');
    const sliceMagic = hexMagic(sliceHeader.subarray(0, 4));
    if (!['cefaedfe', 'feedface', 'cffaedfe', 'feedfacf'].includes(sliceMagic)) return result('unknown', null, null, null, 'invalid-fat-slice-magic');
    const sliceIdentity = classifyMachO(sliceHeader, sliceMagic);
    if (sliceIdentity.reason === 'truncated-mach-o-header') return result('unknown', null, null, null, 'truncated-fat-slice-header');
    if (sliceIdentity.evidence?.[1] !== `CPU type 0x${cpu.toString(16)}`) return result('ambiguous', 'macho-fat', null, null, 'fat-slice-cpu-mismatch');
    if (sliceIdentity.reason === 'cpu-header-width-mismatch') return result('ambiguous', 'macho-fat', null, null, 'fat-slice-width-mismatch');
    const architecture = machMachines.get(cpu);
    if (architecture && !archs.includes(architecture)) archs.push(architecture);
    else if (!architecture) archs.push(null);
  }
  ranges.sort((a, b) => a.start < b.start ? -1 : a.start > b.start ? 1 : 0);
  if (ranges.some((range, index) => index > 0 && ranges[index - 1].end > range.start)) return result('unknown', null, null, null, 'overlapping-fat-slices');
  const knownArchitectures = [...new Set(archs.filter(Boolean))];
  const architecture = knownArchitectures.length === 1 ? knownArchitectures[0] : null;
  const hasUnknownArchitecture = archs.some(value => value === null);
  const fullyIdentified = architecture && !hasUnknownArchitecture && knownArchitectures.length === 1;
  return result(architecture ? 'identified' : 'ambiguous', 'macho-fat', architecture, null,
    hasUnknownArchitecture ? 'unknown-fat-slice-architecture' : knownArchitectures.length > 1 ? 'multiple-architectures' : architecture ? 'abi-not-in-header' : 'unknown-mach-o-cpu', {
      status:fullyIdentified ? 'identified' : 'ambiguous',
      byteOrder:little ? 'little' : 'big', architectures:knownArchitectures, evidence:[`fat Mach-O ${is64 ? '64' : '32'} table`, `${count} slices`, 'slice ranges, alignment and Mach-O headers are valid'],
    });
}

/**
 * Classify a Blob/File, ArrayBuffer, or typed-array view from headers and bounded PE/ELF/Mach-O/Wasm metadata.
 * Resolves to telos.binary-identity/v1. `status` is identified, ambiguous, or
 * unknown; absent format, architecture, and ABI facts are null (never guessed).
 * `format` values are pe32, pe32+, pe (unknown PE optional header), elf32,
 * elf64, macho32, macho64, macho-fat, and wasm. This is metadata, not evidence
 * of runtime or execution compatibility.
 */
export async function identifyBinaryHeader(input) {
  const reader = makeReader(input);
  if (!reader) return result('unknown', null, null, null, 'unsupported-input');
  if (!Number.isSafeInteger(reader.size) || reader.size < 4) return result('unknown', null, null, null, 'truncated-header');
  try {
    const first = await reader.read(0, Math.min(64, reader.size));
    if (!first || first.length < 4) return result('unknown', null, null, null, 'truncated-header');
    if (has(first, [0x4d, 0x5a])) return await parsePe(reader, first);
    if (has(first, [0x7f, 0x45, 0x4c, 0x46])) return await parseElf(reader, first);
    if (has(first, [0, 0x61, 0x73, 0x6d])) {
      if (first.length < 8) return result('unknown', null, null, null, 'truncated-wasm-header');
      const version = u32(first, 4, true);
      return version === 1
        ? result('ambiguous', 'wasm', null, null, 'architecture-and-abi-not-in-header', {
          byteOrder:'little', evidence:['WebAssembly magic', 'core module version 1'], layout:await wasmLayout(reader) })
        : result('unknown', null, null, null, 'unsupported-wasm-version');
    }
    const magic = hexMagic(first.subarray(0, 4));
    if (['cefaedfe', 'feedface', 'cffaedfe', 'feedfacf'].includes(magic)) return await parseMachO(reader, first, magic);
    if (['cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca'].includes(magic)) return await parseFat(reader, first.subarray(0, 4));
    return result('unknown', null, null, null, 'unrecognized-magic');
  } catch {
    return result('unknown', null, null, null, 'header-read-failed');
  }
}
