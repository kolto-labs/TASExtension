"""Range edits on the input document: insert, delete, clone, paste, clear, hold, autofire.

The input document is what Timeline.note returns for each frame: recorded
inputs of the current take, the recorded future after a rewind, and drafts on
top. Every function here returns {frame: Note} for Timeline.apply_inputs, so
each edit is one undoable transaction made of drafts. Nothing here talks to
the owner or executes a frame.

Frames at or before the current frame become pending past edits: the rail
marks the first one, and they take effect only when the user goes back and
plays through them (Apply selected, or seek back and step).

Pure logic: no Qt.
"""
from __future__ import annotations

from dataclasses import replace

from kptas.score.ast import Note

MAX_FRAMES = 20000   # largest edit in one transaction
MAX_CLIPBOARD_BYTES = 2 * 1024 * 1024

# Portable score mouse semantics. wh+N/wh-N are signed wheel detents, not
# browser WheelEvent pixels/lines. The native device multiplies by 120 once
# on the note's first frame; relative movement applies on every frame.
MOUSE_INPUT_CONTRACT = {
    'schema': 'telos-mouse-input/1',
    'wheel_unit': 'detent', 'wheel_phase': 'first-frame',
    'wheel_min': -17895697, 'wheel_max': 17895697,
    'absolute_unit': 'guest-pixel', 'relative_unit': 'guest-pixel',
    'relative_phase': 'every-frame',
}


def _validate_mouse(note):
    if type(note.wheel) is not int or not -17895697 <= note.wheel <= 17895697:
        raise ValueError('wheel must be signed integer detents within the native device range')
    for name, pair, minimum in (('absolute mouse', note.mouse_abs, 0),
                                ('relative mouse', note.mouse_rel, -2147483648)):
        if pair is not None and (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(type(v) is not int or not minimum <= v <= 2147483647 for v in pair)):
            raise ValueError(f'{name} coordinates must be two bounded integer guest pixels')


def document_end(timeline):
    """Last frame with an input in the document (at least the current frame)."""
    end = timeline.tick
    future = getattr(timeline, 'future', None)
    if future is not None:
        end = max(end, future.replay_end)
    if timeline.drafts:
        end = max(end, max(timeline.drafts))
    return end


def _check_span(low, high):
    if type(low) is not int or type(high) is not int or low < 1 or high < low:
        raise ValueError('choose frames from 1 upward')
    if high - low + 1 > MAX_FRAMES:
        raise ValueError(f'this edit covers {high - low + 1} frames; the limit is {MAX_FRAMES}')


def frame_notes(timeline, low, high):
    """Effective inputs for frames low..high."""
    _check_span(low, high)
    return [timeline.note(tick) for tick in range(low, high + 1)]


def blank():
    return Note(count=1)


def insert_frames(timeline, at, count=1, notes=None):
    """Insert `count` frames before frame `at`; later inputs move right.

    `notes` fills the new frames (clone or paste); blank frames otherwise.
    """
    end = document_end(timeline)
    if type(count) is not int or count < 1:
        raise ValueError('insert at least one frame')
    fill = list(notes) if notes is not None else [blank()] * count
    if len(fill) != count:
        raise ValueError('fill length does not match the inserted frames')
    if end < at:
        return {at + i: fill[i] for i in range(count)}
    _check_span(at, end + count)
    moved = frame_notes(timeline, at, end)
    values = {at + i: fill[i] for i in range(count)}
    for offset, note in enumerate(moved):
        values[at + count + offset] = note
    return values


def delete_frames(timeline, at, count=1):
    """Delete frames at..at+count-1; later inputs move left, the end becomes blank."""
    end = document_end(timeline)
    if type(count) is not int or count < 1:
        raise ValueError('delete at least one frame')
    if at > end:
        return {}
    _check_span(at, end)
    later = frame_notes(timeline, at + count, end) if at + count <= end else []
    values = {}
    for offset in range(end - at + 1):
        values[at + offset] = later[offset] if offset < len(later) else blank()
    return values


def clone_frames(timeline, low, high):
    """Insert a copy of frames low..high right after them."""
    notes = frame_notes(timeline, low, high)
    return insert_frames(timeline, high + 1, len(notes), notes)


def paste(timeline, at, notes, *, insert=False):
    """Write `notes` from frame `at` (overwrite), or insert them there."""
    notes = list(notes)
    if not notes:
        return {}
    if insert:
        return insert_frames(timeline, at, len(notes), notes)
    _check_span(at, at + len(notes) - 1)
    return {at + i: note for i, note in enumerate(notes)}


