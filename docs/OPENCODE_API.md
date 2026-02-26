# OpenCode API/SDK Reference

reference for the opencode plugin and SDK APIs used by the persona plugin.
source: `~/src/github.com/anomalyco/opencode/`

## SDK client

```typescript
import { createOpencodeClient } from "@opencode-ai/sdk"

// unscoped client (falls back to process.cwd() on server)
const client = createOpencodeClient({ baseUrl: "http://localhost:4096" })

// directory-scoped client (adds x-opencode-directory header to all requests)
const client = createOpencodeClient({ baseUrl: "http://localhost:4096", directory: "/path/to/project" })
```

the `x-opencode-directory` header determines which project context the server uses.
the server middleware resolves directory from: query param > header > process.cwd().

## plugin API

```typescript
import type { Plugin, PluginInput } from "@opencode-ai/plugin"

// PluginInput fields:
// - client: directory-scoped OpencodeClient (scoped to Instance.directory)
// - project: { id, worktree, vcsDir?, vcs?, time }
// - directory: string (current working directory)
// - worktree: string (project root)
// - serverUrl: URL (server base URL, useful for creating additional clients)
// - $: BunShell
```

**important**: the plugin's `client` is scoped to a single directory/project.
to access sessions across all projects, create an unscoped client from `serverUrl`
and enumerate projects via `client.project.list()`.

### plugin initialization

plugins are loaded once per project (Instance). the plugin function receives
`PluginInput` and returns a `Hooks` object. the plugin's `client` uses internal
fetch (`Server.App().fetch()`) — not HTTP. external clients created from
`serverUrl` use real HTTP.

```typescript
// packages/opencode/src/plugin/index.ts
const client = createOpencodeClient({
  baseUrl: "http://localhost:4096",
  directory: Instance.directory,
  fetch: async (...args) => Server.App().fetch(...args),  // internal, not HTTP
})
```

plugins are loaded from:
1. internal plugins (codex, copilot, gitlab auth)
2. builtin npm plugins (`opencode-anthropic-auth@0.0.13`)
3. user config `plugin` array (npm packages or `file://` paths)

### plugin trigger mechanism

```typescript
// Plugin.trigger() iterates all loaded hooks sequentially
export async function trigger<Name>(name: Name, input: Input, output: Output): Promise<Output> {
  for (const hook of hooks) {
    const fn = hook[name]
    if (!fn) continue
    await fn(input, output)  // hooks mutate output in place
  }
  return output
}
```

hooks mutate `output` in place. multiple plugins can chain — each sees the
previous plugin's mutations.

## sessions

### directory scoping

sessions are stored per-project. `Session.list()` only returns sessions for the
current `Instance.project`. each project is identified by its directory/worktree.

to list sessions across all projects:
1. `globalClient.project.list()` — returns all projects with `{ id, worktree }`
2. for each project, create a directory-scoped client and call `session.list()`

### create

```typescript
const session = await client.session.create({ body: { title: "my session" } })
// session.data: { id, title, directory, projectID, time, ... }
```

### list

```typescript
const sessions = await client.session.list()
// sessions.data: Array<Session>
// Session: { id, slug, title, directory, projectID, parentID?, time: { created, updated, archived? }, ... }
```

no server-side filtering by title in the SDK — filter client-side.

### status

```typescript
const statuses = await client.session.status()
// statuses.data: Record<sessionID, { type: "idle" } | { type: "busy" } | { type: "retry", attempt, message, next }>
```

sessions absent from the status map are idle.

### messages

```typescript
const msgs = await client.session.messages({
  path: { id: sessionId },
  query: { limit: 10 },
})
// msgs.data: Array<{ info: UserMessage | AssistantMessage, parts: Part[] }>
```

message info fields:
- `role`: "user" | "assistant"
- `agent`: string (e.g. "per")
- `model?`: { providerID, modelID }
- `time`: { created }
- assistant-only: `finish`, `tokens`, `cost`, `error`, `parentID`

