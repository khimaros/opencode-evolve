# agent hook protocol v1

a host-agnostic contract for plugging external lifecycle hooks into a coding-agent harness. opencode-evolve is the reference implementation; this document defines the on-the-wire shape so other harnesses (pi, aider, goose, …) can adopt it and run the same hook scripts unchanged.

this is a deliberately minimal spec: subprocess fork per event, JSON in, JSONL out. no long-running daemon, no shared library, no language binding. any executable that can read stdin and write stdout qualifies.

## terms

- **host** — the agent harness embedding the protocol (e.g. opencode, pi).
- **hook script** — an executable file under `$WORKSPACE/hooks/` that the host invokes per lifecycle event.
- **stage** — a named lifecycle event (`mutate_request`, `idle`, `heartbeat`, …).
- **workspace** — a per-user directory containing `hooks/`, `prompts/`, `tests/`, `config/`, `state/`. canonical layout below.

## workspace layout

```
$WORKSPACE/
├── config/
│   └── evolve.jsonc        # host-extension settings
├── state/
│   └── evolve.json         # runtime state (host-managed)
├── hooks/                  # executable hook scripts (autodiscovered)
├── prompts/                # contract prompt files (see § prompt contract)
└── tests/                  # per-hook validation scripts
```

hosts may use a different state filename, but the rest is normative.

## invocation

the host calls each hook script as:

```
$WORKSPACE/hooks/<script> <stage>
```

- argv[1] is the stage name.
- stdin is a single JSON object (`{"hook": "<stage>", ...ctx}`), terminated by EOF.
- stdout is JSONL — each line is a JSON object.
- stderr is forwarded to the host's debug log.
- exit code 0 = success; non-zero = failure (triggers `recover` unless the failing stage is observational).

scripts under `hooks/` whose basename starts with `.` or `__` are ignored. discovery order is alphabetical.

### output framing

each line of stdout MUST be a valid JSON object. recognized keys are merged into the hook result; lines with `{"log": "..."}` are routed to the host's debug log and not merged.

### composability

multiple hook scripts run serially in alphabetical order. results merge across all scripts:

- arrays (`system`, `tools`, `notifications`, `actions`, `modified`) are concatenated
- scalars (`continue`, `prompt`, `user`, `message`, `result`) are joined with newline
- a script's failure triggers `recover` for that script; other scripts continue regardless

## stages

every stage receives `prompts` in the input context — a dict mapping contract stage names to the contents of the corresponding file in `prompts/`. missing files resolve to empty strings. hosts MUST inject this on every call.

### `discover`

called once at host startup per script. registers the hook and its custom tools.

input: `{"hook": "discover"}`

output:
```json
{
  "name": "persona",
  "test": "persona_test.py",
  "tools": [
    {"name": "trait_read", "description": "...", "parameters": {...}, "permission": {"arg": "trait"}}
  ]
}
```

- `name` (optional) — tool prefix and hook id; defaults to filename stem. tools are exposed to the agent as `<name>_<tool_name>`.
- `test` (optional) — validation script under `tests/` used by builtin `<prefix>_hook_write` / `<prefix>_hook_edit` tools. omitted = no validation on writes.
- `tools[].parameters` — three accepted forms:
  - string shorthand: `{"arg": "description"}` → string param.
  - typed: `{"arg": {"type": "string", "description": "...", "optional": true}}`.
  - enum: `{"arg": {"type": "string", "enum": ["a", "b"], "description": "..."}}`. invalid values MUST be rejected by the host before reaching `execute_tool`.
  - supported types: `string`, `number`, `boolean`, `object`, `array`, `any`.
- `tools[].permission.arg` (optional) — name (or list of names) of the parameter whose value is used as the permission pattern key. tools without `permission` use pattern `"*"`.

### `mutate_request`

called once per session at first request. host caches the returned system prompt for the session lifetime.

input: `{"hook": "mutate_request", "session": {"id": "..."}, "history": [...]}`
output: `{"system": ["..."]}` or `{}` (use contract defaults).

### `observe_message`

observational. called after each assistant response. failure does NOT trigger `recover`.

input: `{"hook": "observe_message", "session": {"id": "...", "agent": "..."}, "thinking": "...", "calls": [...], "answer": "..."}`
output: `{"modified": [...], "notify": [...], "actions": [...]}`

### `idle`

called when the agent produces a final response with no tool calls. return `{"continue": "..."}` to push a synthetic user message and keep the session running, or `{}` to let it stop.

input: `{"hook": "idle", "session": {"id": "...", "agent": "..."}, "answer": "..."}`
output: `{}` or `{"continue": "follow-up text"}`

### `heartbeat`

called by the host on a timer. host may run heartbeats in a dedicated session (see § heartbeat semantics).