def _held(held, lanes, pressed):
    """Keep the recorded key order; add or drop only the chosen lanes."""
    if pressed:
        return tuple(held) + tuple(key for key in lanes if key not in held)
    return tuple(key for key in held if key not in lanes)


def set_lanes(timeline, low, high, lanes, pressed):
    """Press or release `lanes` on every frame low..high."""
    values = {}
    for tick, note in zip(range(low, high + 1), frame_notes(timeline, low, high)):
        values[tick] = replace(note, held=_held(note.held, lanes, pressed))
    return values


def autofire(timeline, low, high, lanes, *, period=2, phase=0):
    """Alternate `lanes`: pressed for period/2 frames (at least one), released for the rest."""
    if type(period) is not int or period < 2:
        raise ValueError('autofire needs a period of at least 2 frames')
    on = max(1, period // 2)
    values = {}
    for tick, note in zip(range(low, high + 1), frame_notes(timeline, low, high)):
        pressed = (tick - low + phase) % period < on
        values[tick] = replace(note, held=_held(note.held, lanes, pressed))
    return values


# -- clipboard -------------------------------------------------------------
CLIPBOARD_HEADER = '#!score v1'


def notes_to_text(notes):
    """Score text for frames, one line per run of identical inputs."""
    from kptas.score.fmt import render
    from .timeline_future import input_key
    lines, run, count = [CLIPBOARD_HEADER], None, 0
    for index, note in enumerate(notes):
        if index >= MAX_FRAMES:
            raise ValueError(f'copy at most {MAX_FRAMES} input frames at a time')
        if not isinstance(note, Note) or note.osev:
            raise ValueError('copy plain input frames; OS events require score playback')
        _validate_mouse(note)
        if run is not None and input_key(note) == input_key(run) and not note.wheel and not note.osev:
            count += 1
            continue
        if run is not None:
            lines.append(render(replace(run, count=count)))
        run, count = note, 1
    if run is not None:
        lines.append(render(replace(run, count=count)))
    if run is None:
        raise ValueError('select at least one input frame to copy')
    text = '\n'.join(lines) + '\n'
    if len(text.encode('utf-8')) > MAX_CLIPBOARD_BYTES:
        raise ValueError('the copied inputs exceed 2 MiB')
    return text


def text_to_notes(text):
    """Frames from score text (a header line is optional). Refuses barriers."""
    from kptas.score import compile_score
    if not isinstance(text, str) or not text.strip():
        raise ValueError('the clipboard has no inputs')
    if len(text.encode('utf-8')) > MAX_CLIPBOARD_BYTES:
        raise ValueError('the clipboard is larger than 2 MiB')
    # Clipboard is a bounded literal input range, not a whole executable score.
    # Reject directives before compiling so aliases/repeats/timing metadata
    # cannot be silently flattened or allocate work outside the range budget.
    import re
    text = text.removeprefix('\ufeff').strip().replace('\r\n', '\n').replace('\r', '\n')
    for line in text.split('\n'):
        if line.strip().startswith('#!score') and line.strip() != CLIPBOARD_HEADER:
            raise ValueError('copy plain input frames without timing or recording metadata')
        code = line.split('#', 1)[0].strip()
        if code and not re.match(r'^[0-9]+\s', code):
            raise ValueError('paste plain input frames without score directives')
    body = text if text.startswith(CLIPBOARD_HEADER) else CLIPBOARD_HEADER + '\n' + text
    items = compile_score(body).items
    notes = []
    for item in items:
        if not isinstance(item, Note) or item.count < 1:
            raise ValueError('the clipboard holds more than plain frame inputs')
        if item.osev:
            raise ValueError('the clipboard holds an OS event, which needs score playback')
        if item.count > MAX_FRAMES - len(notes):
            raise ValueError(f'the clipboard holds more than {MAX_FRAMES} frames')
        _validate_mouse(item)
        first = replace(item, count=1)
        notes.append(first)
        notes.extend([replace(item, count=1, wheel=0)] * (item.count - 1))
        if len(notes) > MAX_FRAMES:
            raise ValueError(f'the clipboard holds more than {MAX_FRAMES} frames')
    if not notes:
        raise ValueError('the clipboard has no input frames')
    return notes