### prompt (synchronous, blocks until LLM responds)

```typescript
const result = await client.session.prompt({
  path: { id: sessionId },
  body: {
    agent: "per",
    model: { providerID: "...", modelID: "..." },  // optional
    parts: [{ type: "text", text: "hello", synthetic: true }],
  },
})
// result.data: { info: AssistantMessage, parts: Part[] }
```

### promptAsync (fire-and-forget, returns 204 immediately)

```typescript
await client.session.promptAsync({
  path: { id: sessionId },
  body: { /* same as prompt */ },
})
```

## message parts

### TextPartInput (for sending)

```typescript
{ type: "text", text: string, synthetic?: boolean, ignored?: boolean }
```

- `synthetic: true` marks automated/system-generated messages (detectable in history)
- `ignored: true` excludes the part from model input

### response parts

- `TextPart`: { type: "text", text, synthetic?, ignored? }
- `ToolPart`: { type: "tool", tool, state }
- `ReasoningPart`: { type: "reasoning", text }
- `FilePart`: { type: "file", mime, url, filename? }
- plus: SnapshotPart, PatchPart, AgentPart, StepStartPart, StepFinishPart, etc.

## projects

```typescript
const projects = await client.project.list()
// projects.data: Array<{ id, worktree, vcsDir?, vcs?, time: { created, initialized? } }>
```

`worktree` is the directory path used for creating directory-scoped clients.

## plugin hooks (complete reference)

all hooks from `@opencode-ai/plugin` `Hooks` interface.
source: `packages/plugin/src/index.ts`

### event

```typescript
event?: (input: { event: Event }) => Promise<void>
```

fires for every bus event. used to observe session/message lifecycle.

### config

```typescript
config?: (input: Config) => Promise<void>
```

called once after plugin init with the full opencode config.

### auth

```typescript
auth?: AuthHook  // { provider, loader?, methods[] }
```

register custom auth providers (oauth or api key). see copilot/codex plugins
for examples.

### chat.message

```typescript
"chat.message"?: (
  input: { sessionID: string, agent?: string, model?: { providerID: string, modelID: string }, messageID?: string, variant?: string },
  output: { message: UserMessage, parts: Part[] },
) => Promise<void>
```

fires AFTER the LLM responds. `output.parts` contains the assistant's response
(text, tool calls, reasoning, etc.).

trigger site: `packages/opencode/src/session/prompt.ts`

### chat.params

```typescript
"chat.params"?: (
  input: { sessionID: string, agent: string, model: Model, provider: ProviderContext, message: UserMessage },
  output: { temperature: number, topP: number, topK: number, options: Record<string, any> },
) => Promise<void>
```

modify LLM request parameters (temperature, topP, etc.) before the call.

trigger site: `packages/opencode/src/session/llm.ts`

### chat.headers

```typescript
"chat.headers"?: (
  input: { sessionID: string, agent: string, model: Model, provider: ProviderContext, message: UserMessage },
  output: { headers: Record<string, string> },
) => Promise<void>
```

inject custom HTTP headers into the **outbound LLM API request** (e.g. to
anthropic, openai). used by copilot plugin for `anthropic-beta` header, codex
plugin for `User-Agent`.

**NOTE**: this does NOT receive inbound HTTP headers from the client/proxy.
it only provides headers to send to the LLM provider.

trigger site: `packages/opencode/src/session/llm.ts`

### permission.ask

```typescript
"permission.ask"?: (
  input: Permission,  // { sessionID, id, callID, tool, args, metadata, ... }
  output: { status: "ask" | "deny" | "allow" },
) => Promise<void>
```

intercept tool permission checks. set `output.status` to auto-allow or
auto-deny without user prompt.

trigger site: `packages/opencode/src/permission/next.ts`

### command.execute.before

```typescript
"command.execute.before"?: (
  input: { command: string, sessionID: string, arguments: string },
  output: { parts: Part[] },
) => Promise<void>
```

