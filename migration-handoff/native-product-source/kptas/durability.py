"""Explicit disk barriers and separate authoring autosave; no native/game calls.

Callbacks run on the caller thread. Call checkpoint only on the serialized bundle
writer. An editor timer may call save_draft with an immutable copied snapshot.
Directory fsync failures are errors, not silently downgraded durability claims.
"""
from __future__ import annotations
import hashlib,json,os,time,uuid,threading
import stat
from collections import deque
from pathlib import Path
from typing import Callable,Any


from kptas.platform_sync import sync_dir, require_directory_barrier, capability



def _lock_nb(fd,shared=False):
    """Non-blocking flock; on native Windows (no fcntl) an msvcrt byte lock, which is always exclusive."""
    try:
        import fcntl
    except ImportError:
        import msvcrt
        msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        return
    fcntl.flock(fd,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)

def atomic_json(path: Path, value: Any, barrier: Callable[[str],None]=lambda _:None) -> None:
    """Publish only after content is durable; failed temporary files are retained."""
    require_directory_barrier()
    path=Path(path);tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    data=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()+b'\n'
    with tmp.open('xb') as f:
        f.write(data);f.flush();barrier('before_file_fsync');os.fsync(f.fileno());barrier('after_file_fsync')
    os.replace(tmp,path);barrier('after_replace');sync_dir(path.parent);barrier('after_directory_fsync')


# Ancestors' files change only by replacement; a file whose identity, size and both
# timestamps match what this writer fsynced earlier has had no write since. Stamps
# newer than _RACY_NS are never recorded: a coarse-timestamp filesystem could hide a
# later same-size write inside the same tick.
_RACY_NS=3_000_000_000
_LEDGER_ATTR='_kptas_fsynced_ancestors'


class _Ledger(dict):
    """Writer-owned {file id: stat fingerprint} of fsynced immutable ancestor files."""
    skipped=0;synced=0


def _ledger(bundle: Any) -> _Ledger:
    ledger=getattr(bundle,_LEDGER_ATTR,None)
    if not isinstance(ledger,_Ledger):
        ledger=_Ledger()
        try:setattr(bundle,_LEDGER_ATTR,ledger)
        except AttributeError:pass          # no slot: a throwaway ledger fsyncs everything
    return ledger


def _fsync_unless_recorded(ledger: _Ledger | None,name: str,fd: int,sync: Callable[[],None] | None=None) -> None:
    """fsync fd unless ledger holds its current fingerprint; record only after a successful fsync."""
    if ledger is None:
        os.fsync(fd);return
    st=os.fstat(fd)
    key=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    if ledger.get(name)==key:
        ledger.skipped+=1;return
    ledger.pop(name,None)
    (sync or (lambda:os.fsync(fd)))()
    ledger.synced+=1
    if time.time_ns()-max(st.st_mtime_ns,st.st_ctime_ns)>=_RACY_NS:ledger[name]=key


def restore_point_at_or_before(bundle: Any, tick: int) -> tuple[int,bytes,int] | None:
    """(tick, state_root, epoch) of the latest captured, unevicted restore point <= tick on bundle's
    ancestry, or None. With capture cadence most ticks have no root; this is the tick a resume reaches."""
    from kptas.types import TickFlag,NULL_DIGEST
    want=TickFlag.CAPTURED|TickFlag.RESTORE_POINT
    b,limit=bundle,tick
    while b is not None:
        for r in reversed(b.rows):
            if r.tick>limit or (r.flags & want)!=want or r.flags & TickFlag.EVICTED:continue
            rec=b.record(r.tick)
            if rec.state_root!=NULL_DIGEST:return r.tick,rec.state_root,rec.epoch
        limit=min(limit,b.chain.tick_floor);b=b.parent
    return None


