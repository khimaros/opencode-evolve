# opencode recent projects

source: https://github.com/anomalyco/opencode

## overview

the "recent projects" list in the opencode web UI is not configurable via
`opencode.jsonc`, plugins, or agent config. projects are registered
server-side when any API call targets a directory, and persisted permanently.

## how projects are registered

every API request passes through a middleware that resolves the target
directory in priority order:

1. `?directory=` query parameter
2. `x-opencode-directory` header
3. `process.cwd()` (server working directory)

see: `packages/opencode/src/server/server.ts:187-204`

the middleware calls `Instance.provide()` which calls
`Project.fromDirectory(directory)`:

see: `packages/opencode/src/project/instance.ts:22-43`

`fromDirectory()` walks up the filesystem looking for `.git`:

- if `.git` found: derives project ID from git root commit hash
- if NOT found: falls back to `id: "global"` with `worktree: "/"`
- stores the result in `~/.local/share/opencode/project/`

see: `packages/opencode/src/project/project.ts:53-176`

the global fallback (line 170-175):
```
return {
  id: "global",
  worktree: "/",
  sandbox: "/",
  vcs: Info.shape.vcs.parse(Flag.OPENCODE_FAKE_VCS),
}
```

`Project.list()` returns everything in storage with no filtering:

see: `packages/opencode/src/project/project.ts:281-288`

## the global "/" project

any directory without a `.git` repo anywhere up the tree gets mapped to
the "global" project with `worktree: "/"`. this is why "/" appears in
the recent projects list.

common causes:

- `opencode serve` is run from a directory without `.git` — any client
  request that omits the directory param falls back to `process.cwd()`,
  which resolves as global
- a plugin creates an opencode SDK client scoped to a directory that has
  no git repo yet (e.g. workspace dir before git init)

fix: ensure `.git` exists in the target directory before creating the
client, and run `opencode serve` from inside a git repo.

## server working directory

the `opencode serve` command does NOT initialize any project at startup.
project initialization is deferred to the first API request via middleware.

see: `packages/opencode/src/cli/cmd/serve.ts`
see: `packages/opencode/src/server/server.ts:566-612` (Server.listen)

however, CWD is the fallback directory for any request that doesn't
specify a directory explicitly. if the server is started from a non-git
directory (e.g. `/`, `/home/user`), the first such request creates the
global project permanently.

## storage layers

| layer | location | format |
|-------|----------|--------|
| server state | `~/.local/share/opencode/project/` | per-project metadata |
| browser cache | localStorage key `server.v3` | `{ list, projects, lastProject }` |
| TUI frecency | `~/.local/state/opencode/frecency.jsonl` | `{ path, frequency, lastOpen }` |

## web UI

the frontend fetches projects from the `/project/list` API on startup:

see: `packages/app/src/context/global-sync/bootstrap.ts:61-69`

the `server.projects` context (browser localStorage) provides:

- `list()` — all projects for the current server
- `open(directory)` — add a project
- `close(directory)` — remove a project
- `touch(directory)` — mark as last accessed
- `last()` — get the last accessed project
- `move(directory, toIndex)` — reorder via drag-and-drop
- `expand(directory)` / `collapse(directory)` — toggle workspace tree

see: `packages/app/src/context/server.tsx`

## removing unwanted projects

to remove a project (e.g. "/") from the list:

1. close it from the web UI sidebar, OR
2. remove the entry from `~/.local/share/opencode/project/` (e.g. `global`), OR
3. clear the `server.v3` key from browser localStorage

note: if the root cause is not fixed, the project will reappear on next
API request that targets a non-git directory.
