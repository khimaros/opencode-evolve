# opencode-evolve

self-modifying hook plugin for [opencode](https://opencode.ai). gives LLM agents the ability to modify their own hooks and prompts (with test validation), discover and invoke custom tools, and evolve their behavior at runtime.

## installation

add to your `opencode.jsonc`:

```jsonc
{
  "plugin": ["opencode-evolve"]
}
```

set `OPENCODE_EVOLVE_WORKSPACE` to your workspace directory (default: `~/workspace`).

## workspace layout

```
~/workspace/
├── config/
│   ├── evolve.jsonc        # evolve settings
│   └── runtime.json        # runtime state (auto-managed)
├── hooks/
│   └── evolve.py           # hook script
├── prompts/                # prompt templates
└── traits/                 # persona traits or any hook-specific content
```

## configuration

`config/evolve.jsonc` — all fields optional:

```jsonc
{
  "hook": "evolve.py",            // hook script filename in hooks/
  "heartbeat_ms": 1800000,        // heartbeat interval (30 min)
  "hook_timeout": 30000,          // subprocess timeout (30s)
  "heartbeat_title": "heartbeat", // heartbeat session title
  "heartbeat_agent": "evolve",    // agent ID for heartbeat prompts
  "avatar": "🌀",                 // log/tool output prefix
  "test_script": null              // path to test script for hook validation
}
```

`heartbeat_agent` must match a configured agent in your `opencode.jsonc`. for example:

```jsonc
{
  "default_agent": "evolve",
  "agent": {},
  "plugin": ["opencode-evolve"]
}
```

with a corresponding agent file at `agents/evolve.md`.

## hook protocol

the plugin calls your hook script as a subprocess:

```
$WORKSPACE/hooks/<hook> <hook_name>
```

**input**: JSON on stdin with at minimum `{"hook": "<name>", ...context}`.

**output**: JSONL on stdout. each line is a JSON object. lines with `{"log": "..."}` are printed to the debug log. all other lines are merged into the final result.

**stderr**: forwarded to the debug log.

**exit code**: 0 = success, non-zero = failure (triggers `recover` hook unless the failing hook is observational).

### hooks

#### `discover`

called once at plugin init. return tool definitions.

input:
```json
{"hook": "discover"}
```

output:
```json
{"tools": [{"name": "my_tool", "description": "...", "parameters": {"arg": "description"}}]}
```

#### `mutate_request`

called on each new session to generate the system prompt. return `{"system": [...]}` to manage the session, or `{}` to skip. the result is cached per-session (system prompt is frozen after first call).

input:
```json
{"hook": "mutate_request", "session": {"id": "..."}, "history": [...]}
```

output:
```json
{"system": ["system prompt text..."]}
```

#### `observe_message`

called after each LLM response. observational — failure does not trigger `recover`.

input:
```json
{"hook": "observe_message", "session": {"id": "...", "agent": "..."}, "thinking": "...", "calls": [...], "answer": "..."}
```

output:
```json
{"modified": ["file.md"], "notify": [{"type": "trait_changed", "files": ["file.md"]}], "actions": [...]}
```

#### `idle`

called when the LLM gives a final response with no tool calls. return `{"continue": "message"}` to force the session to keep going.

input:
```json
{"hook": "idle", "session": {"id": "...", "agent": "..."}, "answer": "..."}
```

output:
```json
{}
```
or:
```json
{"continue": "follow-up prompt text"}
```

#### `heartbeat`

called on the heartbeat timer interval. return a system prompt and user message to send to the heartbeat session.

input:
```json
{"hook": "heartbeat", "sessions": [], "history": [...]}
```

output:
```json
{"system": ["..."], "user": "heartbeat prompt text"}
```

#### `compacting`

called when opencode compacts a session. return a custom compaction prompt.

input:
```json
{"hook": "compacting", "session": {"id": "..."}, "history": [...]}
```

output:
```json
{"prompt": "compaction prompt text..."}
```

#### `format_notification`

called to format pending notifications before injecting them into a session. observational — failure does not trigger `recover`.

input:
```json
{"hook": "format_notification", "session": {"id": "..."}, "notifications": [...]}
```

output:
```json
{"message": "[trait-update] updated: FOO.md. re-read if needed."}
```

#### `recover`

called when another hook fails (except observational hooks). return emergency system prompt and user message.

input:
```json
{"hook": "recover", "error": "...", "failed_hook": "..."}
```

output:
```json
{"system": ["recovery prompt"], "user": "recovery instructions"}
```

#### `execute_tool`

called when a discovered tool is invoked.

input:
```json
{"hook": "execute_tool", "tool": "my_tool", "args": {"arg": "value"}}
```

output:
```json
{"result": "tool output", "modified": ["file.md"], "notify": [...]}
```

#### `tool_before` / `tool_after`

called before/after any opencode tool execution. observational.

input (before):
```json
{"hook": "tool_before", "session": {"id": "..."}, "tool": "tool_name", "callID": "...", "args": {}}
```

input (after):
```json
{"hook": "tool_after", "session": {"id": "..."}, "tool": "tool_name", "callID": "...", "title": "...", "output": "..."}
```

## self-modification

the plugin provides builtin tools that let agents modify their own behavior at runtime:

- **hook editing** — `hook_read`, `hook_write`, `hook_patch` let the agent rewrite its own hook script. writes are validated against the configured `test_script` before installation.
- **prompt editing** — `prompt_list`, `prompt_read`, `prompt_write`, `prompt_patch` let the agent modify its own prompt templates.
- **tool discovery** — custom tools defined by the hook's `discover` response are automatically registered with opencode.

## tool discovery

tools are defined by the hook's `discover` response. each tool gets a prefixed name derived from the hook filename stem. for example, if `hook` is `evolve.py`, a tool named `trait_read` becomes `evolve_trait_read`.

### builtin tools

the plugin provides these tools regardless of what the hook returns. they use the same prefix:

- `<prefix>_prompt_list` — list prompt files in the workspace
- `<prefix>_prompt_read` — read a prompt file
- `<prefix>_prompt_write` — write a prompt file
- `<prefix>_prompt_patch` — patch a prompt file (find-and-replace)
- `<prefix>_hook_validate` — validate a hook script without installing
- `<prefix>_hook_read` — read the current hook script
- `<prefix>_hook_write` — write a new hook (validated before install)
- `<prefix>_hook_patch` — patch the hook (validated before install)

## git integration

the workspace is auto-initialized as a git repo. when a new repo is created, any pre-existing files are committed as an "initial" snapshot before any tool-triggered commits, so the diff history clearly shows what was present before vs what is new. after tool execution and heartbeats, changes are committed automatically.

## actions

hooks can return an `actions` array to trigger side effects:

```json
{"actions": [
  {"type": "send", "session_id": "...", "message": "...", "synthetic": true},
  {"type": "create_session", "title": "..."}
]}
```

## writing a custom hook

see [`examples/hello/`](examples/hello/) for a complete working example. the hook script must:

1. be executable
2. accept the hook name as first argument (`sys.argv[1]`)
3. read JSON from stdin
4. write JSONL to stdout
5. exit 0 on success

minimal python hook:

```python
#!/usr/bin/env python3
import json, sys

HOOKS = {}

def hook(fn):
    HOOKS[fn.__name__] = fn
    return fn

@hook
def discover(ctx):
    return {"tools": []}

@hook
def mutate_request(ctx):
    return {"system": ["you are a helpful assistant."]}

if __name__ == "__main__":
    h = HOOKS.get(sys.argv[1])
    if not h:
        print(json.dumps({"error": f"unknown hook: {sys.argv[1]}"}))
        sys.exit(1)
    ctx = json.loads(sys.stdin.read() or "{}")
    result = h(ctx)
    for key, value in result.items():
        print(json.dumps({key: value}), flush=True)
```
