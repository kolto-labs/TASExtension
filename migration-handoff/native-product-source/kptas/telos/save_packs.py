"""Published KotOR 1 speedrun save packs: verify, install into a game's Saves folder, remove.

The packs are the community route saves on speedrun.com. Each archive is
checked against its recorded SHA-256 before any slot is read. A missing or
different archive is downloaded again from its speedrun.com resource; a
different local file is moved aside, never deleted.

How the game lists saves (K1 GOG 1.03, read from the disassembly of
CSWGuiSaveLoad::PopulateGameList 0x006CC160; not observed on screen):
- It lists the SAVES: folder sorted by folder name, byte order (mode 1 of the
  directory lister; mode 2 would sort by file time).
- The slot number is the integer before " - " in the folder name. 0 is the
  Quick Save and 1 the Auto Save. The load list shows SAVENFO SAVEGAMENAME.
- An in-game new save takes the first free number from 2.

So installed slots get numbers 100+ (ChaosDrifter) and 200+ (Lane). A save
already in 000002-000099, such as the "000007 - Game6" reference save, keeps
its place at the top of the list, and --load-game6 still lands on it.

Install never overwrites or deletes a folder it did not create. It writes a
manifest beside the Saves folder (stasis-speedrun-saves.json, outside Saves
because the game lists that folder). Remove deletes only folders whose files
still match that manifest.

The display name edit rebuilds only SAVENFO.res. The rebuilt file must parse
to the same tree as the original except SAVEGAMENAME, or the slot is
installed with its original name.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import uuid
import zipfile

MANIFEST_NAME = 'stasis-speedrun-saves.json'
SLOT_FILES = ('savegame.sav', 'savenfo.res', 'partytable.res', 'globalvars.res', 'screen.tga', 'pifo.ifo')
ARCHIVE_LIMIT = 64 * 1024 * 1024
FILE_LIMIT = 16 * 1024 * 1024
NAME_LIMIT = 60


class SavePackError(ValueError):
    pass


@dataclass(frozen=True)
class SavePack:
    key: str
    label: str        # prefix in the in-game name, e.g. "Chaos 10.1"
    author: str
    published_as: str
    version: str
    category: str
    resource: str
    url: str
    archive: str
    sha256: str
    bytes: int
    root: str         # folder inside the archive that holds the slots
    base: int         # installed slot number = base + original number
    published: str    # speedrun.com resource updatedAt (UTC date)


PACKS = (
    SavePack('chaosdrifter-anyu-10.1', 'Chaos 10.1', 'ChaosDrifter', 'ChaosDrifting', 'Any% Unrestricted 10.1',
             'Any% Unrestricted', 'https://www.speedrun.com/kotor1/resources/kjs9s',
             'https://www.speedrun.com/static/resource/kjs9s.zip?v=0f1d356', 'savesanyu_65f1a142.zip',
             'b32261e9cbe45fb09bc15a1888580fa492613f5c4c5ab5076f7b05d91bcbccbe', 15818072, 'saves', 100, '2024-12-29'),
    SavePack('lane-any-9.4', 'Lane 9.4', 'Lane', 'Lane', 'Any% 9.4', 'Any%',
             'https://www.speedrun.com/kotor1/resources/whtjt',
             'https://www.speedrun.com/static/resource/whtjt.zip?v=ce7e89d', 'any9.4_8030d36e.zip',
             'a10ca34b269d5c5982182bb21909dfe1e089ceecc6cad31bf2e9cc121731331d', 17386178, 'any%9.4', 200, '2022-07-31'),
)


def pack(key):
    for item in PACKS:
        if key.casefold() in (item.key, item.label.casefold(), item.author.casefold(), item.label.split()[0].casefold()):
            return item
    raise SavePackError(f'unknown save pack {key!r}; choose one of {", ".join(p.key for p in PACKS)}')


def archive_directory() -> Path:
    if os.environ.get('KPTAS_SAVE_PACKS'):
        return Path(os.environ['KPTAS_SAVE_PACKS']).expanduser()
    base = os.environ.get('XDG_DATA_HOME')
    return (Path(base) if base else Path.home() / '.local/share') / 'kptas' / 'save-packs'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------- archives

def ensure_archive(item: SavePack, directory=None, *, download=True, log=print) -> Path:
    """Path of a verified archive. Downloads it when missing or different."""
    directory = Path(directory).expanduser() if directory else archive_directory()
    path = directory / item.archive
    if path.is_file():
        actual = _file_sha(path)
        if actual == item.sha256:
            log(f'{item.label}: archive verified ({item.archive}, sha256 {item.sha256[:12]}).')
            return path
        if not download:
            raise SavePackError(f'{path} does not match the published {item.label} pack '
                                f'(sha256 {actual[:12]}, expected {item.sha256[:12]}).')
        aside = path.with_name(f'{path.name}.mismatch-{actual[:12]}')
        os.replace(path, aside)
        log(f'{item.label}: {path.name} did not match. Moved it to {aside.name}.')
    elif not download:
        raise SavePackError(f'{path} is missing. Run again without --no-download, or put the archive there.')
    directory.mkdir(parents=True, exist_ok=True)
    log(f'{item.label}: downloading {item.resource}')
    import urllib.request
    temporary = directory / f'.{item.archive}.{uuid.uuid4().hex}.part'
    try:
        request = urllib.request.Request(item.url, headers={'User-Agent': 'Mozilla/5.0 (Stasis save installer)'})
        h, size = hashlib.sha256(), 0
        with urllib.request.urlopen(request, timeout=60) as response, open(temporary, 'xb') as stream:
            while block := response.read(1 << 20):
                size += len(block)
                if size > ARCHIVE_LIMIT:
                    raise SavePackError(f'{item.url} is larger than {ARCHIVE_LIMIT >> 20} MiB; stopped.')
                h.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if h.hexdigest() != item.sha256:
            raise SavePackError(f'the download from {item.url} does not match the recorded pack '
                                f'(sha256 {h.hexdigest()[:12]}). speedrun.com may have a new version.')
        os.link(temporary, path)  # Never replaces a file that appeared meanwhile.
    finally:
        temporary.unlink(missing_ok=True)
    log(f'{item.label}: downloaded and verified {path}.')
    return path


@dataclass(frozen=True)
class Slot:
    folder: str                 # original folder, e.g. "000015 - Game14"
    number: int
    files: tuple                # ((name as published, bytes), ...)


def _slot_number(folder):
    head, sep, _ = folder.partition(' - ')
    return int(head) if sep and head.isdigit() else None


def read_slots(item: SavePack, archive: Path) -> list[Slot]:
    """Every slot folder of a verified archive, with the allowed save files only."""
    slots: dict[str, list] = {}
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 2000:
            raise SavePackError(f'{archive.name}: too many entries')
        for entry in z.infolist():
            name = entry.filename.replace('\\', '/')
            parts = PurePosixPath(name).parts
            if entry.is_dir():
                continue
            if name.startswith('/') or '..' in parts or stat.S_ISLNK(entry.external_attr >> 16):
                raise SavePackError(f'{archive.name}: unsafe member {name!r}')
            if len(parts) != 3 or parts[0] != item.root or parts[2].casefold() not in SLOT_FILES:
                continue  # Nested archives, steam_autocloud.vdf and other extras stay out.
            if _slot_number(parts[1]) is None:
                continue
            if entry.file_size > FILE_LIMIT:
                raise SavePackError(f'{archive.name}: {name} is larger than {FILE_LIMIT >> 20} MiB')
            slots.setdefault(parts[1], []).append((parts[2], z.read(entry)))
    result = []
    for folder, files in sorted(slots.items()):
        names = {n.casefold() for n, _ in files}
        if len(names) != len(files) or not {'savegame.sav', 'savenfo.res'} <= names:
            raise SavePackError(f'{archive.name}: slot {folder} is incomplete or has duplicate files')
        result.append(Slot(folder, _slot_number(folder), tuple(sorted(files))))
    return result


# ---------------------------------------------------------------- GFF (SAVENFO.res)

def gff_tree(raw: bytes):
    """Typed tree of a GFF V3.2 file: {label: (type, value)}; lists and structs nest."""
    if len(raw) < 56 or raw[4:8] != b'V3.2':
        raise SavePackError('not a GFF V3.2 file')
    so, sn, fo, fn, lo, ln, do, dn, io, inn, lio, lin = struct.unpack_from('<12I', raw, 8)
    if max(so + sn * 12, fo + fn * 12, lo + ln * 16, do + dn, io + inn, lio + lin) > len(raw):
        raise SavePackError('GFF table outside the file')
    labels = [raw[lo + i * 16:lo + i * 16 + 16].split(b'\0')[0].decode('latin1') for i in range(ln)]
    data, indices, lists = raw[do:do + dn], raw[io:io + inn], raw[lio:lio + lin]
    seen = set()

    def node(i, depth=0):
        if i >= sn or depth > 32 or i in seen:
            raise SavePackError('invalid GFF struct reference')
        seen.add(i)
        kind, offset, count = struct.unpack_from('<3I', raw, so + i * 12)
        ids = [] if count == 0 else [offset] if count == 1 else list(struct.unpack_from(f'<{count}I', indices, offset))
        out = {'$type': kind}
        for f in ids:
            typ, label, value = struct.unpack_from('<3I', raw, fo + f * 12)
            if typ in (10, 12, 13):
                size = struct.unpack_from('<I', data, value)[0]
                v = data[value + 4:value + 4 + size]
            elif typ == 11:
                v = data[value + 1:value + 1 + data[value]]
            elif typ in (6, 7, 9):
                v = data[value:value + 8]
            elif typ in (16, 17):
                v = data[value:value + (16 if typ == 16 else 12)]
            elif typ == 14:
                v = node(value, depth + 1)
            elif typ == 15:
                n = struct.unpack_from('<I', lists, value)[0]
                v = [node(x, depth + 1) for x in struct.unpack_from(f'<{n}I', lists, value + 4)]
            else:
                v = value
            out[labels[label]] = (typ, v)
        seen.discard(i)
        return out
    return node(0)


def with_save_name(raw: bytes, name: str) -> bytes:
    """SAVENFO.res bytes with SAVEGAMENAME set to name; everything else parses the same.

    Adds the field when it is missing (the Auto Save has none). Raises
    SavePackError instead of returning a file that differs in any other field.
    """
    encoded = name.encode('cp1252')
    head = raw[:8]
    so, sn, fo, fn, lo, ln, do, dn, io, inn, lio, lin = struct.unpack_from('<12I', raw, 8)
    structs = bytearray(raw[so:so + sn * 12])
    fields = bytearray(raw[fo:fo + fn * 12])
    labels = bytearray(raw[lo:lo + ln * 16])
    data = bytearray(raw[do:do + dn])
    indices = bytearray(raw[io:io + inn])
    lists = bytes(raw[lio:lio + lin])
    names = [labels[i * 16:i * 16 + 16].split(b'\0')[0] for i in range(ln)]
    kind, offset, count = struct.unpack_from('<3I', structs, 0)
    ids = [] if count == 0 else [offset] if count == 1 else list(struct.unpack_from(f'<{count}I', indices, offset))
    target = next((f for f in ids if names[struct.unpack_from('<3I', fields, f * 12)[1]] == b'SAVEGAMENAME'), None)
    value = len(data)
    data += struct.pack('<I', len(encoded)) + encoded
    if target is not None:
        typ, label, _ = struct.unpack_from('<3I', fields, target * 12)
        if typ != 10:
            raise SavePackError('SAVEGAMENAME is not a text field')
        struct.pack_into('<3I', fields, target * 12, typ, label, value)
    else:
        if b'SAVEGAMENAME' in names:
            label = names.index(b'SAVEGAMENAME')
        else:
            label, ln = ln, ln + 1
            labels += b'SAVEGAMENAME'.ljust(16, b'\0')
        target, fn = fn, fn + 1
        fields += struct.pack('<3I', 10, label, value)
        ids.append(target)
        offset = target if len(ids) == 1 else len(indices)
        if len(ids) > 1:
            indices += struct.pack(f'<{len(ids)}I', *ids)
        struct.pack_into('<3I', structs, 0, kind, offset, len(ids))
    blocks = (bytes(structs), bytes(fields), bytes(labels), bytes(data), bytes(indices), lists)
    counts = (len(structs) // 12, len(fields) // 12, len(labels) // 16, len(data), len(indices), len(lists))
    header, position = bytearray(head), 56
    for block, size in zip(blocks, counts):
        header += struct.pack('<2I', position, size)
        position += len(block)
    rebuilt = bytes(header) + b''.join(blocks)
    before, after = gff_tree(raw), gff_tree(rebuilt)
    before.pop('SAVEGAMENAME', None)
    if after.pop('SAVEGAMENAME', None) != (10, encoded) or before != after:
        raise SavePackError('the renamed SAVENFO.res would change more than SAVEGAMENAME')
    return rebuilt


def save_info(raw: bytes) -> dict:
    """SAVEGAMENAME, AREANAME, LASTMODULE, TIMEPLAYED and PCAUTOSAVE from SAVENFO.res bytes."""
    tree = gff_tree(raw)
    out = {}
    for key in ('SAVEGAMENAME', 'AREANAME', 'LASTMODULE', 'TIMEPLAYED', 'PCAUTOSAVE'):
        if key in tree:
            typ, value = tree[key]
            out[key] = value.decode('cp1252', 'replace') if isinstance(value, bytes) else value
    return out


def _title(slot: Slot, info: dict) -> str:
    title = (info.get('SAVEGAMENAME') or '').strip()
    if slot.number == 0 or title == 'QUICKSAVE':
        return 'Quick Save'
    if title:
        return title
    if slot.number == 1 or info.get('PCAUTOSAVE') == 1:
        return 'Auto Save'
    area = (info.get('AREANAME') or '').rpartition(' - ')[2]
    return slot.folder.partition(' - ')[2] + (f' ({area})' if area else '')


# ---------------------------------------------------------------- manifest

def manifest_path(game) -> Path:
    return Path(game).expanduser() / MANIFEST_NAME


def load_manifest(game) -> dict:
    path = manifest_path(game)
    if not path.exists():
        return {'schema': 'stasis.speedrun-saves/v1', 'packs': {}, 'slots': {}}
    with open(path, 'rb') as stream:
        data = json.loads(stream.read(4 * 1024 * 1024))
    if data.get('schema') != 'stasis.speedrun-saves/v1' or not isinstance(data.get('slots'), dict):
        raise SavePackError(f'{path} is not a Stasis save manifest; leaving it alone.')
    return data


def _store_manifest(game, manifest):
    from kptas.durability import atomic_json
    atomic_json(manifest_path(game), manifest)


def _folder_files(folder: Path) -> dict | None:
    """{name: sha256} of a slot folder's regular files; None if it holds anything else."""
    out = {}
    for item in folder.iterdir():
        st = item.lstat()
        if not stat.S_ISREG(st.st_mode):
            return None
        out[item.name] = _file_sha(item)
    return out


