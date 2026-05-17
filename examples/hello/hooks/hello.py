#!/usr/bin/env python3
"""hello evolve hook — notes CRUD with all hook handlers."""

import json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict, get_type_hints

WORKSPACE = Path(__file__).resolve().parent.parent
NOTES = WORKSPACE / "traits"

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
def param(description, type="string", optional=False, enum=None):
    spec = {"type": type, "description": description, "optional": optional}
    if enum:
        spec["enum"] = list(enum)
    return spec

NOTE_PRIORITIES = ["low", "normal", "high"]

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

# compose system prompt from evolve-injected prompt contract parts, appending
# the notes list and an env block. preamble/stage bodies come from ctx.prompts.
def system_prompt(prompts, mode=None):
    parts = [prompts.get("preamble", "")]
    if mode:
        parts.append(prompts.get(mode, ""))
    notes = note_names()
    if notes:
        parts.append(f"\ncurrent notes: {', '.join(notes)}\n")
    parts.append(
        f"\n<env>\nSession start time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n</env>\n"
    )
    return ["".join(p for p in parts if p)]

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
    priority: Annotated[str, param(f"optional priority. one of: {', '.join(NOTE_PRIORITIES)}", optional=True, enum=NOTE_PRIORITIES)] = "",
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
    if priority:
        headers.append(f"priority: {priority}")
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
    # v2 host-capability surfacing: log host/model/user when present.
    host = ctx.get("host") or {}
    if host:
        debug(f"host={host.get('name', '?')} v={host.get('version', '?')}")
    if "model" in ctx:
        debug(f"model={ctx.get('model') or '(none)'}")
    if "user" in ctx:
        debug(f"user_len={len(ctx.get('user') or '')}")
    debug(f"notes: {', '.join(note_names())}")
    # hello appends a notes list and env block; preamble/chat come from
    # ctx.prompts (the evolve prompt contract). hosts that need to gate
    # which paths reach this hook handle that themselves (see
    # opencode-evolve's agent_marker config).
    return {"system": system_prompt(ctx.get("prompts", {}), "chat")}

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
def before_stop(ctx: dict) -> HookResult:
    session = ctx.get("session", {})
    answer = ctx.get("answer", "")
    debug(f"session={session.get('id', '?')} answer_len={len(answer)}")
    # v2: hosts that distinguish loop exit causes pass `exit_reason` and
    # `final`. opencode/pi only fire on natural idle so they may omit
    # both; airun always sets `final: true` (no re-entry).
    if "exit_reason" in ctx:
        debug(f"exit_reason={ctx.get('exit_reason')} final={ctx.get('final', False)}")
        if ctx.get("error"):
            debug(f"error={ctx.get('error')}")
    return {}

@hook
def heartbeat(ctx: dict) -> HookResult:
    debug(f"notes: {', '.join(note_names())}")
    prompts = ctx.get("prompts", {})
    user = (prompts.get("heartbeat") or "").strip()
    if not user:
        return {}
    return {"system": system_prompt(prompts, "heartbeat"), "user": user}

@hook
def recover(ctx: dict) -> HookResult:
    debug(f"recovering from {ctx.get('failed_hook', '?')}: {ctx.get('error', '?')}")
    return {"system": ["system recovery — an error occurred"], "user": "please check notes and continue"}

@hook
def before_tool(ctx: dict) -> HookResult:
    # demo of v2 mutation response keys, gated on a sentinel arg so normal
    # tool calls flow through unchanged. tools matching `_deny_me_*` are
    # refused; tools matching `_redact_*` get their args overwritten.
    tool = ctx.get("tool", "")
    args = ctx.get("args", {}) or {}
    name_arg = (args.get("name") or "")
    if name_arg.startswith("_deny_me_"):
        return {"deny": f"refused by hello: {name_arg}"}
    if name_arg.startswith("_redact_"):
        return {"args": {**args, "name": "REDACTED.md"}}
    return {}

@hook
def after_tool(ctx: dict) -> HookResult:
    # demo of `result` mutation, gated on a sentinel in the result text so
    # production output is untouched. when the tool result starts with
    # `_replace_me`, substitute a canned reply.
    out = ctx.get("output") or ctx.get("result") or ""
    if isinstance(out, str) and out.startswith("_replace_me"):
        return {"result": "REPLACED-BY-AFTER-TOOL"}
    return {}

@hook
def compacting(ctx: dict) -> HookResult:
    debug(f"notes: {', '.join(note_names())}")
    # evolve falls back to the compaction.md contract file when we return nothing
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
        print(json.dumps({"error": "usage: hello.py <hook_name>"}))
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
