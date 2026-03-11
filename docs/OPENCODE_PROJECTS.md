# opencode recent projects

## overview

the "recent projects" list in the opencode web UI is not configurable via
`opencode.jsonc`, plugins, or agent config. it is managed through a
combination of server-side state and client-side browser storage.

## storage layers

| layer | location | format |
|-------|----------|--------|
| server state | `~/.local/share/opencode/project/` | per-project metadata |
| browser cache | localStorage key `server.v3` | `{ list, projects, lastProject }` |
| TUI frecency | `~/.local/state/opencode/frecency.jsonl` | `{ path, frequency, lastOpen }` |

## how projects are added

projects are tracked by the opencode server when opened. the web UI
fetches the list via the `/project/list` API endpoint during bootstrap.

when localStorage is empty, the default is `{ list: [], projects: {}, lastProject: {} }`.

## web UI project API

the `server.projects` context provides:

- `list()` — all projects for the current server
- `open(directory)` — add a project
- `close(directory)` — remove a project
- `touch(directory)` — mark as last accessed
- `last()` — get the last accessed project
- `move(directory, toIndex)` — reorder via drag-and-drop
- `expand(directory)` / `collapse(directory)` — toggle workspace tree

## removing unwanted projects

to remove a project (e.g. "/") from the list:

1. close it from the web UI sidebar, OR
2. remove the entry from `~/.local/share/opencode/project/`, OR
3. clear the `server.v3` key from browser localStorage
