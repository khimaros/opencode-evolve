---
description: example agent for the evolve hello plugin, with a gated mutate_request
mode: primary
---

this is a placeholder prompt which will be replaced dynamically by the
evolve plugin's mutate_request hook, gated on the marker below.

the marker matches `agent_marker` in the evolve config (overridable via
EVOLVE_AGENT_MARKER); without it, opencode-evolve abstains and the hook
is never called for this path.

<~ EVOLVE AGENT MARKER ~>
