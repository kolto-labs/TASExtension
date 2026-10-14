"""Read-only presentation of recorded inputs; never infer physical held keys."""
from kptas.supervisor import parse_note, TelosError
from kptas.score import ScoreError
from kptas.score.keys import token_keys
from kptas.holo.verify import journal_of


def recorded_input(bundle, tick):
    result = {'tick': tick, 'source': 'recorded', 'available': False,
              'note': None, 'keys': [], 'buttons': [], 'mouse_abs': None,
              'mouse_rel': None, 'wheel': 0, 'request': None, 'events': []}
    try:
        rec = bundle.record(tick)
    except (ValueError, KeyError, IndexError):
        return result
    result.update(available=True, note=rec.note)
    try:
        note = parse_note(rec.note)
    except (ValueError, RuntimeError, TelosError, ScoreError):
        result['parse_error'] = True
        return result
    result.update(keys=sorted({key for token in note.held for key in token_keys(token)}),
                  buttons=list(note.buttons), mouse_abs=note.mouse_abs,
                  mouse_rel=note.mouse_rel, wheel=note.wheel, request=note.osev)
    journal = journal_of(rec) or {}
    result['events'] = [event for event in journal.get('ev', []) if isinstance(event, dict)]
    return result


def input_summary(view):
    if not view['available']:
        return 'Not recorded'
    if view.get('parse_error'):
        return view['note'] or 'Unavailable'
    parts = [key.upper() for key in view['keys']] + [key.upper() for key in view['buttons']]
    if view['mouse_abs'] is not None:
        parts.append('mouse @ ' + ','.join(map(str, view['mouse_abs'])))
    if view['mouse_rel'] is not None:
        parts.append('mouse Δ ' + ','.join(map(str, view['mouse_rel'])))
    if view['wheel']:
        parts.append(f"wheel {view['wheel']:+d}")
    if view['request']:
        parts.append('request ' + view['request'])
    return ' · '.join(parts) or 'No input'


def sampled_input(sample, tick):
    """Only a current native device sample may drive the primary key display."""
    from kptas.score.keys import KEYS
    result={'available':False,'tick':tick,'keys':[],'buttons':[],'cursor':None,'armed':False}
    if (not isinstance(sample,dict) or sample.get('source')!='virtual_devices'
            or sample.get('sampled_at_gate') is not True or sample.get('tick')!=tick):
        return result
    by_dik={v[1]:key for key,v in KEYS.items()}
    result.update(available=True,
                  keys=[by_dik.get(key,f'dik:{key}') if isinstance(key,int) else str(key)
                        for key in sample.get('keys',[])],
                  buttons=list(sample.get('buttons',[])),cursor=sample.get('cursor'),
                  armed=bool(sample.get('altf4_save_armed')))
    return result


def key_label(key):
    return {'lctrl':'Ctrl','rctrl':'Right Ctrl','lshift':'Shift','rshift':'Right Shift',
            'lalt':'Alt','ralt':'Right Alt','space':'Space','enter':'Enter','esc':'Esc',
            'lmb':'Left mouse','rmb':'Right mouse','mmb':'Middle mouse',
            'up':'↑','down':'↓','left':'←','right':'→'}.get(key,key.upper())