def _matches(folder: Path, record: dict) -> bool:
    files = _folder_files(folder)
    return files == {name: row['sha256'] for name, row in record['files'].items()}


def save_folders(saves: Path) -> list[str]:
    """Slot folder names in the order the game lists them (byte order of the name)."""
    if not saves.is_dir():
        return []
    return sorted((p.name for p in saves.iterdir() if p.is_dir() and _slot_number(p.name) is not None),
                  key=lambda n: n.encode('latin1', 'replace'))


# ---------------------------------------------------------------- install / remove / list

def install(game, *, packs=PACKS, archives=None, rename=True, download=True, log=print) -> list[dict]:
    """Install every slot of the packs into game/Saves. Safe to run again."""
    game = Path(game).expanduser().resolve()
    saves = game / 'Saves'
    if not saves.is_dir():
        raise SavePackError(f'{saves} does not exist. Point --game at the folder that holds swkotor.exe.')
    manifest = load_manifest(game)
    results = []
    for item in packs:
        archive = ensure_archive(item, archives, download=download, log=log)
        manifest['packs'][item.key] = {'author': item.author, 'published_as': item.published_as,
                                       'version': item.version, 'category': item.category,
                                       'resource': item.resource, 'url': item.url, 'published': item.published,
                                       'archive': item.archive, 'archive_sha256': item.sha256}
        existing = {}
        for name in save_folders(saves):
            if not any(r['folder'] == name for r in manifest['slots'].values()):
                existing[name] = _folder_files(saves / name) or {}
        for slot in read_slots(item, archive):
            results.append(_install_slot(game, saves, manifest, item, slot, existing, rename, log))
    return results


