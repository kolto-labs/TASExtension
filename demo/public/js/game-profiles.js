const profiles = new Map();

export function registerGameProfile(profile) {
  if (!profile || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(profile.id)
    || typeof profile.name !== 'string' || !profile.name.trim()
    || typeof profile.executable !== 'string' || !/^[^/\\]+\.exe$/i.test(profile.executable)
    || typeof profile.archiveNamespace !== 'string' || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(profile.archiveNamespace)
    || typeof profile.binary !== 'string' || !profile.binary.trim()
    || !Array.isArray(profile.requiredFiles)
    || profile.requiredFiles.some(file => !file || typeof file.name !== 'string' || !/^[^/\\]+$/.test(file.name) || typeof file.label !== 'string' || !file.label.trim())) {
    throw new TypeError('Invalid game profile.');
  }
  if (profiles.has(profile.id) || [...profiles.values()].some(item => item.executable.toLowerCase() === profile.executable.toLowerCase())) {
    throw new Error(`Game profile ID or executable is already registered: ${profile.id}`);
  }
  const registered = Object.freeze({
    ...profile,
    executable: profile.executable.toLowerCase(),
    requiredFiles: Object.freeze(profile.requiredFiles.map(file => Object.freeze({ ...file }))),
  });
  profiles.set(registered.id, registered);
  return registered;
}

registerGameProfile({
    id: 'k1',
    name: 'STAR WARS: Knights of the Old Republic',
    executable: 'swkotor.exe',
    archiveNamespace: 'k1',
    binary: 'Windows PE32 / x86',
    requiredFiles: Object.freeze([
      Object.freeze({ name: 'chitin.key', label: 'Resource index' }),
      Object.freeze({ name: 'dialog.tlk', label: 'Dialog text' }),
      Object.freeze({ name: 'binkw32.dll', label: 'Bink video runtime' }),
      Object.freeze({ name: 'mss32.dll', label: 'Miles audio runtime' }),
    ]),
  });
registerGameProfile({
    id: 'k2',
    name: 'STAR WARS: Knights of the Old Republic II',
    executable: 'swkotor2.exe',
    archiveNamespace: 'k2',
    binary: 'Windows PE32 / x86',
    requiredFiles: Object.freeze([
      Object.freeze({ name: 'chitin.key', label: 'Resource index' }),
      Object.freeze({ name: 'dialog.tlk', label: 'Dialog text' }),
      Object.freeze({ name: 'binkw32.dll', label: 'Bink video runtime' }),
      Object.freeze({ name: 'mss32.dll', label: 'Miles audio runtime' }),
    ]),
  });

export const DEFAULT_GAME_PROFILE = profiles.get('k1');
export function gameProfileById(id) {
  return profiles.get(id) || null;
}

async function isPe32X86(file) {
  if (!file || file.size < 64) return false;
  const dos = new DataView(await file.slice(0, 64).arrayBuffer());
  if (dos.getUint16(0, true) !== 0x5a4d) return false;
  const peOffset = dos.getUint32(0x3c, true);
  if (peOffset > file.size - 26) return false;
  const header = new DataView(await file.slice(peOffset, peOffset + 26).arrayBuffer());
  return header.getUint32(0, true) === 0x00004550
    && header.getUint16(4, true) === 0x014c
    && header.getUint16(24, true) === 0x010b;
}

