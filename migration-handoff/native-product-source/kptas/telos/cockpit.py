"""Presentation adapter for one owner-supplied kptas.telos.control.Controls.

Does not launch a game. Owner invokes pump() at the existing stopped gate.
All presentation commands and pump calls share this adapter; never also run
control.run()'s competing hotkey loop against the same Controls instance.
"""
from threading import RLock
from kptas.durability import inspect_checkpoint

class Cockpit:
    def __init__(self, control):
        self.control = control
        self.lock = RLock()
        self.verified = None
        self.durable = None
        self.error = None

    def snapshot(self):
        with self.lock:
            c = self.control
            return {
                'schema': 'kptas-cockpit/1',
                'tick': c.sup.tick, 'epoch': c.sup.epoch,
                'mode': 'playing' if c.playing else 'paused',
                'process_alive': c.sup.alive,
                'branch': {'id': c.bundle.bundle_id, 'path': str(c.bundle.root)},
                'recorded_last_tick': c.bundle.last_tick,
                'verified_restore': self.verified.copy() if self.verified else None,
                'durable_checkpoint': self.durable.copy() if self.durable else None,
                'footage_durability': None, 'draft_durability': None,
                'error': self.error,
                'capabilities': {
                    'play': True, 'pause': True, 'step': True, 'seek': True,
                    'refresh_saved_status': True,
                    'save': 'not connected to the active recorder checkpoint owner',
                    'branch': 'requires prepared score and output via the checked branch pipeline',
                    'edit': 'canonical score editor is not connected',
                    'memory': 'read-only memory provider is not connected',
                },
            }

    def command(self, operation, **args):
        with self.lock:
            c = self.control
            try:
                if operation in ('play', 'pause', 'step', 'refresh_saved_status') and args:
                    raise ValueError('unexpected arguments')
                if operation == 'play':
                    if not c.sup.alive: raise ValueError('no live stopped-gate process')
                    if not c.playing: c.toggle_play()
                elif operation == 'pause':
                    if c.playing: c.toggle_play()
                elif operation == 'step':
                    if not c.sup.alive: raise ValueError('no live stopped-gate process')
                    c.playing = False
                    c.step()
                elif operation == 'seek':
                    if set(args) != {'tick'} or type(args['tick']) is not int or args['tick'] < 0:
                        raise ValueError('seek requires a nonnegative integer tick')
                    c.rewind(args['tick'])
                    # Controls.rewind refuses fallback, non-full-root, and failed canary (restore),
                    # and checks a hybrid (restore@K + replay, capture cadence) by its own receipt.
                    # Record what the receipt says; never call a hybrid a strict restore.
                    receipt = getattr(c, 'last_receipt', None)
                    mode = str(getattr(receipt, 'mode', ''))
                    hybrid = mode == 'hybrid'
                    self.verified = {'tick': c.sup.tick, 'epoch': c.sup.epoch,
                                     'kind': ('last successful restore@K + replay (hybrid); not current-tick certification'
                                              if hybrid else 'last successful strict restore; not current-tick certification'),
                                     'mode': mode or 'restore',
                                     'full_root': str(getattr(receipt, 'verify_level', 'full_root')) == 'full_root',
                                     'canary': str(getattr(receipt, 'canary', 'pass')) if hybrid else 'pass',
                                     'branch_id': c.bundle.bundle_id}
                    if hybrid:
                        import re
                        anchor = re.search(r'restore@(\d+)(?!.*restore@\d)', getattr(receipt, 'verifier', '') or '')
                        if anchor is not None:
                            self.verified['anchor_tick'] = int(anchor.group(1))
                elif operation == 'set_engaged':
                    if getattr(c, 'engagement_qualified', False) is not True:
                        raise ValueError('normal input handoff is not qualified')
                    if set(args) - {'engaged', 'pause_on_engage'} or 'engaged' not in args:
                        raise ValueError('engagement arguments required')
                    c.set_engaged(args['engaged'], args.get('pause_on_engage', True))
                elif operation == 'configure_shortcuts':
                    from .xhotkeys import native_bindings
                    if set(args) != {'shortcuts'}: raise ValueError('shortcuts required')
                    c.sup.backend.hud_bindings(native_bindings(args['shortcuts']))
                elif operation == 'refresh_saved_status':
                    # Explicit and potentially expensive; never a per-frame disk poll.
                    saved = inspect_checkpoint(c.bundle.root)
                    self.durable = {'tick': saved['tick'], 'epoch': saved['epoch'],
                                    'state_root': saved['state_root'],
                                    'branch_id': saved['bundle_id'],
                                    'native_verified': False,
                                    'captured_restore_point': saved['captured_restore_point'],
                                    # capture cadence: where a resume lands (restore point <= tick)
                                    'restore_tick': saved.get('restore_tick', saved['tick']
                                                              if saved['captured_restore_point'] else None),
                                    'scope': 'last explicitly inspected published checkpoint'}
                else:
                    raise ValueError('unsupported operation: ' + str(operation))
                self.error = None
                return {'ok': True, 'operation': operation, 'state': self.snapshot()}
            except Exception as exc:
                c.playing = False
                self.error = {'operation': str(operation), 'message': str(exc),
                              'type': type(exc).__name__}
                return {'ok': False, 'operation': str(operation), 'state': self.snapshot()}

    def pump(self):
        """Execute at most one actual recorded tick; scheduling is the owner's job."""
        with self.lock:
            physical_running = self.control.input_scheduler_running
            if not self.control.playing and not physical_running: return self.snapshot()
            try:
                if not self.control.sup.alive: raise ValueError('no live stopped-gate process')
                if physical_running:
                    self.control.input_scheduler_step()
                else:
                    self.control.step()
                self.error = None
            except Exception as exc:
                self.control.playing = False
                if physical_running:
                    self.control.park_input_scheduler(exc)
                self.error = {'operation': 'play', 'message': str(exc),
                              'type': type(exc).__name__}
            return self.snapshot()