def checkpoint(bundle: Any, *, barrier: Callable[[str],None]=lambda _:None) -> dict[str,Any]:
    """Flush executed data and publish an honest durable boundary; does not verify native restoration."""
    from kptas.types import TickFlag,NULL_DIGEST
    from kptas.holo.pages import AREA_DIRS,list_packs,pack_path
    require_directory_barrier()
    bundle._check_writable()
    tick=bundle.chain.last_tick
    inherited=tick<0 and bundle.header.parent is not None
    if inherited:tick=bundle.header.parent.tick
    if tick<0:raise ValueError('no executed tick to checkpoint')
    rec=bundle.record(tick)
    bundle._flush_pending();barrier('pending_flushed')
    bundle.store.fsync();barrier('payload_fsynced')
    # A child can reference payloads in every ancestor and spill directory. Flush
    # those immutable dependencies before publishing the child's durable marker.
    # Serialized writer ownership is required; no concurrent ancestor mutation.
    # Ancestor files this writer already fsynced and that still carry the same
    # (dev, ino, size, mtime, ctime) are not fsynced again; the writer's own files
    # and every directory fence are repeated on every call (a timestamp-keyed
    # directory cache was rejected: directory-fence-cache-review). Any failure
    # discards the ledger so the next attempt fences everything again.
    ledger=_ledger(bundle)
    try:
        dirs=set();ancestors=[];current=bundle;seen=set()
        while current is not None:
            identity=current.root.resolve(strict=True)
            if identity in seen:raise ValueError('cyclic bundle ancestry')
            seen.add(identity);ancestors.append(current)
            dirs.update((current.root,current.root.parent,identity,identity.parent))
            current.store.fsync()
            immutable=current is not bundle
            for area,pack in list_packs(current.root):
                path=pack_path(current.root,area,pack)
                target=path.resolve(strict=True)
                # Reopened ancestor stores have no dirty flag; explicitly sync the
                # referenced files rather than assuming a previous process did so.
                with path.open('rb') as f:_fsync_unless_recorded(ledger if immutable else None,str(target),f.fileno())
                dirs.update((path.parent,target.parent,target.parent.parent))
            meta=current.root/'meta.json'
            if meta.exists():
                with meta.open('rb') as f:_fsync_unless_recorded(ledger if immutable else None,str(meta),f.fileno())
            current=current.parent
        # Persist newly created containing directories as well as leaf entries.
        for d in tuple(dirs):dirs.update(d.parents)
        for d in sorted(dirs,key=lambda p:len(p.parts),reverse=True):sync_dir(d)
        barrier('payload_directories_fsynced')
        # Ancestor genesis/header/SCOR and parent-path metadata precede child journal.
        for current in reversed(ancestors):
            if current is bundle:current.journal.fsync()
            else:
                # Same guard and flush journal.fsync() performs, before any ledger skip.
                j=current.journal
                if j.write_failure is not None:raise ValueError('journal writer failed; close and recover before syncing')
                j.f.flush()
                _fsync_unless_recorded(ledger,'journal:'+str(current.root.resolve(strict=True)),j.f.fileno(),j.fsync)
        barrier('journal_fsynced')
    except BaseException:
        ledger.clear();raise
    result={'format':'kptas-durable/1','bundle_id':bundle.bundle_id,'tick':tick,'epoch':rec.epoch,
            'state_root':rec.state_root.hex(),'chain_link':bundle.chain.link.hex(),
            'journal_bytes':os.fstat(bundle.journal.f.fileno()).st_size,
            'captured_restore_point':bool(rec.flags & TickFlag.CAPTURED and rec.flags & TickFlag.RESTORE_POINT
                                          and not rec.flags & TickFlag.EVICTED and rec.state_root!=NULL_DIGEST),
            'native_verified':False,'inherited':inherited,'frames':'separate fence required'}
    # Capture cadence: `tick` may have no root (an exit mid-step, a refused step, a forced close). A resume
    # lands on restore_tick, the latest captured restore point <= tick; ticks after it replay from there.
    rp=restore_point_at_or_before(bundle,tick)
    result.update(restore_tick=rp[0] if rp else None,restore_root=rp[1].hex() if rp else None,
                  restore_epoch=rp[2] if rp else None)
    atomic_json(bundle.root/'durable.json',result,lambda phase:barrier('watermark_'+phase))
    barrier('complete')
    return result


