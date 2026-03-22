# roadmap

## in progress

## done

- [x] rename heartbeat_model to model in config for consistency with state
- [x] env var overrides for config (EVOLVE_* prefix)
- [x] builtin tool to get last heartbeat runtime in UTC
- [x] fix heartbeat cleanup (delete/archive) by checking for API errors
- [x] prevent heartbeat cleanup of busy sessions
- [x] archive cleanup mode for heartbeat sessions
- [x] prevent overlapping heartbeat executions
- [x] skip heartbeat when other sessions are active

- [x] set cwd to WORKSPACE for all hook spawns (including heartbeat)
- [x] scope session operations (heartbeat, actions) to WORKSPACE directory

- [x] initial commit of pre-existing files when git repo is first created
