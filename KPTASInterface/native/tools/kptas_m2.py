#!/usr/bin/env python3
"""M2/M3 driver for the KPTASInterface native shim (standard library only).

Launches K1 GOG 1.03 under Wine with native/shim/build/dinput8.dll (shim with
the kst savestate engine), attaches the staging ring, captures every tick,
plays a pinned step list, and writes the per-tick journal plus a side log with
each tick's state root and capture numbers.

  run      launch, capture every tick, play the steps; optionally pin trees at
           target ticks, then restore to each target and continue, comparing
           every continued tick's journal line and state root with the record
  compare  compare two runs: journals and per-tick state roots; page-level
           diff at the first differing root (from the page logs)

The ring consumer keeps a mirror of the live page map (vaddr -> key, prot,
class), page contents only for keys the mirror or a pinned tree references,
and a page log per run (every PAGE record's tick, vaddr, class, key, prot,
flags). A restore set W is every page whose mirror entry differs from the
pinned tree(T): DATA/ZERO records for pages T had, DECOMMIT for pages T did
not have. It is written in the staging-record format and passed by path.

Every run starts cold from pinned inputs: the stage's writable dirs and
swkotor.ini are reset from the pin directory, the prefix's registry files are
reset from prefix-pin, and the game gets a fixed environment (env -i).
Paths come from KPTAS_LIVE (default ~/kptas-live): stage, pin, runs and
session under $KPTAS_LIVE/m2, the prefix at $KPTAS_LIVE/prefix.
"""

import argparse
import base64
import getpass
import hashlib
import json
import mmap
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                    # KPTASInterface
LIVE = Path(os.environ.get("KPTAS_LIVE", Path.home() / "kptas-live")) / "m2"
DEFAULTS = {
    "stage": LIVE / "stage",
    "pin": LIVE / "pin",
    "prefix": LIVE.parent / "prefix",
    "prefix_pin": LIVE / "prefix-pin",
    "session": LIVE / "session",
    "dll": ROOT / "native" / "shim" / "build" / "dinput8.dll",
    "steps": LIVE / "scores" / "m1_exit.steps.json",
}
FT_2004_02_12 = 127210176000000000
WRITABLE = ("Saves", "Temp", "currentgame", "gameinprogress", "logs", "Tempclient", "LocalVault", "DMVault",
            "ServerVault")
ZERO_KEY = hashlib.blake2s(bytes(4096), digest_size=16).digest()
REC_PAGE, REC_TICK_END, REC_WRAP, REC_JOURNAL = 1, 2, 3, 4
F_DATA, F_ZERO, F_DECOMMIT, F_SWEEP = 0x1, 0x2, 0x4, 0x8
TF_RESTORE = 0x400                            # TICK_END of a batch written by a restore
CLASS = {1: "game_arena", 2: "image_data", 3: "harness_game"}


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def winpath(p: Path) -> str:
    return "Z:" + str(p).replace("/", "\\")


# ---------------------------------------------------------------- state root (engine section 8.6)

def state_root(mirror: dict) -> bytes:
    leaves = {}
    for va, (key, prot, mclass) in mirror.items():
        vpn = va >> 12
        leaves.setdefault(vpn >> 10, []).append((vpn, key, prot, mclass))
    top = hashlib.sha256(b"KPRT")
    for hi in sorted(leaves):
        s = hashlib.sha256(b"KPLF" + struct.pack("<H", hi & 0xFFFF))
        for vpn, key, prot, mclass in sorted(leaves[hi]):
            s.update(struct.pack("<H", vpn & 0x3FF) + key + bytes([mclass, 0]) + struct.pack("<H", prot))
        top.update(struct.pack("<H", hi & 0xFFFF) + s.digest())
    return top.digest()


# ---------------------------------------------------------------- staging ring

