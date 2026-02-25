#!/usr/bin/env python3
"""hello evolve hook — notes CRUD with all hook handlers."""

import json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict, get_type_hints

WORKSPACE = Path(__file__).resolve().parent.parent
NOTES = WORKSPACE / "traits"
PROMPTS = WORKSPACE / "prompts"

class HookResult(TypedDict, total=False):
    system: list[str]
    tools: list[dict]
    user: str
    prompt: str
    message: str
    actions: list[dict]
    result: str
    modified: list[str]
    notify: list[dict]
    error: str

HOOKS, TOOLS = {}, {}

def hook(fn):
    HOOKS[fn.__name__] = fn
    return fn

def tool(fn):
    TOOLS[fn.__name__] = fn
    return fn

def debug(msg):
    print(json.dumps({"log": msg}), flush=True)

def note_names():
    if not NOTES.exists():
        return []
    return sorted(f.name for f in NOTES.iterdir() if f.is_file())

def prompt_path(name):
    return PROMPTS / f"{name}.md"

def system_prompt(mode=None):
    parts = [prompt_path("preamble").read_text()]
    if mode:
        parts.append(prompt_path(mode).read_text())
    notes = note_names()
    if notes:
        parts.append(f"\ncurrent notes: {', '.join(notes)}\n")
    parts.append(
        f"\n<env>\nSession start time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n</env>\n"
    )
    return ["".join(parts)]

# --- tools ---

@tool
def note_list() -> HookResult:
    """list all notes"""
    names = note_names()
    return {"result": f"notes: {', '.join(names)}" if names else "no notes yet"}

@tool
def note_read(
    name: Annotated[str, "note filename (e.g. todo.md)"],
) -> HookResult:
    """read a note"""
    try:
        content = (NOTES / name).read_text()
    except FileNotFoundError:
        return {"result": f"not found: {name}"}
    return {"result": content}

@tool
def note_write(
    name: Annotated[str, "note filename (e.g. todo.md)"],
    content: Annotated[str, "full content for the note"],
) -> HookResult:
    """write a note"""
    NOTES.mkdir(parents=True, exist_ok=True)
    (NOTES / name).write_text(content)
    return {"result": f"wrote {name}", "modified": [name],
            "notify": [{"type": "note_changed", "files": [name]}]}

@tool
def note_delete(
    name: Annotated[str, "note filename (e.g. todo.md)"],
) -> HookResult:
    """delete a note"""
    path = NOTES / name
    if not path.exists():
        return {"result": f"not found: {name}"}
    path.unlink()
    return {"result": f"deleted {name}", "modified": [name],
            "notify": [{"type": "note_changed", "files": [name]}]}

# --- tool introspection ---

def tool_defs():
    defs = []
    for name, fn in TOOLS.items():
        hints = get_type_hints(fn, include_extras=True)
        params = {
            p: h.__metadata__[0]
            for p, h in hints.items()
            if p != "return" and hasattr(h, "__metadata__")
        }
        defs.append(
            {"name": name, "description": fn.__doc__ or "", "parameters": params}
        )
    return defs

# --- hooks ---

@hook
def discover(ctx: dict) -> HookResult:
    names = [t["name"] for t in tool_defs()]
    debug(f"tools: {', '.join(names)}")
    return {"tools": tool_defs()}

@hook
def mutate_request(ctx: dict) -> HookResult:
    debug(f"notes: {', '.join(note_names())}")
    return {"system": system_prompt("chat")}

@hook
def format_notification(ctx: dict) -> HookResult:
    notifications = ctx.get("notifications", [])
    changed = set()
    for n in notifications:
        if n.get("type") == "note_changed":
            changed.update(n.get("files", []))
    if not changed:
        return {}
    return {"message": f"[note-update] changed: {', '.join(sorted(changed))}"}

@hook
def observe_message(ctx: dict) -> HookResult:
    session = ctx.get("session", {})
    debug(f"session={session.get('id', '?')} agent={session.get('agent', '?')}")
    return {}

@hook
def idle(ctx: dict) -> HookResult:
    session = ctx.get("session", {})
    answer = ctx.get("answer", "")
    debug(f"session={session.get('id', '?')} answer_len={len(answer)}")
    return {}

@hook
def heartbeat(ctx: dict) -> HookResult:
    debug(f"notes: {', '.join(note_names())}")
    try:
        user = prompt_path("heartbeat").read_text()
        if not user.strip():
            debug("heartbeat prompt is empty, skipping")
            return {}
        return {"system": system_prompt("heartbeat"), "user": user}
    except FileNotFoundError:
        debug("heartbeat.md not found, skipping")
        return {}

@hook
def recover(ctx: dict) -> HookResult:
    debug(f"recovering from {ctx.get('failed_hook', '?')}: {ctx.get('error', '?')}")
    return {"system": ["system recovery — an error occurred"], "user": "please check notes and continue"}

@hook
def tool_before(ctx: dict) -> HookResult:
    return {}

@hook
def tool_after(ctx: dict) -> HookResult:
    return {}

@hook
def compacting(ctx: dict) -> HookResult:
    debug(f"notes: {', '.join(note_names())}")
    try:
        return {"prompt": prompt_path("compaction").read_text()}
    except FileNotFoundError:
        debug("compaction.md not found, skipping")
        return {}

@hook
def execute_tool(ctx: dict) -> HookResult:
    name = ctx.get("tool", "")
    handler = TOOLS.get(name)
    if not handler:
        debug(f"unknown tool: {name}")
        return {"result": f"unknown tool: {name}"}
    args = ctx.get("args", {})
    debug(f"tool={name} args={list(args.keys())}")
    try:
        result = handler(**args)
        debug(f"tool={name} result keys={list(result.keys())}")
        return result
    except Exception as e:
        debug(f"tool={name} error: {e}")
        return {"result": f"tool error: {e}"}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: evolve.py <hook_name>"}))
        sys.exit(1)
    h = HOOKS.get(sys.argv[1])
    if not h:
        print(json.dumps({"error": f"unknown hook: {sys.argv[1]}"}))
        sys.exit(1)
    try:
        ctx = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        ctx = {}
    try:
        result = h(ctx)
    except Exception as e:
        debug(f"{sys.argv[1]}: {e}")
        result = {"error": str(e)}
    for key, value in result.items():
        print(json.dumps({key: value}), flush=True)
