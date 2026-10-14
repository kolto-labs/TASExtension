"""Typed lab-only engine requests; no position writes or visual/input automation."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_TAG = re.compile(r"[A-Za-z0-9_. -]{1,128}\Z")
_ACTIONS = {
    "observe": {"target"},
    "move_to": {"x", "y", "z", "run", "clear"},
    "move_to_object": {"target", "run", "range", "clear"},
    "attack": {"target", "clear"},
    "clear_actions": set(),
}


def validate_command(command: Any) -> dict[str, Any]:
    if not isinstance(command, Mapping):
        raise ValueError("engine command must be a table")
    c = dict(command)
    op = c.get("action")
    if not isinstance(op, str) or op not in _ACTIONS:
        raise ValueError(f"unknown engine action {op!r}")
    unknown = c.keys() - {"at", "action", "actor"} - _ACTIONS[op]
    if unknown:
        raise ValueError(f"unsupported engine command fields: {sorted(unknown)}")
    if type(c.get("at", 0)) is not int or not 0 <= c.get("at", 0) <= 2**31 - 1:
        raise ValueError("engine command at must be an integer in 0..2147483647")
    c.setdefault("at", 0)
    c.setdefault("actor", "pc")
    for key in ("actor", "target"):
        if key in c and (not isinstance(c[key], str) or not _TAG.fullmatch(c[key])):
            raise ValueError(f"engine command {key} must be a nonempty plain object tag (max128 characters)")
    required = {"x", "y", "z"} if op == "move_to" else {"target"} if op in ("move_to_object", "attack") else set()
    if not required <= c.keys():
        raise ValueError(f"{op} requires {', '.join(sorted(required))}")
    for key in ("x", "y", "z", "range"):
        if key in c:
            value = c[key]
            if type(value) not in (int, float) or abs(value) > 1e6 or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite coordinate within ±1000000")
            if key == "range" and value < 0:
                raise ValueError("range must be nonnegative")
    for key in ("run", "clear"):
        if key in c and type(c[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    return c


def validate_schedule(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 4096:
        raise ValueError("engine.commands must be an array of at most4096 command tables")
    rows = [validate_command(c) for c in value]
    if any(a["at"] > b["at"] for a, b in zip(rows, rows[1:])):
        raise ValueError("engine.commands must be ordered by at")
    return rows


def lua_request(command: Mapping[str, Any]) -> str:
    """Generate only vetted NWScript calls; tags exclude Lua quote/control syntax."""
    c = validate_command(command)
    actor = "nwscript.call('GetFirstPC')" if c["actor"] == "pc" else f"nwscript.call('GetObjectByTag','{c['actor']}',0)"
    lines = [f"local actor={actor}", "assert(nwscript.call('GetIsObjectValid',actor)==1,'invalid actor')"]
    if "target" in c:
        lines += [f"local target=nwscript.call('GetObjectByTag','{c['target']}',0)",
                  "assert(nwscript.call('GetIsObjectValid',target)==1,'invalid target')"]
    op = c["action"]
    if op != "observe":
        lines += ["assert(nwscript.call('GetObjectType',actor)==1,'actor must be a creature')"]
        if op == "move_to":
            lines += [f"local destination=nwscript.call('Location',nwscript.call('Vector',{c['x']!r},{c['y']!r},{c['z']!r}),0.0)"]
        lines += ["nwscript.with_caller(actor,function()"]
        if op == "clear_actions" or c.get("clear", False):
            lines += ["nwscript.call('ClearAllActions')"]
        if op == "move_to":
            lines += [f"nwscript.call('ActionMoveToLocation',destination,{int(c.get('run', True))})"]
        elif op == "move_to_object":
            lines += [f"nwscript.call('ActionMoveToObject',target,{int(c.get('run', True))},{c.get('range', 1.5)!r})"]
        elif op == "attack":
            lines += ["nwscript.call('ActionAttack',target,0)"]
        lines += ["end)"]
    lines += ["local p=nwscript.call('GetPosition',actor)",
              "return {actor=actor,position={x=p.x,y=p.y,z=p.z},current_action=nwscript.call('GetCurrentAction',actor)," +
              ("target=target,distance=nwscript.call('GetDistanceBetween',actor,target)," if "target" in c else "") +
              "request_accepted=true,completed=false}"]
    return "\n".join(lines)