class Ring:
    HDR = 4096

    def __init__(self, path: Path, mb: int):
        cap = mb << 20
        with open(path, "wb") as f:
            f.truncate(self.HDR + cap)
            hdr = bytearray(self.HDR)
            hdr[0:4] = b"KPSR"
            struct.pack_into("<HHII", hdr, 4, 1, 8, self.HDR, cap)
            f.seek(0)
            f.write(hdr)
        self.f = open(path, "r+b")
        self.m = mmap.mmap(self.f.fileno(), self.HDR + cap)
        self.cap = cap
        self.rpos = 0
        struct.pack_into("<I", self.m, 44, os.getpid())
        struct.pack_into("<I", self.m, 48, 1)

    def wpos(self) -> int:
        while True:
            a = struct.unpack_from("<Q", self.m, 16)[0]
            b = struct.unpack_from("<Q", self.m, 16)[0]
            if a == b:
                return a

    def records(self):
        """Yield (kind, mclass, flags, prot, vaddr, tick, hash, data) for everything written so far."""
        w = self.wpos()
        m, cap, base = self.m, self.cap, self.HDR
        while self.rpos < w:
            off = self.rpos % cap
            rem = cap - off
            if rem < 40:
                self.rpos += rem
                continue
            magic, kind, mclass, flags, prot, vaddr, dlen, tick = struct.unpack_from("<2sBBHHIIQ", m, base + off)
            if magic != b"SR":
                raise RuntimeError(f"ring: bad magic at pos {self.rpos}")
            if kind == REC_WRAP:
                self.rpos += rem
                continue
            h = bytes(m[base + off + 24: base + off + 40])
            data = bytes(m[base + off + 40: base + off + 40 + dlen]) if dlen else b""
            self.rpos += 40 + ((dlen + 7) & ~7)
            yield kind, mclass, flags, prot, vaddr, tick, h, data
        struct.pack_into("<Q", m, 24, self.rpos)


class Store:
    """Mirror of the live page map, pinned trees and the contents they need."""

    def __init__(self, pagelog: Path | None, targets: set, windows: dict, win_content_ticks: int):
        self.mirror = {}          # vaddr -> (key, prot, mclass)
        self.content = {}         # key -> bytes
        self.refs = {}            # key -> count
        self.pinned = {}          # tick -> mirror copy
        self.targets = targets
        self.windows = windows    # tick -> window start T (ticks T+1..T+K after each target)
        self.win_content_ticks = win_content_ticks
        self.win_batches = {}     # tick -> {vaddr: (key, prot, mclass) or None}, recorded run
        self.win_content = {}     # key -> bytes for pages of recorded window batches
        self.last_tick = -1
        self.batches = 0
        self.restore_batches = []  # (tick, npages)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.pagelog = open(pagelog, "wb") if pagelog else None
        self.cont_diffs = {}      # tick -> [(vaddr, orig entry, continued entry, orig bytes, continued bytes)]
        self.recording = True     # pins are taken only while the original run is recorded
        self.error = None

    def _ref(self, key, data):
        if key == ZERO_KEY:
            return
        n = self.refs.get(key, 0)
        if n == 0 and data is not None:
            self.content[key] = data
        self.refs[key] = n + 1

    def _unref(self, key):
        if key == ZERO_KEY:
            return
        n = self.refs.get(key, 0) - 1
        if n <= 0:
            self.refs.pop(key, None)
            self.content.pop(key, None)
        else:
            self.refs[key] = n

    def apply_batch(self, tick, pages, restore):
        with self.lock:
            for (mclass, flags, prot, vaddr, key, data) in pages:
                old = self.mirror.get(vaddr)
                if flags & F_DECOMMIT:
                    if old:
                        self._unref(old[0])
                        del self.mirror[vaddr]
                    continue
                if (flags & F_DATA) and key not in self.content and self.refs.get(key, 0) and key != ZERO_KEY:
                    self.content[key] = data
                self._ref(key, data if flags & F_DATA else None)
                if old:
                    self._unref(old[0])
                self.mirror[vaddr] = (key, prot, mclass)
            if self.pagelog:
                for (mclass, flags, prot, vaddr, key, data) in pages:
                    self.pagelog.write(struct.pack("<QIBBHH16s", tick, vaddr, mclass, 1 if restore else 0, prot,
                                                   flags, key))
            if self.recording and not restore and tick in self.targets:
                snap = dict(self.mirror)
                for (key, prot, mclass) in snap.values():
                    self._ref(key, None)
                self.pinned[tick] = snap
            if not restore and tick in self.windows:
                cur = {}
                datas = {}
                for (mclass, flags, prot, vaddr, key, data) in pages:
                    cur[vaddr] = None if flags & F_DECOMMIT else (key, prot, mclass)
                    if flags & F_DATA:
                        datas[key] = data
                if self.recording:
                    self.win_batches[tick] = cur
                    if tick - self.windows[tick] <= self.win_content_ticks:
                        self.win_content.update(datas)
                elif tick in self.win_batches and tick not in self.cont_diffs:
                    orig = self.win_batches[tick]
                    diff = sorted(va for va in set(orig) | set(cur) if orig.get(va, "absent") != cur.get(va, "absent"))
                    if diff:
                        out = []
                        for va in diff[:64]:
                            o, c = orig.get(va, "absent"), cur.get(va, "absent")
                            ob = self.win_content.get(o[0]) if isinstance(o, tuple) else None
                            cb = datas.get(c[0]) if isinstance(c, tuple) else None
                            out.append((va, o, c, ob, cb))
                        self.cont_diffs[tick] = (len(diff), out)
            self.last_tick = tick
            self.batches += 1
            if restore:
                self.restore_batches.append((tick, len(pages)))
            self.cond.notify_all()

    def wait_tick(self, tick, restore_count=None, timeout=600):
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                if self.error:
                    raise RuntimeError(f"consumer failed: {self.error}")
                ok = self.last_tick == tick and (restore_count is None or len(self.restore_batches) >= restore_count)
                if ok:
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"consumer did not reach tick {tick} (at {self.last_tick})")
                self.cond.wait(min(left, 1.0))