def _install_slot(game, saves, manifest, item, slot, existing, rename, log):
    key = f'{item.key}/{slot.folder}'
    number = item.base + slot.number
    folder = f'{number:06d} - Game{number - 1}'
    target = saves / folder
    record = manifest['slots'].get(key)
    files = dict(slot.files)
    nfo_name = next(n for n in files if n.casefold() == 'savenfo.res')
    info = save_info(files[nfo_name])
    display = f'{item.label}: {_title(slot, info)}'[:NAME_LIMIT]
    row = {'key': key, 'folder': folder, 'name': display}
    if target.exists():
        if record and record['folder'] == folder and _matches(target, record):
            log(f'kept      {folder}  {record["name"]}')
            return {**row, 'name': record['name'], 'state': 'kept'}
        log(f'skipped   {folder}  this folder exists and was not installed by Stasis (or changed since). Left as is.')
        return {**row, 'state': 'skipped', 'reason': 'folder exists'}
    taken = [n for n in save_folders(saves) if _slot_number(n) == number]
    if taken:
        log(f'skipped   {folder}  slot number {number} is used by "{taken[0]}". Left as is.')
        return {**row, 'state': 'skipped', 'reason': f'number used by {taken[0]}'}
    renamed = False
    if rename:
        try:
            files[nfo_name] = with_save_name(files[nfo_name], display)
            renamed = True
        except (SavePackError, struct.error, UnicodeError) as exc:
            log(f'note      {folder}  kept the original in-game name: {exc}')
    if not renamed:
        display = info.get('SAVEGAMENAME') or _title(slot, info)
    core = ('savegame.sav', 'savenfo.res', 'partytable.res', 'globalvars.res')
    source = {name.casefold(): _sha(data) for name, data in slot.files if name.casefold() in core}
    same_as = next((name for name, have in existing.items()
                    if {n.casefold(): h for n, h in have.items() if n.casefold() in core} == source), None)
    staging = game / f'.stasis-saves-{uuid.uuid4().hex}'
    staging.mkdir()
    try:
        for name, data in files.items():
            with open(staging / name, 'xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if target.exists():
            raise SavePackError(f'{target} appeared during install')
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    record = {'pack': item.key, 'source_folder': slot.folder, 'source_title': info.get('SAVEGAMENAME'),
              'folder': folder, 'number': number, 'name': display, 'renamed': renamed,
              'area': info.get('AREANAME'), 'module': info.get('LASTMODULE'),
              'time_played': info.get('TIMEPLAYED'), 'autosave': info.get('PCAUTOSAVE') == 1,
              'same_as': same_as, 'installed_utc': _now(),
              'files': {name: {'source_sha256': _sha(dict(slot.files)[name]), 'sha256': _sha(data),
                               'bytes': len(data)} for name, data in files.items()}}
    manifest['slots'][key] = record
    _store_manifest(game, manifest)
    log(f'installed {folder}  {display}' + (f'  (same save as "{same_as}")' if same_as else ''))
    return {**row, 'name': display, 'state': 'installed'}


def remove(game, *, packs=None, log=print) -> list[dict]:
    """Delete installed slots that still match the manifest. Changed slots stay."""
    game = Path(game).expanduser().resolve()
    manifest = load_manifest(game)
    keys = {p.key for p in packs} if packs else None
    results = []
    for key, record in sorted(manifest['slots'].items()):
        if keys is not None and record['pack'] not in keys:
            continue
        target = game / 'Saves' / record['folder']
        if not target.exists():
            log(f'gone      {record["folder"]}  already removed')
            del manifest['slots'][key]
            results.append({'key': key, 'folder': record['folder'], 'state': 'gone'})
        elif _matches(target, record):
            for name in record['files']:
                (target / name).unlink()
            target.rmdir()
            del manifest['slots'][key]
            log(f'removed   {record["folder"]}  {record["name"]}')
            results.append({'key': key, 'folder': record['folder'], 'state': 'removed'})
        else:
            log(f'kept      {record["folder"]}  its files changed after install, so it stays.')
            results.append({'key': key, 'folder': record['folder'], 'state': 'changed'})
        _store_manifest(game, manifest)
    return results


def listing(game) -> list[dict]:
    """The game's save slots in list order, with the in-game name and the pack that installed it."""
    game = Path(game).expanduser()
    saves = game / 'Saves'
    try:
        installed = {r['folder']: r for r in load_manifest(game)['slots'].values()}
    except (OSError, ValueError):
        installed = {}
    rows = []
    for index, folder in enumerate(save_folders(saves), 1):
        name = ''
        nfo = next((p for p in (saves / folder).iterdir() if p.name.casefold() == 'savenfo.res'), None)
        if nfo is not None:
            try:
                info = save_info(nfo.read_bytes())
                name = _title(Slot(folder, _slot_number(folder), ()), info)
            except (SavePackError, struct.error, OSError):
                name = '(unreadable SAVENFO.res)'
        record = installed.get(folder)
        rows.append({'row': index, 'folder': folder, 'name': name,
                     'pack': record['pack'] if record else '', 'source_folder': record['source_folder'] if record else ''})
    return rows
