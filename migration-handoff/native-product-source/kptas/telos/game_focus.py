"""Explicit UI execution focus for a locally launched, attested X11 game.

This is a controlled local-owner check, not a hostile-X-server guarantee.
Frame-ring producer IDs are Windows IDs and must never be used as /proc PIDs.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
import re

from kptas.types import BackendError


def process_stat(pid, proc=Path('/proc')):
    fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise BackendError('game focus owner is no longer live')
    return int(fields[1]), fields[19]


@dataclass(frozen=True)
class LaunchAnchor:
    pid: int
    starttime: str
    exe: Path
    display: str
    prefix: str
    session: str
    nonce: str


def launch_anchor(pid, cfg, env):
    game = cfg.get('game', {})
    display = env.get('DISPLAY', '')
    prefix = env.get('WINEPREFIX', '')
    if not display or not prefix or not env.get('KPTAS_SESSION') or not env.get('KPTAS_FOCUS_NONCE'):
        raise BackendError('UI game focus needs an explicit local display, Wine prefix and session')
    exe = (Path(os.path.expanduser(game['dir'])) / game.get('exe', 'swkotor.exe')).resolve(strict=True)
    return LaunchAnchor(pid, process_stat(pid)[1], exe, display,
                        prefix, env['KPTAS_SESSION'], env['KPTAS_FOCUS_NONCE'])


class GameFocus:
    def __init__(self, anchor, *, proc=Path('/proc'), run=subprocess.run):
        self.anchor, self.proc, self.run = anchor, proc, run
        self.binding = None

    def xdo(self, *args):
        return self.command('xdotool', *map(str, args))

    def command(self, *args):
        result = self.run(list(args), capture_output=True,
                          timeout=.75, check=False, env={**os.environ, 'DISPLAY': self.anchor.display})
        if result.returncode:
            raise BackendError('game focus X11 request failed')
        return result.stdout.decode().strip()

    def eligible_window(self, wid, size):
        geometry = dict(line.split('=', 1) for line in self.xdo('getwindowgeometry', '--shell', wid).splitlines() if '=' in line)
        if (int(geometry.get('WIDTH', 0)), int(geometry.get('HEIGHT', 0))) != size:
            return False
        tree = self.command('xwininfo', '-id', str(wid), '-tree')
        root = re.search(r'Root window id:\s*(0x[0-9a-fA-F]+)', tree)
        parent = re.search(r'Parent window id:\s*(0x[0-9a-fA-F]+)', tree)
        if root is None or parent is None:
            raise BackendError('game focus cannot qualify the X11 window tree')
        if int(root[1], 16) != int(parent[1], 16):
            # A window manager may reparent a real top-level client into its
            # decoration frame. Its client inventory distinguishes that from
            # the game's nested render child on the same PID.
            clients = self.command('xprop', '-root', '_NET_CLIENT_LIST')
            if wid not in {int(value, 16) for value in re.findall(r'0x[0-9a-fA-F]+', clients)}:
                return False
        classes = re.findall(r'"([^"\n]*)"', self.command('xprop', '-id', str(wid), 'WM_CLASS'))
        return self.anchor.exe.name.casefold() in {name.casefold() for name in classes}

    def owned_processes(self):
        """Correlate reparented Wine children using an owner-only launch nonce.

        A nonce is correlation evidence under the controlled same-user host
        assumption, not a security boundary against another same-user process.
        Executable/environment/window/lifetime evidence is still required.
        """
        found = set()
        marker = b'KPTAS_FOCUS_NONCE=' + self.anchor.nonce.encode()
        for index, root in enumerate(self.proc.iterdir()):
            if index > 16384:
                raise BackendError('game focus process inventory exceeds its bound')
            if not root.name.isdecimal():
                continue
            try:
                with (root / 'environ').open('rb') as stream:
                    env = stream.read(131073)
                if len(env) <= 131072 and marker in env.split(b'\0'):
                    found.add(int(root.name))
                    if len(found) > 64:
                        raise BackendError('game focus launch process set exceeds its bound')
            except OSError:
                continue  # Unrelated unreadable processes are not candidates.
        return found

    def evidence(self, pid):
        root = self.proc / str(pid)
        before = process_stat(pid, self.proc)[1]
        env = dict(part.split(b'=', 1) for part in root.joinpath('environ').read_bytes().split(b'\0') if b'=' in part)
        for key, expected in [('DISPLAY', self.anchor.display), ('WINEPREFIX', self.anchor.prefix),
                              ('KPTAS_SESSION', self.anchor.session), ('KPTAS_FOCUS_NONCE', self.anchor.nonce)]:
            if env.get(key.encode()) != expected.encode():
                raise BackendError('game focus process environment differs from its owned launch')
        rows = []
        for line in root.joinpath('maps').read_text().splitlines():
            row = line.split(None, 5)
            if len(row) == 6 and row[5] == str(self.anchor.exe):
                major, minor = (int(value, 16) for value in row[3].split(':'))
                rows.append((major, minor, int(row[4])))
        if not rows or len(set(rows)) != 1:
            raise BackendError('game focus process does not map the exact staged executable')
        # Reuse descriptor and namespace identity checks, including Btrfs's
        # same-descriptor mapping bridge. No title or Windows PID admission.
        from kptas.backend.handsoff_provenance import _read_module
        _, identity = _read_module(self.anchor.exe, rows[0], root)
        if process_stat(pid, self.proc)[1] != before:
            raise BackendError('game focus process lifetime changed during qualification')
        return before, identity['descriptor_signature']

    def focus(self, display, size):
        try:
            if display != self.anchor.display:
                raise BackendError('UI display differs from the owned game display')
            if (type(size) is not tuple or len(size) != 2
                    or any(type(value) is not int or not 0 < value <= 16384 for value in size)):
                raise BackendError('UI game focus requires verified native render geometry')
            deadline = time.monotonic() + 5
            candidates = []
            for pid in sorted(self.owned_processes()):
                if time.monotonic() >= deadline:
                    raise BackendError('game focus qualification exceeded its deadline')
                # search's status 1 means no match, unlike identity/read errors.
                result = self.run(['xdotool', 'search', '--onlyvisible', '--pid', str(pid)],
                    capture_output=True, timeout=.75, check=False,
                    env={**os.environ, 'DISPLAY': self.anchor.display})
                if result.returncode not in (0, 1):
                    raise BackendError('game focus window enumeration failed')
                ids = result.stdout.decode().split() if result.returncode == 0 else []
                if len(ids) > 64:
                    raise BackendError('game focus window inventory exceeds its bound')
                evidence = self.evidence(pid) if ids else None
                for value in ids:
                    if time.monotonic() >= deadline:
                        raise BackendError('game focus qualification exceeded its deadline')
                    wid = int(value)
                    if not 0 < wid < 1 << 32 or int(self.xdo('getwindowpid', wid)) != pid:
                        raise BackendError('game focus window PID ownership changed')
                    if self.eligible_window(wid, size):
                        candidates.append((wid, pid, evidence))
            if len(candidates) != 1:
                raise BackendError('game focus requires exactly one qualified visible game window')
            binding = candidates[0]
            if self.binding is not None and binding != self.binding:
                raise BackendError('game focus binding changed within the owned launch')
            wid, pid, evidence = binding
            if (pid not in self.owned_processes() or self.evidence(pid) != evidence
                    or int(self.xdo('getwindowpid', wid)) != pid or not self.eligible_window(wid, size)):
                raise BackendError('game focus binding changed before handoff')
            self.xdo('windowfocus', '--sync', wid)
            if int(self.xdo('getwindowfocus')) != wid or int(self.xdo('getwindowpid', wid)) != pid:
                raise BackendError('game focus handoff was not acknowledged')
            if (pid not in self.owned_processes() or self.evidence(pid) != evidence
                    or not self.eligible_window(wid, size)):
                raise BackendError('game focus process or window changed during handoff')
            self.binding = binding
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            raise BackendError(f'game focus qualification failed: {exc}') from exc