def consumer(ring: Ring, store: Store, stop: threading.Event, verify: bool):
    """Batches: PAGE*, JOURNAL, TICK_END. A TICK_END with TF_RESTORE closes a restore batch."""
    pages = []
    batch = hashlib.blake2s(digest_size=16)
    try:
        while not stop.is_set():
            got = False
            for kind, mclass, flags, prot, vaddr, tick, h, data in ring.records():
                got = True
                if kind == REC_PAGE:
                    if verify and (flags & F_DATA) and hashlib.blake2s(data, digest_size=16).digest() != h:
                        raise RuntimeError(f"page hash mismatch at {vaddr:#x} tick {tick}")
                    pages.append((mclass, flags, prot, vaddr, h, data if flags & F_DATA else None))
                    batch.update(bytes([REC_PAGE]) + struct.pack("<I", vaddr) + h)
                elif kind == REC_JOURNAL:
                    batch.update(bytes([REC_JOURNAL]) + struct.pack("<I", 0) + h)
                elif kind == REC_TICK_END:
                    if verify and batch.digest() != h:
                        raise RuntimeError(f"TICK_END digest mismatch at tick {tick}")
                    restore = bool(flags & TF_RESTORE)
                    store.apply_batch(tick, pages, restore)
                    pages = []
                    batch = hashlib.blake2s(digest_size=16)
            if not got:
                time.sleep(0.002)
    except Exception as e:                    # surfaced to the driver through wait_tick
        with store.lock:
            store.error = repr(e)
            store.cond.notify_all()


def write_restore_set(path: Path, store: Store, tick: int) -> dict:
    """W = pages whose live entry differs from tree(T)."""
    tree = store.pinned[tick]
    n = {"data": 0, "zero": 0, "decommit": 0}
    with store.lock, open(path, "wb") as f:
        live = store.mirror
        for va in sorted(set(tree) | set(live)):
            t, l = tree.get(va), live.get(va)
            if t == l:
                continue
            if t is None:
                mclass = l[2]
                f.write(struct.pack("<2sBBHHIIQ16s", b"SR", REC_PAGE, mclass, F_DECOMMIT, 0, va, 0, tick, bytes(16)))
                n["decommit"] += 1
                continue
            key, prot, mclass = t
            if key == ZERO_KEY:
                f.write(struct.pack("<2sBBHHIIQ16s", b"SR", REC_PAGE, mclass, F_ZERO, prot, va, 0, tick, key))
                n["zero"] += 1
            else:
                data = store.content.get(key)
                if data is None:
                    raise RuntimeError(f"no content for {va:#x} key {key.hex()} at tick {tick}")
                f.write(struct.pack("<2sBBHHIIQ16s", b"SR", REC_PAGE, mclass, F_DATA, prot, va, 4096, tick, key))
                f.write(data)
                n["data"] += 1
    n["pages"] = n["data"] + n["zero"] + n["decommit"]
    return n