input: `{"hook": "heartbeat", "sessions": [...], "history": [...]}`
output: `{"system": ["..."], "user": "heartbeat prompt"}`

### `compacting`

called when the host compacts a session. return a custom prompt or `{}` to apply the contract default (`compaction.md`).

input: `{"hook": "compacting", "session": {"id": "..."}, "history": [...]}`
output: `{"prompt": "..."}` or `{}`

### `format_notification`

observational. format pending notifications before injecting them as a synthetic user message.

input: `{"hook": "format_notification", "session": {"id": "..."}, "notifications": [...]}`
output: `{"message": "[update] modified: FOO.md"}`

### `recover`

called when a non-observational hook script fails. emits an emergency system + user message into the affected session.

input: `{"hook": "recover", "error": "...", "failed_hook": "..."}`
output: `{"system": ["..."], "user": "..."}`

### `execute_tool`

called when the agent invokes a tool registered via `discover`.

input: `{"hook": "execute_tool", "tool": "<tool_name>", "args": {...}}`
output: `{"result": "...", "modified": [...], "notify": [...]}`

### `tool_before` / `tool_after`

observational. wrap any host-side tool execution (built-in or hook-registered).

input (before): `{"hook": "tool_before", "session": {...}, "tool": "...", "callID": "...", "args": {...}}`
input (after):  `{"hook": "tool_after",  "session": {...}, "tool": "...", "callID": "...", "title": "...", "output": "..."}`

## prompt contract

the host loads files in `prompts/` and injects their contents into every hook call as `ctx.prompts`. the contract defines a fixed set of stage files and the default behavior the host MUST apply when the corresponding hook returns nothing.

| stage | file | applied at | default if hook returns `{}` |
|-------|------|------------|------------------------------|
| `preamble` | `preamble.md` | prepended to every system prompt | — (composed by hooks) |
| `chat` | `chat.md` | `mutate_request` | system = `[preamble, chat]` |
| `heartbeat` | `heartbeat.md` | `heartbeat` | system = `[preamble, heartbeat]`, user = `heartbeat` content |
| `compaction` | `compaction.md` | `compacting` | prompt = `compaction` content |
| `recover` | `recover.md` | `recover` | system = `[preamble, recover]`, user = `recover` content |

builtin `<prefix>_prompt_*` tools MUST be enum-constrained to these filenames; hook scripts cannot read or write prompt files outside the contract.

## builtin tools

hosts MUST register the following tools per discovered hook (using its registration `name` as prefix). these tools operate directly on the workspace filesystem; they do not invoke `execute_tool`.

- `<prefix>_datetime` — current UTC datetime; accepts optional `timezone` parameter.
- `<prefix>_heartbeat_time` — last heartbeat runtime in UTC.
- `<prefix>_prompt_list` / `_read` / `_write` / `_edit` — manage existing prompts; cannot create or delete.
- `<prefix>_hook_list` / `_read` / `_write` / `_edit` — manage existing hook scripts; cannot create or delete. writes are validated against the registered `test` script when configured.
- `<prefix>_hook_validate` — run a hook's test script against proposed content without installing.

## actions

hooks MAY return an `actions` array to request side effects. hosts MUST implement at least:

```json
{"actions": [
  {"type": "send", "session_id": "...", "message": "...", "synthetic": true},
  {"type": "create_session", "title": "..."}
]}
```

unsupported action types MUST be ignored, not error.

## heartbeat semantics

- the host owns the timer (interval is host-configurable, default 30 min).
- heartbeats run in a dedicated session, configurable by title and agent id.
- heartbeats MUST be skipped while another session is busy in the same workspace, and MUST NOT overlap with a still-running prior heartbeat.
- cleanup modes (`none`, `new`, `archive`, `compact`) and thresholds (`heartbeat_cleanup_count`, `heartbeat_cleanup_tokens`) are host-configurable; thresholds evaluate independently and the first to trip wins.

## git integration

the host SHOULD auto-initialize the workspace as a git repo, take an initial snapshot of pre-existing files, and commit changes after each tool execution and heartbeat. this is a strong recommendation, not a hard requirement — clients of `<prefix>_hook_*` and `<prefix>_prompt_*` tools rely on the resulting diff history.

## conformance

a host is "v1-conformant" if:

1. it discovers and invokes hooks exactly as specified in § invocation.
2. it implements every stage in § stages, including the observational/recover semantics.
3. it injects `ctx.prompts` on every hook call and applies the defaults in § prompt contract.
4. it registers the builtin tools in § builtin tools per discovered hook.
5. it supports at minimum the `send` and `create_session` action types.
6. it owns the heartbeat timer per § heartbeat semantics.

partial conformance is permitted but MUST be documented (e.g. "observation-only: no heartbeat, no idle continuation"). hooks SHOULD degrade gracefully when stages are not invoked.
