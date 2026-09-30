#!/usr/bin/env python3
"""M1 driver for the KPTASInterface native shim (standard library only).

Launches K1 GOG 1.03 under Wine with native/shim/build/dinput8.dll, listens
for the shim on loopback, locks tier tas, sets the clock, steps a score's
notes, and writes the per-tick journal.

  run      launch, play a score, write the journal (one canonical JSON line per tick)
  compare  compare two journals: first divergent tick and the fields that differ

Every run starts cold from the same pinned inputs: the stage's swkotor.ini,
Saves, Temp, currentgame and logs are reset from the pin directory, the game
gets a fixed environment (env -i), the same port, cwd and command line.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                    # KPTASInterface
sys.path.insert(0, str(ROOT))

LIVE = Path(os.environ.get("KPTAS_LIVE", Path.home() / "kptas-live")) / "m1"
DEFAULTS = {
    "stage": LIVE / "stage",
    "pin": LIVE / "pin",
    "prefix": LIVE.parent / "prefix",
    "session": LIVE / "session",
    "dll": ROOT / "native" / "shim" / "build" / "dinput8.dll",
}
DATA_VA, DATA_END = 0x0078D000, 0x00836000
FT_2004_02_12 = 127210176000000000          # 2004-02-12T00:00:00Z as FILETIME


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def winpath(p: Path) -> str:
    return "Z:" + str(p).replace("/", "\\")


# ---------------------------------------------------------------- stage and launch

def reset_stage(stage: Path, pin: Path, dll: Path) -> None:
    shutil.copyfile(pin / "swkotor.ini", stage / "swkotor.ini")
    for d in ("Saves", "Temp", "currentgame", "logs", "Tempclient", "LocalVault", "DMVault", "ServerVault"):
        p = stage / d
        if p.is_symlink():
            raise SystemExit(f"refusing to touch symlinked {p}")
        if p.exists():
            shutil.rmtree(p)
    shutil.copytree(pin / "saves", stage / "Saves")
    (stage / "logs").mkdir()
    shutil.copyfile(dll, stage / "dinput8.dll")


def game_env(a) -> dict:
    clock = (f"mode=virtual;hz={a.hz};qpf_hz={a.qpf};uptime0_ms={a.uptime0};"
             f"initial_ft={FT_2004_02_12}")
    env = {
        # The environment block reaches game state (it changed .data and rand counts), so it is pinned.
        "HOME": str(Path.home()), "USER": getpass.getuser(), "LANG": "C", "PATH": "/usr/bin:/bin",
        "DISPLAY": a.display, "WINEPREFIX": str(a.prefix), "WINEDEBUG": "-all",
        "WINEDLLOVERRIDES": "dinput8=n,b;winepulse.drv=d;winealsa.drv=d;wineoss.drv=d",
        "KPTAS_PORT": str(a.port), "KPTAS_CLOCK": clock, "KPTAS_SESSION": winpath(a.session),
    }
    if a.watchdog:
        env["KPTAS_WATCHDOG"] = str(a.watchdog)
    if a.watchdog_ctx:
        env["KPTAS_WATCHDOG_CTX"] = "1"
    if a.context:
        env["KPTAS_CONTEXT"] = a.context
    env["KPTAS_PRESENT"] = a.present
    if a.profile:
        env["KPTAS_PROFILE"] = str(a.profile)
    return env


def launch(a) -> subprocess.Popen:
    env = game_env(a)
    envs = " ".join(f"{k}='{v}'" for k, v in sorted(env.items()))
    wrap = "setarch x86_64 -R " if a.wrapper == "setarch_R" else ""
    cmd = f"exec env -i {envs} {wrap}wine swkotor.exe > '{a.session}/game.out' 2>&1 < /dev/null"
    return subprocess.Popen(["host-spawn", "-no-pty", "-cwd", str(a.stage), "sh", "-c", cmd],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_wine(prefix: Path) -> None:
    subprocess.run(["host-spawn", "-no-pty", "sh", "-c", f"WINEPREFIX='{prefix}' wineserver -k"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


# ---------------------------------------------------------------- wire

class Wire:
    def __init__(self, conn: socket.socket, journal, sidelog):
        self.conn = conn
        self.buf = b""
        self.next_id = 1
        self.journal = journal
        self.sidelog = sidelog
        self.ticks = 0
        self.last_entry = None
        self.page_every = 0
        self.want_pages = False

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
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"bad line from shim ({e}): {line[:600]!r}") from None

    def call(self, verb: str, args: dict | None = None, timeout: float = 600.0) -> dict:
        r = self._call(verb, args, timeout)
        if self.want_pages and verb in ("step", "wait"):
            self.want_pages = False
            pages = self._call("digest", {"regions": ["data.pages"]}, 60)["data.pages"]
            self.sidelog.write(canon({"tick": self.ticks - 1, "pages": pages}) + "\n")
        return r

    def _call(self, verb: str, args: dict | None = None, timeout: float = 600.0) -> dict:
        rid = self.next_id
        self.next_id += 1
        self.conn.sendall((canon({"id": rid, "verb": verb, "args": args or {}}) + "\n").encode())
        while True:
            msg = self._line(timeout)
            if "push" in msg:
                self._push(msg)
                continue
            if msg.get("id") != rid:
                raise RuntimeError(f"unexpected reply {msg}")
            if not msg.get("ok"):
                raise RuntimeError(f"{verb} failed: {msg.get('error')}")
            return msg["result"]

    def _push(self, msg: dict) -> None:
        if msg["push"] == "tick":
            entry = msg["entry"]
            self.journal.write(canon(entry) + "\n")
            self.sidelog.write(canon({k: v for k, v in msg.items() if k not in ("entry", "push")}) + "\n")
            self.ticks = msg["tick"] + 1
            self.last_entry = entry
            if self.page_every and msg["tick"] % self.page_every == 0:
                self.want_pages = True


# ---------------------------------------------------------------- score

def note_args(note) -> dict:
    from kptas.score import keys as K
    from kptas.score.fmt import render
    keys = sorted({K.dik(k) for tok in note.held for k in K.token_keys(tok)})
    text = render(note).split(None, 1)
    args = {"note": text[1] if len(text) > 1 else ".", "keys": keys, "buttons": list(note.buttons)}
    mouse = {}
    if note.mouse_abs is not None:
        mouse["abs"] = list(note.mouse_abs)
    if note.mouse_rel is not None:
        mouse["rel"] = list(note.mouse_rel)
    if mouse:
        args["mouse"] = mouse
    if note.wheel:
        args["wheel"] = note.wheel
    return args


def play(w: Wire, score, a, marks: dict) -> None:
    from kptas.score import Mark, Note, Segment, Sync
    for item in score.items:
        if isinstance(item, Note):
            if item.count == 0:
                continue
            args = note_args(item)
            args["n"] = item.count
            w.call("step", args)
            maybe_shot(w, a)
            if a.dump_at is not None and w.ticks - 1 >= a.dump_at and not a._dumped:
                dump(w, a)
        elif isinstance(item, Sync):
            r = w.call("wait", {"event": item.event, "arg": item.arg,
                                "timeout": item.timeout or 1800, "note": "."})
            print(f"  @sync {item.event}: waited={r.get('waited')} tick={r['tick']}")
        elif isinstance(item, Segment):
            if item.hz and item.hz != a.hz:
                w.call("clock", {"mode": "virtual", "hz": item.hz, "qpf_hz": a.qpf,
                                 "uptime0_ms": a.uptime0, "initial_ft": FT_2004_02_12})
        elif isinstance(item, Mark):
            marks[item.name] = w.ticks - 1
        else:
            print(f"  skipped unsupported item: {item}")


def read_spec(w: Wire, spec: str) -> str:
    """'ADDR[>OFF...]:LEN': each '>OFF' dereferences a u32 and adds OFF."""
    chain, n = spec.rsplit(":", 1)
    parts = [int(x, 0) for x in chain.split(">")]
    addr = parts[0]
    for off in parts[1:]:
        ptr = int.from_bytes(base64.b64decode(w.call("mem.read", {"addr": addr, "len": 4})["data_b64"]), "little")
        addr = ptr + off
    try:
        return base64.b64decode(w.call("mem.read", {"addr": addr, "len": int(n, 0)})["data_b64"]).hex()
    except RuntimeError as e:
        return f"unreadable at {addr:#x} ({e})"


def maybe_shot(w: Wire, a) -> None:
    """Grab the X display once the run passes each --shot tick (needs --present != skip)."""
    while a.shot and w.ticks - 1 >= a.shot[0]:
        t = a.shot.pop(0)
        png = Path(a.session) / f"shot-{w.ticks - 1:06d}.png"
        subprocess.run(["host-spawn", "-no-pty", "ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab",
                        "-video_size", "1024x768", "-i", a.display, "-frames:v", "1", str(png)],
                       stdin=subprocess.DEVNULL, timeout=60)
        print(f"  shot at tick {w.ticks - 1} (asked {t}): {png}")


def dump(w: Wire, a, out: Path | None = None) -> None:
    """Snapshot .data and the page digests at the current gate, for bisecting."""
    if out is None:
        a._dumped = True
        out = Path(a.dump_dir or a.session)
    out.mkdir(parents=True, exist_ok=True)
    tick = w.ticks - 1
    pages = w.call("digest", {"regions": ["data.pages"]})["data.pages"]
    raw = bytearray()
    for addr in range(DATA_VA, DATA_END, 0x10000):
        n = min(0x10000, DATA_END - addr)
        raw += base64.b64decode(w.call("mem.read", {"addr": addr, "len": n})["data_b64"])
    (out / f"data-{tick}.bin").write_bytes(raw)
    (out / f"pages-{tick}.json").write_text(canon(pages) + "\n")
    print(f"  dumped .data at tick {tick} to {out}")


# ---------------------------------------------------------------- commands

def cmd_run(a) -> int:
    from kptas.score import compile_score
    a._dumped = False
    score = compile_score(a.score, include_dirs=[ROOT / "lib"]) if a.score else None
    a.session.mkdir(parents=True, exist_ok=True)
    for f in ("kptas_shim.log", "game.out"):
        (a.session / f).unlink(missing_ok=True)
    kill_wine(a.prefix)
    reset_stage(a.stage, a.pin, a.dll)

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
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    marks = {}
    rc = 0
    with open(out, "w") as jf, open(str(out) + ".side", "w") as sf:
        w = Wire(conn, jf, sf)
        try:
            hello = w.call("hello", {"proto": "kptas-wire/1"}, timeout=60)
        except Exception:
            kill_wine(a.prefix)
            raise
        print(f"hello: shim={hello['shim']} text_sha256={hello['text_sha256'][:16]} "
              f"seed_hooked={hello['seed_hooked']} context={hello['caps']['context']} hooks={len(hello['hooks'])}")
        (a.session / "hello.json").write_text(json.dumps(hello, indent=1, sort_keys=True))
        w.call("tier.lock", {"tier": "tas"})
        w.call("clock", {"mode": "virtual", "hz": a.hz, "qpf_hz": a.qpf, "uptime0_ms": a.uptime0,
                         "initial_ft": FT_2004_02_12, "initial_realtime": "2004-02-12T00:00:00Z"})
        w.call("subscribe", {"topics": ["tick"]})
        if a.start_delay:
            time.sleep(a.start_delay)   # diagnostics: lets a watchdog fire during CRT startup
        try:
            r = w.call("start", {}, timeout=a.boot_timeout)
            print(f"start: tick {r['tick']} subticks={r['subticks']} boot {time.monotonic() - t0:.1f}s")
            dump(w, a, Path(str(out) + ".dump"))   # tick-0 .data, so a one-off divergence can be diffed later
            w.page_every = 600
            if score is not None:
                play(w, score, a, marks)
            while w.ticks - 1 < a.min_ticks:
                n = min(600, a.min_ticks - (w.ticks - 1))
                w.call("step", {"n": n, "note": "."})
                maybe_shot(w, a)
                if a.dump_at is not None and w.ticks - 1 >= a.dump_at and not a._dumped:
                    dump(w, a)
            if a.dump_at is not None and not a._dumped:
                dump(w, a)
            for spec in a.read:
                print(f"  read {spec}: {read_spec(w, spec)}")
            w.call("bye", {"exit": 0}, timeout=30)
        except (ConnectionError, TimeoutError, RuntimeError, OSError) as e:
            print(f"run ended early at tick {w.ticks - 1}: {e}", file=sys.stderr)
            rc = 1
        jf.flush()
    conn.close()
    srv.close()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    kill_wine(a.prefix)
    data = out.read_bytes()
    print(f"journal: {out} ticks={data.count(b'\n')} sha256={hashlib.sha256(data).hexdigest()} "
          f"wall={time.monotonic() - t0:.1f}s marks={marks}")
    return rc


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


def cmd_compare(a) -> int:
    la = Path(a.a).read_text().splitlines()
    lb = Path(a.b).read_text().splitlines()
    n = min(len(la), len(lb))
    for i in range(n):
        if la[i] != lb[i]:
            ea, eb = json.loads(la[i]), json.loads(lb[i])
            fields = diff_obj(ea, eb)
            print(f"first divergence at line {i} (tick {ea.get('tick')}): {fields}")
            for f in fields[:12]:
                print(f"  {f}")
            return 1
    if len(la) != len(lb):
        print(f"identical for {n} ticks; lengths differ ({len(la)} vs {len(lb)})")
        return 1
    print(f"identical: {n} ticks, sha256 {hashlib.sha256(Path(a.a).read_bytes()).hexdigest()}")
    return 0


def cmd_datadiff(a) -> int:
    """Byte ranges that differ between two .data dumps (from run --dump-at)."""
    x, y = Path(a.a).read_bytes(), Path(a.b).read_bytes()
    runs, start = [], None
    for i in range(min(len(x), len(y))):
        if x[i] != y[i]:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, min(len(x), len(y))))
    for s0, e0 in runs[:64]:
        print(f"  {DATA_VA + s0:#010x}+{e0 - s0}: {x[s0:e0][:16].hex()} vs {y[s0:e0][:16].hex()}")
    print(f"{len(runs)} differing ranges, {sum(e - s for s, e in runs)} bytes")
    return 1 if runs else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--score", help="score file (kptas.score); omit to only step idle ticks")
    r.add_argument("--out", required=True, help="journal output path")
    r.add_argument("--min-ticks", type=int, default=0, help="step idle notes until this tick")
    r.add_argument("--port", type=int, default=47317)
    r.add_argument("--display", default=":147")
    r.add_argument("--hz", type=int, default=60)
    r.add_argument("--qpf", type=int, default=10_000_000)
    r.add_argument("--uptime0", type=int, default=3_600_000)
    r.add_argument("--context", choices=["fiber", "inline"], default=None)
    r.add_argument("--watchdog", type=int, default=0, help="seconds without a gate before logging a stall (diagnostics; 0 = off)")
    r.add_argument("--wrapper", choices=["none", "setarch_R"], default="setarch_R",
                   help='launch_wrapper: "setarch_R" disables host ASLR for the Wine process tree')
    r.add_argument("--present", default="skip", help='"skip", "draw" or N (present every Nth tick); output only')
    r.add_argument("--watchdog-ctx", action="store_true",
                   help="on a stall, suspend the main thread and log its context (perturbs .data)")
    r.add_argument("--start-delay", type=float, default=0, help="seconds to wait before start (diagnostics)")
    r.add_argument("--profile", type=int, default=0, help="sample the main thread every N ms (diagnostics)")
    r.add_argument("--dump-at", type=int, default=None, help="dump .data at the first gate >= this tick")
    r.add_argument("--dump-dir", default=None)
    r.add_argument("--shot", type=lambda v: sorted(int(x) for x in v.split(",")), default=[],
                   help="comma list of ticks to screenshot (use with --present N)")
    r.add_argument("--read", action="append", default=[], help="addr:len to mem.read before bye")
    r.add_argument("--connect-timeout", type=float, default=90)
    r.add_argument("--boot-timeout", type=float, default=300)
    for k, v in DEFAULTS.items():
        r.add_argument(f"--{k}", type=Path, default=v)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    d = sub.add_parser("datadiff")
    d.add_argument("a")
    d.add_argument("b")
    a = p.parse_args()
    return {"run": cmd_run, "compare": cmd_compare, "datadiff": cmd_datadiff}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