# ---------------------------------------------------------------- stage and launch

def reset_stage(a) -> None:
    stage, pin = a.stage, a.pin
    shutil.copyfile(pin / "swkotor.ini", stage / "swkotor.ini")
    for d in WRITABLE:
        p = stage / d
        if p.is_symlink():
            raise SystemExit(f"refusing to touch symlinked {p}")
        if p.exists():
            shutil.rmtree(p)
    shutil.copytree(pin / "saves", stage / "Saves")
    (stage / "logs").mkdir()
    shutil.copyfile(a.dll, stage / "dinput8.dll")
    for reg in ("system.reg", "user.reg", "userdef.reg"):
        shutil.copyfile(a.prefix_pin / reg, a.prefix / reg)


def snap_files(stage: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for d in WRITABLE:
        p = stage / d
        if p.exists() and not p.is_symlink():
            shutil.copytree(p, dst / d)
    shutil.copy2(stage / "swkotor.ini", dst / "swkotor.ini")


def restore_files(stage: Path, src: Path) -> None:
    for d in WRITABLE:
        p = stage / d
        if p.is_symlink():
            raise SystemExit(f"refusing to touch symlinked {p}")
        if p.exists():
            shutil.rmtree(p)
        if (src / d).exists():
            shutil.copytree(src / d, p)
    shutil.copy2(src / "swkotor.ini", stage / "swkotor.ini")


def game_env(a) -> dict:
    clock = (f"mode=virtual;hz={a.hz};qpf_hz={a.qpf};uptime0_ms={a.uptime0};"
             f"initial_ft={FT_2004_02_12}")
    return {
        "HOME": str(Path.home()), "USER": getpass.getuser(), "LANG": "C", "PATH": "/usr/bin:/bin",
        "DISPLAY": a.display, "WINEPREFIX": str(a.prefix), "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": "dinput8=n,b;winepulse.drv=d;winealsa.drv=d;wineoss.drv=d",
        "KPTAS_PORT": str(a.port), "KPTAS_CLOCK": clock, "KPTAS_SESSION": winpath(a.session),
        "KPTAS_PRESENT": a.present,
    }


def launch(a) -> subprocess.Popen:
    env = game_env(a)
    envs = " ".join(f"{k}='{v}'" for k, v in sorted(env.items()))
    cmd = f"exec env -i {envs} setarch x86_64 -R wine swkotor.exe > '{a.session}/game.out' 2>&1 < /dev/null"
    return subprocess.Popen(["host-spawn", "-no-pty", "-cwd", str(a.stage), "sh", "-c", cmd],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_wine(prefix: Path) -> None:
    subprocess.run(["host-spawn", "-no-pty", "sh", "-c", f"WINEPREFIX='{prefix}' wineserver -k"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


# ---------------------------------------------------------------- wire

class Wire:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.buf = b""
        self.next_id = 1
        self.on_tick = None

    def _line(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no line from the shim")
            self.conn.settimeout(left)
            chunk = self.conn.recv(1 << 20)
            if not chunk:
                raise ConnectionError("shim closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, verb: str, args: dict | None = None, timeout: float = 900.0) -> dict:
        rid = self.next_id
        self.next_id += 1
        self.conn.sendall((canon({"id": rid, "verb": verb, "args": args or {}}) + "\n").encode())
        while True:
            msg = self._line(timeout)
            if "push" in msg:
                if msg["push"] == "tick" and self.on_tick:
                    self.on_tick(msg)
                continue
            if msg.get("id") != rid:
                raise RuntimeError(f"unexpected reply {msg}")
            if not msg.get("ok"):
                raise RuntimeError(f"{verb} failed: {msg.get('error')}")
            return msg["result"]


# ---------------------------------------------------------------- run

def plan_steps(steps: list, cut: set) -> list:
    """Segments (first_tick, last_tick, args, first): the original notes, split at every cut tick."""
    segs = []
    t = 0
    for s in steps:
        a0 = {k: v for k, v in s.items() if k != "n"}
        start, end = t + 1, t + s["n"]
        pieces = sorted(c for c in cut if start <= c < end)
        lo = start
        for c in pieces + [end]:
            segs.append((lo, c, a0, lo == start))
            lo = c + 1
        t = end
    return segs


def cmd_run(a) -> int:
    steps = json.loads(Path(a.steps).read_text())
    targets = sorted(int(x) for x in a.targets.split(",")) if a.targets else []
    cont = a.continue_ticks
    windows = {}
    for T in targets:
        for t in range(T + 1, T + cont + 1):
            windows.setdefault(t, T)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    a.session.mkdir(parents=True, exist_ok=True)
    for f in ("kptas_shim.log", "game.out"):
        (a.session / f).unlink(missing_ok=True)
    kill_wine(a.prefix)
    reset_stage(a)
    ring_path = out.with_suffix(".ring")
    ring = Ring(ring_path, a.ring_mb)
    store = Store(out.with_suffix(".pages") if a.page_log else None, set(targets), windows, a.win_content)
    stop = threading.Event()
    th = threading.Thread(target=consumer, args=(ring, store, stop, a.verify_ring), daemon=True)
    th.start()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.port))
    srv.listen(1)
    srv.settimeout(a.connect_timeout)
    t0 = time.monotonic()
    proc = launch(a)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        print("shim did not connect", file=sys.stderr)
        kill_wine(a.prefix)
        return 2
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    w = Wire(conn)
    rec = {}                                  # tick -> (entry line, root)
    side = open(str(out) + ".side", "w")
    jf = open(out, "w")
    cur = {"mode": "record", "cmp": None}

    def on_tick(msg):
        t = msg["tick"]
        entry = canon(msg["entry"])
        root = msg.get("root")
        info = {k: v for k, v in msg.items() if k not in ("entry", "push")}
        if cur["mode"] == "record":
            rec[t] = (entry, root)
            jf.write(entry + "\n")
            side.write(canon(info) + "\n")
        elif cur["mode"] == "record-off":
            info["mode"] = "off"
            side.write(canon(info) + "\n")
        else:
            c = cur["cmp"]
            orig = rec.get(t)
            c["ticks"] += 1
            info["mode"] = cur["mode"]
            side.write(canon(info) + "\n")
            if orig is None:
                c["missing"] += 1
                return
            je, re_ = orig[0] == entry, orig[1] == root
            c["journal_eq"] += je
            c["root_eq"] += re_
            if (not je or not re_) and c["first_bad"] is None:
                ea, eb = json.loads(orig[0]), json.loads(entry)
                c["first_bad"] = {"tick": t, "journal_equal": je, "root_equal": re_,
                                  "fields": diff_obj(ea, eb)[:12], "orig_root": orig[1], "root": root}
    w.on_tick = on_tick
    rc = 0
    report = {"targets": targets, "continue": cont, "restores": []}
    try:
        hello = w.call("hello", {"proto": "kptas-wire/1"}, timeout=60)
        (a.session / "hello.json").write_text(json.dumps(hello, indent=1, sort_keys=True))
        print(f"hello: shim={hello['shim']} caps={hello['caps']} hooks={len(hello['hooks'])}")
        if a.tier_probe:
            probe = {}
            for verb in ("mem.write", "semantic.apply"):
                try:
                    w.call(verb, {"addr": 0x78d000, "data_b64": "AA==", "fields": {}})
                    probe[verb] = "accepted"
                except RuntimeError as e:
                    probe[verb] = str(e)
            print(f"tier probe (unlocked session): {probe}")
            report["tier_probe_unlocked"] = probe
        w.call("tier.lock", {"tier": a.tier})
        if a.tier_probe and a.tier == "tas":
            try:
                w.call("mem.write", {"addr": 0x78d000, "data_b64": "AA=="})
                report["tier_probe_tas"] = "accepted"
            except RuntimeError as e:
                report["tier_probe_tas"] = str(e)
            print(f"tier probe (tas): mem.write -> {report['tier_probe_tas']}")
        w.call("clock", {"mode": "virtual", "hz": a.hz, "qpf_hz": a.qpf, "uptime0_ms": a.uptime0,
                         "initial_ft": FT_2004_02_12, "initial_realtime": "2004-02-12T00:00:00Z"})
        print("ring.attach:", w.call("ring.attach", {"path": winpath(ring_path), "mb": a.ring_mb}))
        w.call("capture", {"enabled": True})
        w.call("subscribe", {"topics": ["tick"]})
        r = w.call("start", {}, timeout=a.boot_timeout)
        print(f"start: tick {r['tick']} root={r['state_root'][:16] if r['state_root'] else None} "
              f"boot {time.monotonic() - t0:.1f}s")
        if a.tier_probe and a.tier == "lab":
            cur_bytes = w.call("mem.read", {"addr": 0x78d000, "len": 4})["data_b64"]
            r2 = w.call("mem.write", {"addr": 0x78d000, "data_b64": cur_bytes})   # same bytes: state unchanged
            report["tier_probe_lab"] = r2
            print(f"tier probe (lab): mem.write of the current bytes at 0x78d000 -> {r2}")
        segs = plan_steps(steps, set(targets) | {a.stop_at} if a.stop_at else set(targets))
        if a.stop_at:
            segs = [s for s in segs if s[0] <= a.stop_at]
        files_dir = Path(str(out) + ".files")
        for (lo, hi, args, first) in segs:
            q = dict(args)
            q["n"] = hi - lo + 1
            q["first"] = first
            w.call("step", q)
            if hi in targets:
                snap_files(a.stage, files_dir / str(hi))
                store.wait_tick(hi)
                print(f"  pinned tick {hi}: root={rec[hi][1][:16]} pages={len(store.pinned[hi])} "
                      f"contents={len(store.content)}")
            if a.stop_at and hi >= a.stop_at:
                break
        last = max(rec)
        store.wait_tick(last)
        v = w.call("verify", {})
        mroot = state_root(store.mirror).hex()
        print(f"record done at tick {last}: audit ok={v['ok']} pages={v['pages']} cap_us_mean={v['cap_us_mean']} "
              f"max={v['cap_us_max']} emitted_mean={v['emitted_mean']} arena_hw={v['arena_hw'] >> 20}MB; "
              f"python mirror root {'==' if mroot == rec[last][1] else '!='} shim root")
        report["record_verify"] = v
        report["mirror_root_ok"] = mroot == rec[last][1]
        store.recording = False
        for T in targets:
            if T not in store.pinned:
                print(f"  target {T} not pinned; skipped")
                continue
            now = max(store.last_tick, 0)
            store.wait_tick(now)
            store.cont_diffs.clear()
            rs_path = out.parent / f"{out.stem}.restore-{T}.bin"
            wset = write_restore_set(rs_path, store, T)
            restore_files(a.stage, files_dir / str(T))
            nrb = len(store.restore_batches)
            t1 = time.monotonic()
            rr = w.call("restore", {"tick": T, "root": rec[T][1], "path": winpath(rs_path)})
            store.wait_tick(T, restore_count=nrb + 1)
            mroot = state_root(store.mirror).hex()
            print(f"  restore {now} -> {T}: W={wset} verified={rr.get('verified')} root_ok={rr.get('root_ok')} "
                  f"written={rr.get('pages')} committed={rr.get('committed')} decommitted={rr.get('decommitted')} "
                  f"audit={rr.get('audit')} us={rr.get('us')} wall={time.monotonic() - t1:.2f}s "
                  f"mirror_root_ok={mroot == rec[T][1]}")
            cmpst = {"ticks": 0, "journal_eq": 0, "root_eq": 0, "missing": 0, "first_bad": None}
            cur["mode"], cur["cmp"] = f"continue@{T}", cmpst
            end = T + cont
            for (lo, hi, args, first) in plan_steps(steps, {T, end}):
                if hi <= T or lo > end:
                    continue
                q = dict(args)
                q["n"] = hi - lo + 1
                q["first"] = first
                w.call("step", q)
                if cmpst["first_bad"] and cmpst["ticks"] > (cmpst["first_bad"]["tick"] - T) + 5:
                    break
            store.wait_tick(T + cmpst["ticks"])   # the last continued tick the shim pushed
            cur["mode"] = "record-off"
            item = {"from": now, "to": T, "W": wset, "reply": rr, "compare": cmpst,
                    "page_diffs": {str(k): {"n": v[0], "first": [hex(x[0]) for x in v[1][:40]]}
                                   for k, v in sorted(store.cont_diffs.items())[:5]}}
            report["restores"].append(item)
            fb = cmpst["first_bad"]
            print(f"  continue {T + 1}..{T + cmpst['ticks']}: journal_eq={cmpst['journal_eq']}/{cmpst['ticks']} "
                  f"root_eq={cmpst['root_eq']}/{cmpst['ticks']} first_bad={fb}")
            for k, (n, lst) in sorted(store.cont_diffs.items())[:2]:
                print(f"    tick {k}: {n} pages differ from the recorded batch: {[hex(x[0]) for x in lst[:16]]}")
                show_page_diff(lst[:a.show_bytes])
            store.cont_diffs.clear()
            cur["mode"] = "record-off"
        if a.stop_at is None or targets:
            v = w.call("verify", {})
            report["final_verify"] = v
        w.call("bye", {"exit": 0}, timeout=30)
    except (ConnectionError, TimeoutError, RuntimeError, OSError) as e:
        print(f"run ended early at tick {max(rec) if rec else -1}: {e}", file=sys.stderr)
        report["error"] = str(e)
        rc = 1
    jf.close()
    side.close()
    stop.set()
    conn.close()
    srv.close()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    kill_wine(a.prefix)
    th.join(timeout=10)
    if store.pagelog:
        store.pagelog.close()
    try:
        ring.m.close()
        ring.f.close()
        if not a.keep_ring:
            ring_path.unlink()
    except OSError:
        pass
    data = out.read_bytes()
    report["journal_sha256"] = hashlib.sha256(data).hexdigest()
    report["ticks"] = data.count(b"\n")
    report["wall_s"] = round(time.monotonic() - t0, 1)
    Path(str(out) + ".report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    print(f"journal: {out} ticks={report['ticks']} sha256={report['journal_sha256']} wall={report['wall_s']}s")
    return rc


def show_page_diff(lst: list) -> None:
    for (va, o, c, ob, cb) in lst:
        def desc(e):
            return e if isinstance(e, str) else ("decommit" if e is None else e[0].hex()[:8] + f"/prot{e[1]:x}")
        cls = next((CLASS.get(e[2]) for e in (o, c) if isinstance(e, tuple)), "?")
        if isinstance(o, tuple) and o[0] == ZERO_KEY:
            ob = bytes(4096)
        if isinstance(c, tuple) and c[0] == ZERO_KEY:
            cb = bytes(4096)
        if ob is None or cb is None:
            print(f"      {va:#010x} {cls}: recorded={desc(o)} continued={desc(c)}")
            continue
        offs = [i for i in range(0, 4096, 4) if ob[i:i + 4] != cb[i:i + 4]]
        print(f"      {va:#010x} {cls} {len(offs)} words differ (recorded/continued): " +
              " ".join(f"+{i:03x}:{ob[i:i + 4][::-1].hex()}/{cb[i:i + 4][::-1].hex()}" for i in offs[:10]))


# ---------------------------------------------------------------- compare

def diff_obj(x, y, path=""):
    if type(x) is not type(y):
        return [path or "."]
    if isinstance(x, dict):
        out = []
        for k in sorted(set(x) | set(y)):
            if k not in x or k not in y:
                out.append(f"{path}.{k}")
            else:
                out += diff_obj(x[k], y[k], f"{path}.{k}")
        return out
    if isinstance(x, list):
        if len(x) != len(y):
            return [f"{path}[len {len(x)}!={len(y)}]"]
        out = []
        for i, (p, q) in enumerate(zip(x, y)):
            out += diff_obj(p, q, f"{path}[{i}]")
        return out
    return [] if x == y else [path]


def load_pages(path: Path, upto: int) -> dict:
    m = {}
    rs = struct.Struct("<QIBBHH16s")
    with open(path, "rb") as f:
        while True:
            b = f.read(rs.size)
            if len(b) < rs.size:
                break
            tick, va, mclass, restore, prot, flags, key = rs.unpack(b)
            if restore:
                continue
            if tick > upto:
                break
            if flags & F_DECOMMIT:
                m.pop(va, None)
            else:
                m[va] = (key, prot, mclass)
    return m


def cmd_compare(a) -> int:
    la = Path(a.a).read_text().splitlines()
    lb = Path(a.b).read_text().splitlines()
    sa = [json.loads(l) for l in Path(a.a + ".side").read_text().splitlines()]
    sb = [json.loads(l) for l in Path(a.b + ".side").read_text().splitlines()]
    n = min(len(la), len(lb))
    jbad = next((i for i in range(n) if la[i] != lb[i]), None)
    ra = {s["tick"]: s.get("root") for s in sa if "mode" not in s}
    rb = {s["tick"]: s.get("root") for s in sb if "mode" not in s}
    common = sorted(set(ra) & set(rb))
    rbad = [t for t in common if ra[t] != rb[t]]
    print(f"journals: {n} common ticks, first differing line: {jbad}"
          + (f" fields {diff_obj(json.loads(la[jbad]), json.loads(lb[jbad]))[:12]}" if jbad is not None else ""))
    print(f"roots: {len(common)} common ticks, {len(common) - len(rbad)} equal, first differing tick: "
          f"{rbad[0] if rbad else None}")
    if rbad and a.pages:
        t = rbad[0]
        pa = load_pages(Path(a.a).with_suffix(".pages"), t)
        pb = load_pages(Path(a.b).with_suffix(".pages"), t)
        diff = sorted(va for va in set(pa) | set(pb) if pa.get(va) != pb.get(va))
        by = {}
        for va in diff:
            c = (pa.get(va) or pb.get(va))[2]
            by.setdefault(CLASS.get(c, c), []).append(va)
        print(f"  tick {t}: {len(diff)} pages differ: " +
              ", ".join(f"{k}: {len(v)} (first {[hex(x) for x in v[:8]]})" for k, v in by.items()))
    return 0 if jbad is None and not rbad and len(la) == len(lb) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True, help="journal output path (side log, page log, report beside it)")
    r.add_argument("--targets", default="", help="comma list of ticks to pin, restore to and continue from")
    r.add_argument("--continue-ticks", type=int, default=600)
    r.add_argument("--win-content", type=int, default=120,
                   help="keep recorded page bytes for the first N ticks after each target (divergence byte diffs)")
    r.add_argument("--show-bytes", type=int, default=6, help="pages per divergent tick to show word diffs for")
    r.add_argument("--stop-at", type=int, default=None, help="end the recorded run at this tick")
    r.add_argument("--ring-mb", type=int, default=64)
    r.add_argument("--page-log", action="store_true", help="write every PAGE record's key to <out>.pages")
    r.add_argument("--verify-ring", action="store_true", help="re-hash every DATA page and TICK_END digest")
    r.add_argument("--keep-ring", action="store_true")
    r.add_argument("--tier-probe", action="store_true",
                   help="send mem.write/semantic.apply unlocked, then mem.write under the locked tier")
    r.add_argument("--tier", choices=["tas", "lab"], default="tas")
    r.add_argument("--port", type=int, default=47391)
    r.add_argument("--display", default=":171")
    r.add_argument("--hz", type=int, default=60)
    r.add_argument("--qpf", type=int, default=10_000_000)
    r.add_argument("--uptime0", type=int, default=3_600_000)
    r.add_argument("--present", default="skip")
    r.add_argument("--connect-timeout", type=float, default=90)
    r.add_argument("--boot-timeout", type=float, default=600)
    for k, v in DEFAULTS.items():
        r.add_argument(f"--{k.replace('_', '-')}", dest=k, type=Path, default=v)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--pages", action="store_true", help="page-level diff at the first differing root")
    a = p.parse_args()
    return {"run": cmd_run, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