fires before a slash command executes. can modify the output parts.

### tool.execute.before

```typescript
"tool.execute.before"?: (
  input: { tool: string, sessionID: string, callID: string },
  output: { args: any },
) => Promise<void>
```

fires before a tool executes. can modify `output.args` to alter tool input.

trigger site: `packages/opencode/src/session/prompt.ts`

### tool.execute.after

```typescript
"tool.execute.after"?: (
  input: { tool: string, sessionID: string, callID: string },
  output: { title: string, output: string, metadata: any },
) => Promise<void>
```

fires after a tool executes. can modify the tool result before it's returned
to the LLM.

trigger site: `packages/opencode/src/session/prompt.ts`

### shell.env

```typescript
"shell.env"?: (
  input: { cwd: string },
  output: { env: Record<string, string> },
) => Promise<void>
```

inject environment variables into bash/shell tool execution and pty sessions.
the returned env is merged with `process.env`.

trigger sites: `packages/opencode/src/tool/bash.ts`, `packages/opencode/src/pty/index.ts`

### experimental.chat.system.transform

```typescript
"experimental.chat.system.transform"?: (
  input: { sessionID?: string, model: Model },
  output: { system: string[] },
) => Promise<void>
```

modify or replace the system prompt. `output.system` is an array of strings
that will be joined. if the plugin empties the array, the original system
prompt is restored (fallback).

the system prompt is composed from (in order):
1. agent prompt OR provider prompt
2. custom system strings from the `stream()` call
3. per-message system from `input.user.system`

then this hook fires and can replace everything.

trigger site: `packages/opencode/src/session/llm.ts`

### experimental.chat.messages.transform

```typescript
"experimental.chat.messages.transform"?: (
  input: {},
  output: { messages: { info: Message, parts: Part[] }[] },
) => Promise<void>
```

modify the message history before it's sent to the LLM. can filter, rewrite,
or inject messages. input is empty — all context is in `output.messages`.

trigger site: `packages/opencode/src/session/prompt.ts`

### experimental.session.compacting

```typescript
"experimental.session.compacting"?: (
  input: { sessionID: string },
  output: { context: string[], prompt?: string },
) => Promise<void>
```

customize the compaction prompt.
- `context`: additional strings appended to the default compaction prompt
- `prompt`: if set, replaces the default compaction prompt entirely

trigger site: `packages/opencode/src/session/compaction.ts`

### experimental.text.complete

```typescript
"experimental.text.complete"?: (
  input: { sessionID: string, messageID: string, partID: string },
  output: { text: string },
) => Promise<void>
```

fires when a text part finishes streaming. can modify the final text.

trigger site: `packages/opencode/src/session/processor.ts`

## HTTP server architecture

### framework

opencode uses Hono on Bun. the server is a single `Hono` app.
source: `packages/opencode/src/server/server.ts`

### middleware chain (in order)

1. **error handler**: catches `NamedError`, `HTTPException`, generic errors
2. **basic auth**: optional, enabled when `OPENCODE_SERVER_PASSWORD` is set
   (username defaults to `"opencode"`, override with `OPENCODE_SERVER_USERNAME`)
3. **request logger**: logs method + path for all requests (except `/log`)
4. **CORS**: allows `localhost:*`, `127.0.0.1:*`, `tauri://localhost`,
   `*.opencode.ai` (https), plus custom whitelist
5. **global routes**: mounted at `/global` (before directory scoping)
6. **auth routes**: `PUT/DELETE /auth/:providerID`
7. **directory scoping middleware**: resolves directory from
   `query.directory > header x-opencode-directory > process.cwd()`,
   runs remaining handlers inside `Instance.provide({ directory })`
8. **all other routes**: `/project`, `/session`, `/pty`, `/mcp`, `/file`,
   `/config`, `/experimental`, `/provider`, `/permission`, `/question`

### context system

