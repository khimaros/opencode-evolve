# roadmap

## in progress

## todo

- [ ] hook protocol — `before_tool.deny` short-circuit. opencode-evolve currently logs the deny intent but cannot actually short-circuit a tool call from `tool.execute.before`. needs upstream opencode plugin API.
- [ ] hook protocol — `mutate_request.tools` payload. opencode's plugin API does not expose the tool list at the relevant hook point; needs a different hook point or upstream work.
- [ ] hook protocol — `on_error` stage. needs upstream emission point in opencode.
- [ ] hook protocol — `on_permission` stage. needs upstream API; opencode's permission engine is internal.
- [ ] hook protocol — `before_turn` / `after_turn` stages. need upstream emission points in opencode.

## done

- [x] sibling project `pi-evolve` at `../pi-evolve/` — first cut of pi-coding-agent extension implementing the hook protocol; examples/ and tests/ symlinked to opencode-evolve
- [x] extract host-neutral hook protocol spec from README so other agent harnesses (e.g. pi) can implement the same contract (now lives at https://github.com/khimaros/hcp-spec/)
- [x] tighten integration test: assert build-request and heartbeat system prompts match the exact content the hello example should produce (preamble + stage + env); apply heartbeat hook's returned system prompt to the heartbeat session

- [x] extend opencode integration test to cover the heartbeat flow (stall the build request so a heartbeat tick fires, then assert on the captured heartbeat chat/completions)
- [x] end-to-end test that captures the actual LLM request (system prompt + tool defs) produced by real opencode when the evolve plugin is loaded, via a mock openai-compatible server
- [x] add timezone parameter to evolve_datetime tool (default: UTC)
- [x] log full tool call results (especially errors) instead of 200-char truncated preview
- [x] missing/unhandled hooks should not produce error logs or trigger recovery
- [x] fine-grained permissions for builtin tools (hook_write, hook_edit) and hook-defined tools
- [x] composable multi-hook support (autodiscovery, registration via discover, serial merge)
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