class DraftAutosave:
    """Wall-clock deadline for copied editor state; poll from the editor's idle loop.

    No background Bundle access. Updating or polling does not execute game input.
    The caller schedules poll even while paused; default deadline is15seconds.
    """
    def __init__(self,directory: Path,interval: float=15.0,clock: Callable[[],float]=time.monotonic):
        require_directory_barrier()
        if interval<=0:raise ValueError('positive autosave interval required')
        self.root=Path(directory);self.root.mkdir(parents=True,exist_ok=True)
        for directory in (self.root,*self.root.parents):sync_dir(directory)
        self.interval,self.clock=interval,clock;self.deadline=clock()+interval
        self.snapshot=None;self.saved_digest=None
        self._lock=threading.RLock();self._stop=threading.Event();self._thread=None;self.notifications=deque()
        self._event_boundary=None

    def update(self,score: str,branch: str,cursor: dict[str,Any]) -> None:
        # Roundtrip captures nested cursor values; UI cannot mutate a pending save in place.
        with self._lock:
            self.snapshot=json.loads(json.dumps({'format':'kptas-authoring/1','score':score,'branch':branch,'cursor':cursor}))

    def event(self,kind: str,details: dict[str,Any]) -> None:
        # The timer and editor share one append stream. Lock the entire write,
        # flush and barrier sequence, including failures.
        with self._lock:
            data=json.dumps({'kind':kind,'details':details},sort_keys=True,separators=(',',':')).encode()
            if len(data)>16*1024*1024:raise ValueError('event exceeds recovery bound')
            frame=len(data).to_bytes(4,'little')+data+hashlib.sha256(data).digest()
            path=self.root/'events.log'
            require_directory_barrier()
            fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_APPEND|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('authoring event log must be a regular file')
                _lock_nb(fd)
                with os.fdopen(fd,'a+b') as f:
                    fd=None
                    before=_event_file_identity(f)
                    if before!=self._event_boundary:
                        prefix=_read_event_stream(f,collect=False)
                        if prefix['torn_bytes']:
                            raise ValueError(f"authoring event log has a torn tail at byte {prefix['valid_bytes']}; preserve and inspect it before further appends")
                    # O_APPEND keeps placement atomic; the lock covers prefix
                    # admission, append and both durable publication barriers.
                    self._event_boundary=None
                    f.write(frame);f.flush();os.fsync(f.fileno())
                    sync_dir(self.root)
                    self._event_boundary=_event_file_identity(f)
            finally:
                if fd is not None:os.close(fd)

    def start(self) -> None:
        """Start wall-clock draft saves even while no game ticks or editor actions occur."""
        if self._thread is not None:raise RuntimeError('autosave already started')
        def run():
            while not self._stop.wait(min(self.interval,1.0)):
                try:
                    event=self.poll()
                    if event is not None:self.notifications.append(event)
                except Exception as exc:
                    self.notifications.append({'event':'save-error','error':repr(exc)})
        self._thread=threading.Thread(target=run,name='authoring-autosave',daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:self._thread.join()
        event=self.poll(force=True)
        if event is not None:self.notifications.append(event)

    def poll(self,force: bool=False) -> dict[str,Any] | None:
        with self._lock:return self._poll(force)

    def _poll(self,force: bool=False) -> dict[str,Any] | None:
        if self.snapshot is None or (not force and self.clock()<self.deadline):return None
        self.deadline=self.clock()+self.interval
        digest=hashlib.sha256(json.dumps(self.snapshot,sort_keys=True).encode()).hexdigest()
        if digest==self.saved_digest:return None
        try:atomic_json(self.root/'authoring.json',self.snapshot)
        except Exception as exc:
            # Surface the original exception even if error-log storage itself also fails.
            try:self.event('save-error',{'error':repr(exc)})
            except Exception:pass
            raise
        self.saved_digest=digest
        return {'event':'draft-saved','sha256':digest,'branch':self.snapshot['branch'],
                'cursor':self.snapshot['cursor'],'executed_state_saved':False}


def _event_file_identity(stream):
    info=os.fstat(stream.fileno())
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _read_event_stream(stream,*,collect=True):
    """Bounded frames; append admission scans without retaining the whole log."""
    events=[];offset=0;stream.seek(0)
    while True:
        header=stream.read(4)
        if not header:return {'events':events,'valid_bytes':offset,'torn_bytes':0}
        if len(header)!=4:return {'events':events,'valid_bytes':offset,'torn_bytes':len(header)}
        size=int.from_bytes(header,'little')
        if size>16*1024*1024:raise ValueError('event length exceeds recovery bound')
        body=stream.read(size+32)
        if len(body)!=size+32:return {'events':events,'valid_bytes':offset,'torn_bytes':4+len(body)}
        data,digest=body[:size],body[size:]
        if hashlib.sha256(data).digest()!=digest:raise ValueError('event checksum mismatch')
        event=json.loads(data)
        if collect:events.append(event)
        offset+=4+size+32


def read_events(path: Path) -> dict[str,Any]:
    """Read verified events without repairing or truncating the original log.

    A partial final frame is reported separately. A complete bad frame is an
    error, not a recoverable tail, so corruption cannot hide accepted events.
    """
    fd=os.open(Path(path),os.O_RDONLY|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('authoring event log must be a regular file')
        if os.name=='posix':
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        with os.fdopen(fd,'rb') as f:
            fd=None
            return _read_event_stream(f)
    finally:
        if fd is not None:os.close(fd)


def inspect_checkpoint(root: Path) -> dict[str,Any]:
    """Validate the last published state without truncating any original tail.

    This is an offline recovery inspection, not native restore admission. Hold a
    shared journal lock so a live writer cannot race the inspection. Recovery
    continuation needs a separately copied verified-prefix source. Existing
    create_branch/seek can write parent metadata or receipts; do not point them
    directly at this preserved original. Copy recovery is not implemented here.
    """
    require_directory_barrier()
    from kptas.holo.bundle import Bundle
    from kptas.holo.journal import Scanner,ChainState
    from kptas.types import TickFlag,NULL_DIGEST
    from kptas.filetree import validate_rows
    root=Path(root)
    with (root/'journal').open('rb') as f:
        _lock_nb(f.fileno(),shared=True)
        marker=json.loads((root/'durable.json').read_text())
        if marker.get('format')!='kptas-durable/1':raise ValueError('unknown checkpoint format')
        chain=ChainState();matched=False
        for _,kind,payload,end in Scanner(f):
            chain.feed(kind,payload)
            if end==marker['journal_bytes']:
                inherited=marker.get('inherited',False)
                boundary=(chain.last_tick==marker['tick'] and chain.last_epoch==marker['epoch'])
                if inherited is True:
                    boundary=(chain.last_tick<0 and chain.header.parent is not None
                              and chain.header.parent.tick==marker['tick'])
                elif inherited is not False:
                    boundary=False
                matched=(chain.link.hex()==marker['chain_link'] and chain.bundle_id==marker['bundle_id'] and boundary)
                break
            if end>marker['journal_bytes']:break
        if not matched:raise ValueError('durable marker journal prefix mismatch')
        with Bundle.open(root) as b:
            rec=b.record(marker['tick'])
            if rec.state_root.hex()!=marker['state_root'] or rec.epoch!=marker['epoch']:
                raise ValueError('durable marker state mismatch')
            captured=bool(rec.flags & TickFlag.CAPTURED and rec.flags & TickFlag.RESTORE_POINT
                          and not rec.flags & TickFlag.EVICTED and rec.state_root!=NULL_DIGEST)
            if marker.get('captured_restore_point') is not captured:
                raise ValueError('durable marker capture flags mismatch')
            if 'restore_tick' in marker:
                rp=restore_point_at_or_before(b,marker['tick'])
                if ((rp[0],rp[1].hex(),rp[2]) if rp else (None,None,None))!=(
                        marker['restore_tick'],marker.get('restore_root'),marker.get('restore_epoch')):
                    raise ValueError('durable marker restore point mismatch')
            count=0
            if captured:
                m=b.map_at(marker['tick'])
                if m.root()!=rec.state_root:raise ValueError('durable map root mismatch')
                for leaf in m.leaves.values():
                    for ref in leaf.values():b.page(ref.key);count+=1
            file_count=0
            if rec.flags & TickFlag.FILES:
                if rec.files_root==NULL_DIGEST:
                    raise ValueError('checkpoint files flag has no files root')
                _,payloads=validate_rows(b.file_tree(rec.files_root),lambda h:b.file(bytes.fromhex(h)))
                file_count=len(payloads)
            return {**marker,'page_keys_checked':count,'file_blobs_checked':file_count,'source_unchanged':True,
                    'tail_bytes_after_checkpoint':os.fstat(f.fileno()).st_size-marker['journal_bytes'],
                    'torn_tail_bytes':b.torn_bytes,'native_verified':False,
                    'recovery_action':'inspect only; recover through a separately copied verified-prefix source, not the preserved original'}