async function readPe32Imports(file) {
  const dos = new DataView(await file.slice(0, 64).arrayBuffer());
  const peOffset = dos.getUint32(0x3c, true);
  const head = new DataView(await file.slice(peOffset, Math.min(file.size, peOffset + 64 * 1024)).arrayBuffer());
  const sectionCount = head.getUint16(6, true), optionalSize = head.getUint16(20, true), optional = 24;
  if (sectionCount > 96 || optionalSize < 112 || optional + optionalSize + sectionCount * 40 > head.byteLength) return null;
  const importRva = head.getUint32(optional + 96 + 8, true);
  if (!importRva) return [];
  const sections = Array.from({ length:sectionCount }, (_, index) => {
    const at = optional + optionalSize + index * 40;
    return { virtualSize:head.getUint32(at + 8, true), virtualAddress:head.getUint32(at + 12, true), rawSize:head.getUint32(at + 16, true), rawAddress:head.getUint32(at + 20, true) };
  });
  const fileOffset = rva => {
    const section = sections.find(item => rva >= item.virtualAddress && rva - item.virtualAddress < Math.max(item.virtualSize, item.rawSize));
    if (!section) return null;
    const delta = rva - section.virtualAddress;
    if (delta >= section.rawSize) return null;
    const offset = section.rawAddress + delta;
    return offset < file.size ? offset : null;
  };
  const descriptorOffset = fileOffset(importRva);
  if (descriptorOffset === null) return null;
  const imports = new Set();
  for (let offset = descriptorOffset, count = 0; count < 256 && offset + 20 <= file.size; count++, offset += 20) {
    const descriptor = new DataView(await file.slice(offset, offset + 20).arrayBuffer());
    const nameRva = descriptor.getUint32(12, true);
    if (!nameRva) return [...imports];
    const nameOffset = fileOffset(nameRva);
    if (nameOffset === null) return null;
    const bytes = new Uint8Array(await file.slice(nameOffset, Math.min(file.size, nameOffset + 260)).arrayBuffer());
    const terminator = bytes.indexOf(0);
    if (terminator <= 0) return null;
    imports.add(new TextDecoder('ascii').decode(bytes.subarray(0, terminator)).toLowerCase());
  }
  return null;
}

const WINDOWS_SYSTEM_DLLS = new Set([
  'winmm.dll', 'opengl32.dll', 'kernel32.dll', 'user32.dll', 'gdi32.dll', 'shell32.dll',
  'ole32.dll', 'dinput8.dll', 'glu32.dll', 'version.dll', 'imm32.dll',
]);

export async function identifyGameProfile(entries) {
  const candidates = [...profiles.values()].filter(profile =>
    entries.find(entry => entry.name.toLowerCase() === profile.executable));
  if (candidates.length > 1) {
    return { profile: null, error: 'Choose one game installation at a time; multiple registered game executables were found.' };
  }
  if (!candidates.length) {
    const nativeGameBinary = entries.find(entry => !entry.name.includes('/') && ['swkotor', 'swkotor2'].includes(entry.name.toLowerCase()));
    const otherExecutable = entries.find(entry => !entry.name.includes('/') && /\.exe$/i.test(entry.name));
    return nativeGameBinary
      ? { profile: null, error: 'This runtime currently accepts the Windows PE32 (i386) KOTOR executable. The selected Linux/macOS/native binary needs a matching platform runtime adapter.', importedLibraries: [] }
      : otherExecutable
      ? { profile: null, error: 'This browser runtime supports the Windows x86 KOTOR 1 and KOTOR 2 executables only. Other game and binary formats are not supported.', importedLibraries: [] }
      : { profile: null, target: 'unknown', error: null, importedLibraries: [] };
  }
  const profile = candidates[0];
  if (profile.id !== 'k1' && profile.id !== 'k2') {
    return { profile: null, error: `The ${profile.name} profile is registered, but this browser runtime has no matching runtime adapter.`, importedLibraries: [] };
  }
  if (profile.id === 'k2') {
    const executable = entries.find(entry => entry.name.toLowerCase() === profile.executable);
    if (!(await isPe32X86(executable.file))) {
      return { profile: null, error: 'KOTOR 2 requires a valid Windows PE32 (i386) swkotor2.exe. Linux, macOS, Android, console, 64-bit, and other executable formats need a different runtime adapter.', importedLibraries: [] };
    }
    const importedLibraries = await readPe32Imports(executable.file);
    if (!importedLibraries) return { profile:null, error:'KOTOR 2 has an invalid or unsupported PE import table.', importedLibraries:[] };
    return { profile, error:null, importedLibraries:importedLibraries.filter(name => !WINDOWS_SYSTEM_DLLS.has(name)) };
  }
  return { profile, error: null, importedLibraries: [] };
}
