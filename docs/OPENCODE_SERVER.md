# opencode server

source: https://opencode.ai/docs/server/

## overview

the `opencode serve` command runs a headless HTTP server exposing an
OpenAPI endpoint for client interaction.

## usage

```
opencode serve [--port <number>] [--hostname <string>] [--cors <origin>]
```

### options

| flag | description | default |
|------|-------------|---------|
| `--port` | port to listen on | `4096` |
| `--hostname` | hostname to listen on | `127.0.0.1` |
| `--mdns` | enable mDNS discovery | `false` |
| `--mdns-domain` | custom domain for mDNS service | `opencode.local` |
| `--cors` | additional browser origins to allow | `[]` |

multiple CORS origins can be specified by repeating the flag.

## authentication

set `OPENCODE_SERVER_PASSWORD` for HTTP basic auth protection. username
defaults to `opencode` or can be overridden with `OPENCODE_SERVER_USERNAME`.

## how it works

when running `opencode`, a TUI client communicates with a server that
publishes an OpenAPI 3.1 specification. this architecture supports multiple
clients and programmatic interaction.

the `/tui` endpoint allows driving the TUI remotely, used by IDE plugins.

## API specification

view the OpenAPI spec at `http://<hostname>:<port>/doc`

## available APIs

- **global** — health checks and event streams
- **project** — list and retrieve current project
- **path & VCS** — current path and version control information
- **instance** — dispose current instance
- **config** — retrieve and update configuration
- **provider** — authentication and provider management
- **sessions** — create, manage, and interact with sessions
- **messages** — send messages and retrieve conversation history
- **commands** — execute slash commands and shell operations
- **files** — search files, read content, get status
- **tools** — access experimental tool functionality
- **LSP/formatters/MCP** — server status monitoring
- **agents** — list available agents
- **logging** — write log entries
- **TUI** — remote control of the TUI interface
- **auth** — set provider credentials
- **events** — server-sent events stream
- **docs** — OpenAPI specification endpoint