opencode uses `AsyncLocalStorage` (node:async_hooks) for request-scoped state.
`Instance.provide()` sets the current directory/project context for the
duration of a request. all downstream code (session, plugin, tools) reads
from `Instance.directory`, `Instance.project`, etc.

**important**: HTTP request headers are NOT stored in the async context.
they are only available within the Hono route handler scope (`c.req.header()`).

### request flow: HTTP → plugin hook

```
POST /:sessionID/message  (packages/opencode/src/server/routes/session.ts:697)
  ├── c.req (Hono Context — has HTTP headers, URL, etc.)
  ├── SessionPrompt.prompt({ ...body, sessionID })      (prompt.ts)
  │     ├── SessionProcessor.create().process()          (processor.ts)
  │     │     └── LLM.stream(input)                      (llm.ts:46)
  │     │           ├── Plugin.trigger("experimental.chat.system.transform",
  │     │           │     { sessionID, model },           ← NO HTTP context
  │     │           │     { system })
  │     │           ├── Plugin.trigger("chat.params", ...)
  │     │           ├── Plugin.trigger("chat.headers", ...)  ← outbound to LLM only
  │     │           └── streamText(...)                   → LLM provider
  │     └── Plugin.trigger("chat.message", ...)          ← after LLM response
  └── stream.write(JSON.stringify(msg))
```

HTTP headers are available at the route handler level (`c.req.header()`) but
are NOT threaded through `SessionPrompt.prompt()` → `LLM.stream()` →
`Plugin.trigger()`. no plugin hook currently receives inbound HTTP headers.

### key HTTP routes

| method | path | operationId | description |
|--------|------|-------------|-------------|
| POST | `/:sessionID/message` | `session.prompt` | send message (streaming response) |
| POST | `/:sessionID/prompt_async` | `session.prompt_async` | send message (fire-and-forget, 204) |
| POST | `/session` | `session.create` | create session |
| GET | `/session` | `session.list` | list sessions |
| GET | `/session/status` | `session.status` | session status map |
| GET | `/:sessionID/message` | `session.messages` | list messages |
| GET | `/project` | `project.list` | list projects |
| GET | `/global/event` | — | SSE event stream |
| GET | `/doc` | — | OpenAPI spec |

### environment flags

| flag | description |
|------|-------------|
| `OPENCODE_SERVER_PASSWORD` | enable basic auth |
| `OPENCODE_SERVER_USERNAME` | basic auth username (default: `"opencode"`) |
| `OPENCODE_DISABLE_DEFAULT_PLUGINS` | skip builtin plugins |
| `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` | override max output tokens (default: 32000) |

## events (SSE)

messages sent via `client.session.prompt()` flow through the same Bus event path
as user-typed messages. the TUI and webui subscribe to `/global/event` (SSE) and
automatically display messages in the correct session.

key event types: `message.updated`, `message.part.updated`, `session.status`,
`session.created`, `session.updated`.

## webui directory scoping

the webui encodes the directory in the URL path as base64: `/{base64dir}/session/{id}`.
it creates per-directory SDK clients dynamically. the global webui SDK (at root `/`)
has no directory scoping.

## source file index

key files in `packages/opencode/src/`:

| file | description |
|------|-------------|
| `server/server.ts` | Hono app, middleware chain, route mounting |
| `server/routes/session.ts` | session CRUD, message send, SSE events |
| `plugin/index.ts` | plugin loader, `Plugin.trigger()` |
| `session/llm.ts` | LLM streaming, system prompt assembly, hook triggers |
| `session/prompt.ts` | prompt loop, tool execution, message transforms |
| `session/processor.ts` | stream processing, text.complete hook |
| `session/compaction.ts` | session compaction logic |
| `session/system.ts` | base system prompt generation |
| `util/context.ts` | `AsyncLocalStorage` context helper |
| `project/instance.ts` | `Instance` namespace (directory, project, provide) |

plugin type definitions: `packages/plugin/src/index.ts`
