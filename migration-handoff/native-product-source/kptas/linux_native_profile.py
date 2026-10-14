"""Static ELF profile inspection; this never launches or admits a native backend.

Linux K2 needs an ELF-native adapter. A matching profile is evidence about
instruction bytes only, not proof of tick, input, capture or restore support.
"""
from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping


class LinuxProfileError(ValueError):
    pass


def inspect_profile(image: bytes, table: Mapping, *, game: str = 'k2') -> dict:
    """Inspect immutable image bytes against an explicitly pinned ELF32 profile.

    `sites` maps names to {address: int, expected: nonempty hexadecimal bytes}.
    Addresses are ELF virtual addresses, never PE RVAs or Wine addresses.
    """
    if type(image) is not bytes or len(image) > 256 << 20:
        raise LinuxProfileError('Expected bounded immutable executable bytes')
    if not isinstance(table, Mapping) or game != 'k2' or table.get('game') != game:
        raise LinuxProfileError('Requested Linux title differs from profile')
    if table.get('format') != 'elf32-i386':
        raise LinuxProfileError('Profile must explicitly declare elf32-i386')
    digest = hashlib.sha256(image).hexdigest()
    if table.get('reference_sha256') != digest:
        raise LinuxProfileError('Executable SHA256 differs from profile')

    def span(offset, size):
        if offset < 0 or size < 0 or offset + size > len(image):
            raise LinuxProfileError('ELF span exceeds executable bounds')
        return image[offset:offset + size]

    header = span(0, 52)
    if header[:7] != b'\x7fELF\x01\x01\x01':
        raise LinuxProfileError('Expected little-endian ELF32 version1')
    fields = struct.unpack('<HHIIIIIHHHHHH', header[16:])
    kind, machine, version, entry, phoff = fields[:5]
    ehsize, phsize, phcount = fields[7:10]
    if kind != 2 or machine != 3 or version != 1 or ehsize != 52:
        raise LinuxProfileError('Expected i386 ET_EXEC image')
    if phsize != 32 or not 1 <= phcount <= 4096:
        raise LinuxProfileError('Unsupported ELF program-header layout')
    span(phoff, phsize * phcount)
    segments = []
    for index in range(phcount):
        p = struct.unpack('<IIIIIIII', span(phoff + index * phsize, phsize))
        ptype, offset, address, _physical, filesz, memsz, flags, align = p
        if filesz > memsz or address + memsz > 1 << 32:
            raise LinuxProfileError('Invalid ELF segment extent')
        span(offset, filesz)
        if ptype == 1:
            if align not in (0, 1) and (align & (align - 1) or address % align != offset % align):
                raise LinuxProfileError('Invalid ELF load-segment alignment')
            segments.append(dict(address=address, offset=offset, filesz=filesz,
                                 memsz=memsz, flags=flags))

    def instruction(address, size):
        matches = [s for s in segments if s['flags'] & 1
                   and s['address'] <= address
                   and address + size <= s['address'] + s['filesz']]
        if len(matches) != 1:
            raise LinuxProfileError('Instruction span must have one executable file mapping')
        s = matches[0]
        return span(s['offset'] + address - s['address'], size)

    instruction(entry, 1)
    sites = table.get('sites', {})
    if not isinstance(sites, Mapping) or len(sites) > 4096:
        raise LinuxProfileError('Invalid bounded instruction-site table')
    checked = []
    for name, site in sites.items():
        if type(name) is not str or not name or not isinstance(site, Mapping):
            raise LinuxProfileError('Invalid named instruction site')
        address, encoded = site.get('address'), site.get('expected')
        if type(address) is not int or not 0 <= address < 1 << 32:
            raise LinuxProfileError('Invalid ELF virtual instruction address')
        if type(encoded) is not str or not 2 <= len(encoded) <= 8192:
            raise LinuxProfileError('Expected bounded nonempty instruction bytes')
        try:
            expected = bytes.fromhex(encoded)
        except ValueError as exc:
            raise LinuxProfileError('Invalid instruction-byte encoding') from exc
        if not expected or instruction(address, len(expected)) != expected:
            raise LinuxProfileError(f'Instruction bytes differ at {name}')
        checked.append(dict(name=name, address=address, bytes=len(expected)))
    return dict(game=game, format='elf32-i386', exe_sha256=digest,
                entry=entry, load_segments=segments, checked_sites=checked,
                static_bytes_verified=True, native_backend_admitted=False,
                reason='ELF adapter and runtime fidelity qualification are required')
