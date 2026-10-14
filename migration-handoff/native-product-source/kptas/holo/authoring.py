"""Resolve the durable authored score without changing a holorecord's genesis.

A live take's SCOR predates appended inputs. Only its committed immutable score
version is executable; live.score and interrupted candidate text are not authority.
"""
from __future__ import annotations
import hashlib
import json
import os
import stat
import re
from pathlib import Path
from kptas.types import FormatError


def _read(path: Path, limit: int) -> bytes:
    # Open without waiting for a FIFO writer; check the opened descriptor, not
    # a raceable pre-open stat. Metadata and score versions must be real files.
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FormatError(f'{path}: authoring data must be a regular file')
        with os.fdopen(fd,'rb') as f:
            fd=None
            data=f.read(limit+1)
    finally:
        if fd is not None:os.close(fd)
    if len(data)>limit:raise FormatError(f'{path}: authoring metadata exceeds size limit')
    return data


def resolved_score(bundle) -> str:
    """Return verified text, or the embedded SCOR for an ordinary recording.

    Bundle.open remains available for forensic inspection when authoring is
    incomplete. Calling this resolver refuses such execution without writing data.
    """
    root=bundle.root
    manifest_path=root.with_name(root.stem.split('@',1)[0]+'.session.json')
    if not os.path.lexists(manifest_path) and not any((root/name).exists() for name in ('live.score','live.pending.json')) and not next(root.glob('authoring-*.score'),None):
        return bundle.chain.score
    try:
        manifest_data=_read(manifest_path,1024*1024);manifest=json.loads(manifest_data)
        if manifest.get('format')!='telos-live/1':raise ValueError('unknown live session format')
        takes=[t for t in manifest['takes'] if Path(t['path']).resolve()==root.resolve()]
        if len(takes)!=1:raise ValueError('session does not identify this take uniquely')
        take=takes[0];digest=take.get('score_sha256')
        if not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None:
            raise ValueError('invalid authored score digest')
        filename='authoring-'+digest+'.score'
        if take.get('score_file')!=filename or take.get('score_sha256')!=digest:
            raise ValueError('manifest authored score does not match committed version')
        version=root/filename
        if version.is_symlink():raise ValueError('authored score must be a local regular file')
        data=_read(version,4*1024*1024)
        if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('authored score hash mismatch')
        marker_data=_read(root/'durable.json',1024*1024);marker=json.loads(marker_data)
        tick=marker['tick']
        if (type(tick) is not int or marker.get('format')!='kptas-durable/1'
                or marker.get('bundle_id')!=bundle.bundle_id or tick!=max(bundle.last_tick,bundle.chain.tick_floor)
                or any(type(v) is not int or v!=tick for v in
                       (take.get('last'),take.get('durable_tick')))):
            raise ValueError('authored score durable boundary mismatch')
        rec=bundle.record(tick)
        if rec.state_root.hex()!=marker['state_root'] or rec.epoch!=marker['epoch']:
            raise ValueError('authored score durable state mismatch')
        # Check the exact acknowledged journal prefix, including its chain link.
        from kptas.holo.journal import Scanner,ChainState
        chain=ChainState();matched=False
        with (root/'journal').open('rb') as f:
            for _,kind,payload,end in Scanner(f):
                chain.feed(kind,payload)
                if end==marker['journal_bytes']:
                    boundary=chain.last_tick==tick
                    if marker.get('inherited') is True:
                        boundary=chain.last_tick<0 and chain.header.parent is not None and chain.header.parent.tick==tick
                    matched=boundary and chain.link.hex()==marker['chain_link'] and chain.bundle_id==bundle.bundle_id
                    break
                if end>marker['journal_bytes']:break
        if not matched:raise ValueError('authored score durable journal mismatch')
        text=data.decode('utf-8')
        if re.search(r'^\s*@include\b',text,re.MULTILINE):raise ValueError('authored score must contain expanded includes')
        from kptas.score import compile_score
        score=compile_score(text);embedded=compile_score(bundle.chain.score)
        if score.header!=embedded.header:raise ValueError('authored score changes the embedded header')
        from kptas.score.fmt import render
        if ([render(item) for item in score.items[:len(embedded.items)]]
                != [render(item) for item in embedded.items]):
            raise ValueError('authored score changes the embedded prefix')
        # Reject a concurrent publication instead of mixing two committed versions.
        if (_read(manifest_path,1024*1024)!=manifest_data
                or _read(root/'durable.json',1024*1024)!=marker_data):
            raise ValueError('live authoring changed while reading')
        return text
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise FormatError(f'{root}: cannot use live authored score: {exc}') from exc
