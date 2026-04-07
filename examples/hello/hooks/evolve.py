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

# parameter spec: dict metadata = typed param, bare string = string type (backwards compat)
def param(description, type="string", optional=False):
    return {"type": type, "description": description, "optional": optional}

def hook(fn):
    HOOKS[fn.__name__] = fn
    return fn

def tool(fn=None, *, permission=None):
    def decorator(f):
        if permission:
            f._permission = permission
        TOOLS[f.__name__] = f
        return f
    if fn is not None:
        return decorator(fn)
    return decorator

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

def truthy(v):
    """coerce json-decoded bool, int, or string to a python bool"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "1", "yes", "on")

@tool
def note_list(
    include_hidden: Annotated[str, param("include hidden (dot-prefixed) notes", type="boolean", optional=True)] = "false",
) -> HookResult:
    """list all notes"""
    names = note_names()
    if not truthy(include_hidden):
        names = [n for n in names if not n.startswith(".")]
    return {"result": f"notes: {', '.join(names)}" if names else "no notes yet"}

@tool(permission={"arg": "name"})
def note_read(
    name: Annotated[str, "note filename (e.g. todo.md)"],
    limit: Annotated[str, param("maximum lines to return (default: all)", type="number", optional=True)] = "",
) -> HookResult:
    """read a note"""
    try:
        content = (NOTES / name).read_text()
    except FileNotFoundError:
        return {"result": f"not found: {name}"}
    if limit:
        try:
            content = "\n".join(content.splitlines()[:int(limit)])
        except ValueError:
            pass
    return {"result": content}

@tool(permission={"arg": "name"})
def note_write(
    name: Annotated[str, "note filename (e.g. todo.md)"],
    content: Annotated[str, "full content for the note"],
    tags: Annotated[object, param("optional tags to prepend as a 'tags:' header line", type="array[string]", optional=True)] = None,
    metadata: Annotated[object, param("optional key/value metadata, json-encoded into the header", type="object", optional=True)] = None,
    extras: Annotated[object, param("optional free-form extras (any json value), json-encoded as 'extras:' header line", type="any", optional=True)] = None,
    raw_list: Annotated[object, param("optional mixed-type list, json-encoded as 'items:' header line", type="array", optional=True)] = None,
) -> HookResult:
    """write a note"""
    NOTES.mkdir(parents=True, exist_ok=True)
    headers = []
    if tags:
        headers.append(f"tags: {', '.join(str(t) for t in tags)}")
    if metadata:
        headers.append(f"metadata: {json.dumps(metadata)}")
    if extras is not None:
        headers.append(f"extras: {json.dumps(extras)}")
    if raw_list:
        headers.append(f"items: {json.dumps(raw_list)}")
    body = ("\n".join(headers) + "\n" + content) if headers else content
    (NOTES / name).write_text(body)
    return {"result": f"wrote {name}", "modified": [name],
            "notify": [{"type": "note_changed", "files": [name]}]}

@tool(permission={"arg": "name"})
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
        entry = {"name": name, "description": fn.__doc__ or "", "parameters": params}
        if hasattr(fn, "_permission"):
            entry["permission"] = fn._permission
        defs.append(entry)
    return defs

# --- hooks ---

@hook
def discover(ctx: dict) -> HookResult:
    names = [t["name"] for t in tool_defs()]
    debug(f"tools: {', '.join(names)}")
    return {"name": "hello", "test": "hello_test.py", "tools": tool_defs()}

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
        sys.exit(0)
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
