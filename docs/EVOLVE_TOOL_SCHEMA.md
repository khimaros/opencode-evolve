# evolve hook tool schema pipeline

how hook tool definitions in python become LLM tool schemas.

## pipeline

1. **python function** — `@tool` decorator registers a function in `TOOLS` dict
2. **`tool_defs()`** (`persona.py`) — introspects type hints via `get_type_hints(..., include_extras=True)`, extracts `Annotated` metadata into parameter specs
3. **`discover` hook** — returns `{"tools": tool_defs()}` to the plugin
4. **`buildToolArgs()`** (`opencode-evolve/src/index.ts:417`) — converts each parameter spec into an opencode `tool.schema.*` call
5. **`parseTypeSchema()`** (`opencode-evolve/src/index.ts:384`) — maps the `type` string from `param()` to a schema builder

## type mapping (parseTypeSchema)

| python `param(type=...)` | opencode schema | JSON Schema effect |
|---|---|---|
| `"string"` (default) | `tool.schema.string()` | `{"type": "string"}` |
| `"number"` | `tool.schema.number()` | `{"type": "number"}` |
| `"boolean"` | `tool.schema.boolean()` | `{"type": "boolean"}` |
| `"object"` | `tool.schema.record(string, any)` | `{"type": "object", "additionalProperties": {}}` |
| `"array"` | `tool.schema.array(any)` | `{"type": "array"}` |
| `"array[string]"` | `tool.schema.array(string)` | `{"type": "array", "items": {"type": "string"}}` |
| `"object[string, number]"` | `tool.schema.record(string, number)` | `{"type": "object", "additionalProperties": {"type": "number"}}` |
| `"any"` | `tool.schema.any()` | `{}` |
| bare string (no `param()`) | `tool.schema.string()` | `{"type": "string"}` |

## key detail: `type="object"` becomes `record(string, any)`

the `object` type produces a JSON Schema with `additionalProperties: {}` (any
value type). this means the LLM sees a generic map from string keys to
unconstrained values. there is **no per-field schema** — the LLM relies
entirely on the description string and examples to understand the expected
shape.

this is why the `fields` parameter on `record_append` is vulnerable to
misformatting: the LLM may wrap values in extra objects (e.g.
`{"type": {"type": "observation"}}` instead of `{"type": "observation"}`).
clear examples and explicit "do NOT wrap" language in the description are the
main defense.

## optional parameters

`param(..., optional=True)` calls `.optional()` on the schema, which adds the
parameter to the tool schema but marks it as not required.

## parameter resolution order

in `buildToolArgs()`:
- if the spec is a bare string → `tool.schema.string().describe(spec)`
- if the spec is a dict (from `param()`) → extract `type`, `description`, `optional`, then call `parseTypeSchema(type, description)`

## source locations

- `param()` helper: `server/workspace/hooks/persona.py:46`
- `tool_defs()`: `server/workspace/hooks/persona.py:887`
- `buildToolArgs()`: `opencode-evolve/src/index.ts:417`
- `parseTypeSchema()`: `opencode-evolve/src/index.ts:384`
- type map: `opencode-evolve/src/index.ts:397-404`
