#!/usr/bin/env python3
"""end-to-end test: capture the actual LLM request opencode sends when the
evolve plugin is loaded, via a mock openai-compatible server.

strategy:
  1. build the plugin (dist/index.js)
  2. spawn a local http server impersonating an openai chat-completions endpoint
  3. write an opencode.json in a temp workspace that:
       - defines a custom provider (npm defaults to @ai-sdk/openai-compatible)
         whose baseURL points at our mock server
       - registers dist/index.js as a plugin
       - selects mock/mock as both model and small_model
  4. run `opencode run "hello"` against that workspace
  5. assert the captured request body contains a real system prompt and the
     evolve_* tool schemas
  6. dump the full captured payload to tests/.artifacts/ for inspection
"""

import json, os, shlex, shutil, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = PROJECT_ROOT / "tests" / ".artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

# opencode binary selection. this test asserts opencode-side fixes (zod
# cross-instance metadata, enum validation) that the upstream npm release
# may not yet ship, so we refuse to silently fall back to `opencode` on
# PATH — that masks regressions by running against an unrelated build.
#   OPENCODE_BIN=<command>   full command override (space-separated, shlex-parsed)
#   OPENCODE_SRC=<path>      local opencode checkout; runs via `bun run <src>/packages/opencode/src/index.ts`
def resolve_opencode_cmd():
    override = os.environ.get("OPENCODE_BIN")
    if override:
        return shlex.split(override)
    src = os.environ.get("OPENCODE_SRC")
    if src:
        entry = Path(src) / "packages" / "opencode" / "src" / "index.ts"
        if not entry.exists():
            print(f"OPENCODE_SRC set but entry point not found: {entry}", file=sys.stderr)
            sys.exit(2)
        return ["bun", "run", "--conditions=browser", str(entry)]
    print("error: set OPENCODE_BIN=<path-to-opencode> or OPENCODE_SRC=<opencode-checkout>.",
          file=sys.stderr)
    print("       `opencode` on PATH is not used — this test relies on local opencode fixes.",
          file=sys.stderr)
    sys.exit(2)

OPENCODE_CMD = resolve_opencode_cmd()
print(f"opencode command: {' '.join(OPENCODE_CMD)}")

PASS = FAIL = 0

def check(desc, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS: {desc}")
    else:
        FAIL += 1
        print(f"FAIL: {desc}")
        if detail:
            print(f"  {detail}")

# --- mock openai-compatible server ---

captured = []
capture_lock = threading.Lock()

# stall the first build request long enough for at least one heartbeat tick
# to fire inside the still-alive opencode process. heartbeat requests bypass
# the stall so they can complete and be captured.
HEARTBEAT_MS = 500
STALL_SECONDS = 5
stalled_once = False

def is_heartbeat_request(body):
    for m in body.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, str) and "[heartbeat]" in c:
            return True
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and "[heartbeat]" in (p.get("text") or ""):
                    return True
    return False

SSE_RESPONSE = (
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    'data: [DONE]\n\n'
).encode()

class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with capture_lock:
            captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        global stalled_once
        if (not stalled_once
                and "chat/completions" in self.path
                and body.get("tools")
                and not is_heartbeat_request(body)):
            stalled_once = True
            time.sleep(STALL_SECONDS)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(SSE_RESPONSE)

    def do_GET(self):
        # some providers probe /models; return empty list
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[]}')

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def start_mock():
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port

# --- build plugin ---

print("building plugin...")
r = subprocess.run(["npx", "tsc"], cwd=PROJECT_ROOT, capture_output=True, text=True)
if r.returncode != 0:
    print("FAIL: build")
    print(r.stdout); print(r.stderr)
    sys.exit(1)
plugin_path = PROJECT_ROOT / "dist" / "index.js"
check("plugin built", plugin_path.exists(), f"missing: {plugin_path}")

# --- set up workspace ---

workdir = Path(tempfile.mkdtemp(prefix="evolve-llm-test-"))
# seed the project from the hello example so its hook is autodiscovered and
# exposes @tool-annotated functions. WORKSPACE == project_dir so the plugin's
# heartbeat client can reuse projectClient (workspace-scoped client rebuild
# doesn't work under `opencode run`, only `opencode serve --port=`).
hello_src = PROJECT_ROOT / "examples" / "hello"
shutil.copytree(hello_src, workdir / "project")
project_dir = workdir / "project"
evolve_workspace = project_dir
# ensure hook is executable after copy
hook_file = evolve_workspace / "hooks" / "evolve.py"
hook_file.chmod(0o755)

server, port = start_mock()
base_url = f"http://127.0.0.1:{port}/v1"
print(f"mock server on {base_url}")

config = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        "mock": {
            "name": "Mock",
            "options": {"apiKey": "test", "baseURL": base_url},
            "models": {"mock": {"name": "Mock Model"}},
        }
    },
    "model": "mock/mock",
    "small_model": "mock/mock",
    "plugin": [plugin_path.as_uri()],
}
(project_dir / "opencode.json").write_text(json.dumps(config, indent=2))

# fully isolate opencode's global config / data / state from the user's
# environment. opencode merges ~/.config/opencode/opencode.{json,jsonc} into
# every project's config, which otherwise leaks MCP servers, providers,
# plugins, etc. into these tests. point every xdg dir + HOME at a throwaway
# tree so we're running against a pristine opencode install.
fake_home = Path(tempfile.mkdtemp(prefix="evolve-home-"))
for sub in (".config/opencode", ".local/share/opencode",
            ".cache/opencode", ".local/state/opencode"):
    (fake_home / sub).mkdir(parents=True, exist_ok=True)

# start from os.environ for PATH / locale / node binaries, then strip any
# opencode-specific vars that could re-inject outside config.
base_env = {k: v for k, v in os.environ.items()
            if not k.startswith(("OPENCODE_", "XDG_"))}

env = {
    **base_env,
    "HOME": str(fake_home),
    "XDG_CONFIG_HOME": str(fake_home / ".config"),
    "XDG_DATA_HOME": str(fake_home / ".local/share"),
    "XDG_CACHE_HOME": str(fake_home / ".cache"),
    "XDG_STATE_HOME": str(fake_home / ".local/state"),
    "OPENCODE_TEST_HOME": str(fake_home),
    "OPENCODE_EVOLVE_WORKSPACE": str(evolve_workspace),
    "EVOLVE_HEARTBEAT_MS": str(HEARTBEAT_MS),
    # heartbeat tick fires before chat.message captures the model, so seed it
    "EVOLVE_MODEL": "mock/mock",
    # the build session is intentionally stalled — don't let heartbeat skip on it
    "EVOLVE_HEARTBEAT_SKIP_ACTIVE": "false",
    # no custom agents in the test opencode.json — use the builtin
    "EVOLVE_HEARTBEAT_AGENT": "build",
    "OPENAI_API_KEY": "test",
    # avoid opencode trying to autoupdate / share
    "CI": "1",
}

# --- run opencode ---

print("running opencode...")
proc = subprocess.Popen(
    [*OPENCODE_CMD, "run", "--print-logs", "--log-level", "INFO", "hello world"],
    cwd=str(project_dir),
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

# wait up to 90s for two tools-bearing chat-completions requests:
#   1) the real build-agent request (stalled by the mock for STALL_SECONDS)
#   2) the heartbeat tick that fires inside the plugin while the build is stalled
# (opencode also fires title-generation with no tools — ignored)
deadline = time.time() + 90
chat_req = None
hb_req_seen = None
while time.time() < deadline:
    with capture_lock:
        for c in captured:
            if "chat/completions" not in c["path"]:
                continue
            if not c["body"].get("tools"):
                continue
            if is_heartbeat_request(c["body"]):
                hb_req_seen = hb_req_seen or c
            else:
                chat_req = chat_req or c
    if chat_req and hb_req_seen:
        break
    if proc.poll() is not None:
        time.sleep(0.5)
        with capture_lock:
            for c in captured:
                if "chat/completions" in c["path"] and c["body"].get("tools"):
                    if is_heartbeat_request(c["body"]):
                        hb_req_seen = hb_req_seen or c
                    else:
                        chat_req = chat_req or c
        break
    time.sleep(0.2)

if proc.poll() is None:
    proc.terminate()
try:
    stdout, stderr = proc.communicate(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    stdout, stderr = proc.communicate()

(ARTIFACTS / "opencode_integration.stdout.log").write_text(stdout or "")
(ARTIFACTS / "opencode_integration.stderr.log").write_text(stderr or "")

server.shutdown()

check("chat/completions request captured", chat_req is not None,
      f"captured paths: {[c['path'] for c in captured]}")

if not chat_req:
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

# --- dump artifact ---

(ARTIFACTS / "opencode_integration.build_request.json").write_text(json.dumps(chat_req, indent=2, default=str))
print(f"captured request dumped to {ARTIFACTS / 'opencode_integration.build_request.json'}")

body = chat_req["body"]

# --- assertions on system prompt ---

messages = body.get("messages", [])
system_msgs = [m for m in messages if m.get("role") == "system"]
check("has system message(s)", len(system_msgs) > 0, f"messages: {[m.get('role') for m in messages]}")

system_text = "\n".join(
    (m["content"] if isinstance(m.get("content"), str)
     else "".join(p.get("text", "") for p in (m.get("content") or []) if isinstance(p, dict)))
    for m in system_msgs
)
check("system prompt non-empty", len(system_text) > 100, f"len={len(system_text)}")
check("system prompt mentions environment", "<env>" in system_text or "environment" in system_text.lower())
# hello's mutate_request fully replaces the system prompt with its own preamble,
# so we check for the hello hook's contribution rather than opencode's env block
check("system prompt from hello hook present", "note-taking assistant" in system_text,
      f"got: {system_text[:200]}")

# --- assertions on tools ---

tools = body.get("tools", [])
check("tools array present", isinstance(tools, list) and len(tools) > 0, f"got: {type(tools).__name__} len={len(tools) if isinstance(tools, list) else 'n/a'}")

tool_names = []
for t in tools if isinstance(tools, list) else []:
    # openai format: {"type":"function","function":{"name":...,"parameters":...}}
    fn = t.get("function") if isinstance(t, dict) else None
    if fn and "name" in fn:
        tool_names.append(fn["name"])

check("tool names parsed", len(tool_names) > 0, f"tools[0]={tools[0] if tools else None}")

expected_evolve_tools = {
    "evolve_datetime",
    "evolve_prompt_list",
    "evolve_hook_list",
}
present = expected_evolve_tools & set(tool_names)
check(f"builtin evolve tools present ({sorted(expected_evolve_tools)})",
      present == expected_evolve_tools,
      f"found: {sorted(present)}; all tools: {sorted(tool_names)}")

# every tool should have a parameters schema
missing_schema = [n for n in tool_names if not any(
    (t.get("function", {}).get("name") == n and t.get("function", {}).get("parameters"))
    for t in tools
)]
check("all tools have parameters schema", not missing_schema, f"missing: {missing_schema}")

# --- prompt contract: evolve_prompt_{read,write,edit} must enum-constrain prompt arg ---
EXPECTED_PROMPT_FILES = ["preamble.md", "chat.md", "heartbeat.md", "compaction.md", "recover.md"]

def tool_params_local(name):
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == name:
            return fn.get("parameters", {})
    return {}

for prompt_tool in ("evolve_prompt_read", "evolve_prompt_write", "evolve_prompt_edit"):
    params = tool_params_local(prompt_tool)
    prompt_prop = (params.get("properties") or {}).get("prompt") or {}
    check(f"{prompt_tool}.prompt has enum",
          set(prompt_prop.get("enum") or []) == set(EXPECTED_PROMPT_FILES),
          f"got: {prompt_prop.get('enum')}")
    check(f"{prompt_tool}.prompt description mentions contract",
          "contract" in (prompt_prop.get("description") or "").lower(),
          f"got: {prompt_prop.get('description')}")

# --- assertions on hook-defined tools from examples/hello ---
# hook prefix is "evolve" (from discover), so note_* → hello_note_*

expected_hello_tools = {
    "hello_note_list",
    "hello_note_read",
    "hello_note_write",
    "hello_note_delete",
}
present_hello = expected_hello_tools & set(tool_names)
check(f"hello hook tools present ({sorted(expected_hello_tools)})",
      present_hello == expected_hello_tools,
      f"found: {sorted(present_hello)}")

def tool_params(name):
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == name:
            return fn.get("parameters", {})
    return {}

def prop(name, field):
    return (tool_params(name).get("properties") or {}).get(field) or {}

def required(name):
    return set(tool_params(name).get("required") or [])

# hello_note_list: include_hidden: boolean, optional
p = prop("hello_note_list", "include_hidden")
check("note_list.include_hidden is boolean", p.get("type") == "boolean", f"got: {p}")
check("note_list.include_hidden has description", "hidden" in (p.get("description") or "").lower())
check("note_list.include_hidden is optional (not in required)",
      "include_hidden" not in required("hello_note_list"),
      f"required: {required('hello_note_list')}")

# hello_note_read: name: string required, limit: number optional
p = prop("hello_note_read", "name")
check("note_read.name is string", p.get("type") == "string", f"got: {p}")
check("note_read.name is required", "name" in required("hello_note_read"))
p = prop("hello_note_read", "limit")
check("note_read.limit is number", p.get("type") == "number", f"got: {p}")
check("note_read.limit is optional", "limit" not in required("hello_note_read"))

# hello_note_write: name+content required strings, tags: array[string] optional, metadata: object optional
p = prop("hello_note_write", "tags")
check("note_write.tags is array", p.get("type") == "array", f"got: {p}")
items_type = (p.get("items") or {}).get("type")
check("note_write.tags items are string", items_type == "string", f"got items: {p.get('items')}")
check("note_write.tags is optional", "tags" not in required("hello_note_write"))

p = prop("hello_note_write", "metadata")
check("note_write.metadata is object", p.get("type") == "object", f"got: {p}")
check("note_write.metadata is optional", "metadata" not in required("hello_note_write"))

# enum param: note_write.priority is a string constrained to a fixed set
p = prop("hello_note_write", "priority")
check("note_write.priority has enum", p.get("enum") == ["low", "normal", "high"], f"got: {p}")
check("note_write.priority is optional", "priority" not in required("hello_note_write"))

# verify the captured schema actually rejects non-enum values per JSON Schema
import jsonschema
write_schema_full = tool_params("hello_note_write")
valid_payload = {"name": "x.md", "content": "y", "priority": "high"}
invalid_payload = {"name": "x.md", "content": "y", "priority": "urgent"}
ok_valid = True
try:
    jsonschema.validate(valid_payload, write_schema_full)
except jsonschema.ValidationError as e:
    ok_valid = False
    valid_err = str(e)
check("note_write valid enum value passes jsonschema validation",
      ok_valid, f"unexpectedly rejected: {valid_err if not ok_valid else ''}")
ok_invalid = False
invalid_err = ""
try:
    jsonschema.validate(invalid_payload, write_schema_full)
except jsonschema.ValidationError as e:
    ok_invalid = True
    invalid_err = str(e)
check("note_write invalid enum value is rejected by jsonschema",
      ok_invalid and "priority" in invalid_err,
      f"got: {invalid_err or '(no error)'}")

# verify commit fa5b1d7 ("more precise zod schema for nested objects"):
# object/array/any params must expose the explicit jsonValue primitive union
# (string|number|boolean|null|array|object) rather than an under-specified `any`.
def resolve_schema(node, full_schema):
    """follow a single $ref if present, returning the resolved object"""
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref.startswith("#/$defs/"):
            key = ref.split("/")[-1]
            return (full_schema.get("$defs") or {}).get(key, {})
    return node

def has_primitive_union(node, full_schema):
    """true if node (possibly via $ref) is an anyOf containing >=4 primitives"""
    node = resolve_schema(node, full_schema)
    if not isinstance(node, dict):
        return False
    variants = node.get("anyOf") or node.get("oneOf") or []
    types = {v.get("type") for v in variants if isinstance(v, dict)}
    # require at least string+number+boolean+null to prove it's the jsonValue union
    return {"string", "number", "boolean", "null"}.issubset(types)

write_schema = tool_params("hello_note_write")

# object without inner types → additionalProperties must resolve to jsonValue union
metadata_node = (write_schema.get("properties") or {}).get("metadata") or {}
addl = metadata_node.get("additionalProperties")
check("note_write.metadata values are jsonValue union (not bare any)",
      addl is not None and has_primitive_union(addl, write_schema),
      f"got additionalProperties: {addl}")

# any type → schema must itself resolve to the jsonValue union
p_extras = (write_schema.get("properties") or {}).get("extras") or {}
check("note_write.extras (type=any) is jsonValue union",
      has_primitive_union(p_extras, write_schema),
      f"got: {p_extras}")

# bare array (no inner type) → items must resolve to jsonValue union
p_raw = (write_schema.get("properties") or {}).get("raw_list") or {}
check("note_write.raw_list is array", p_raw.get("type") == "array", f"got: {p_raw}")
items_node = p_raw.get("items")
check("note_write.raw_list items are jsonValue union (not bare any)",
      items_node is not None and has_primitive_union(items_node, write_schema),
      f"got items: {items_node}")

req_write = required("hello_note_write")
check("note_write.name required", "name" in req_write)
check("note_write.content required", "content" in req_write)
for opt_field in ("tags", "metadata", "extras", "raw_list"):
    check(f"note_write.{opt_field} is optional",
          opt_field not in req_write, f"required: {req_write}")

# hello_note_delete: name required string
p = prop("hello_note_delete", "name")
check("note_delete.name is string", p.get("type") == "string", f"got: {p}")
check("note_delete.name is required", "name" in required("hello_note_delete"))

# --- assertions on heartbeat flow ---
# while the build request was stalled, the plugin's heartbeat tick should have
# fired (EVOLVE_HEARTBEAT_MS << STALL_SECONDS), creating a new session and
# sending a chat/completions request whose user message starts with [heartbeat]
# and whose system prompt comes from hello's heartbeat() hook.

hb_req = None
with capture_lock:
    for c in captured:
        if "chat/completions" in c["path"] and is_heartbeat_request(c["body"]):
            hb_req = c
            break

check("heartbeat chat/completions request captured", hb_req is not None,
      f"captured paths: {[c['path'] for c in captured]}")

if hb_req:
    (ARTIFACTS / "opencode_integration.heartbeat_request.json").write_text(
        json.dumps(hb_req, indent=2, default=str))
    hb_body = hb_req["body"]
    hb_messages = hb_body.get("messages", [])

    def message_text(m):
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return ""

    user_msgs = [message_text(m) for m in hb_messages if m.get("role") == "user"]
    check("heartbeat user message has [heartbeat] prefix",
          any("[heartbeat]" in t for t in user_msgs),
          f"user msgs: {[t[:80] for t in user_msgs]}")
    check("heartbeat user message contains hello prompt body",
          any("Review your notes" in t for t in user_msgs),
          f"user msgs: {[t[:80] for t in user_msgs]}")

    hb_system = "\n".join(
        message_text(m) for m in hb_messages if m.get("role") == "system")
    check("heartbeat system prompt non-empty", len(hb_system) > 0)
    check("heartbeat request carries tools",
          isinstance(hb_body.get("tools"), list) and len(hb_body["tools"]) > 0)

# --- end-to-end enum rejection ---
# second opencode run against a mock that emits a tool_call for hello_note_write
# with priority="urgent" (not in the enum). opencode should reject the args and
# the next chat/completions request should carry a tool-result with an error.

rej_captured = []
rej_lock = threading.Lock()
rej_step = [0]  # [0] = count of non-heartbeat tools-bearing requests seen

BAD_ARGS = '{"name":"x.md","content":"y","priority":"urgent"}'
def sse_tool_call(args):
    return (
        'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
        '"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,'
        '"id":"call_1","type":"function","function":{"name":"hello_note_write",'
        f'"arguments":{json.dumps(args)}}}}}]}}}}]}}\n\n'
        'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
        '"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        'data: [DONE]\n\n'
    ).encode()

SSE_DONE = (
    'data: {"id":"2","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"done"},"finish_reason":null}]}\n\n'
    'data: {"id":"2","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    'data: [DONE]\n\n'
).encode()

class RejectionHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[]}')
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with rej_lock:
            rej_captured.append({"path": self.path, "body": body})
            is_tools_req = ("chat/completions" in self.path and body.get("tools")
                            and not is_heartbeat_request(body))
            if is_tools_req:
                rej_step[0] += 1
                step = rej_step[0]
            else:
                step = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # step 1: emit the bad tool call; all subsequent: emit "done"
        self.wfile.write(sse_tool_call(BAD_ARGS) if step == 1 else SSE_DONE)

rej_port = free_port()
rej_server = ThreadingHTTPServer(("127.0.0.1", rej_port), RejectionHandler)
threading.Thread(target=rej_server.serve_forever, daemon=True).start()
rej_base = f"http://127.0.0.1:{rej_port}/v1"

rej_project = Path(tempfile.mkdtemp(prefix="evolve-rej-test-"))
shutil.copytree(hello_src, rej_project / "project", dirs_exist_ok=False)
rej_dir = rej_project / "project"
(rej_dir / "hooks" / "evolve.py").chmod(0o755)
rej_config = dict(config)
rej_config["provider"] = {"mock": {
    "name": "Mock", "options": {"apiKey": "test", "baseURL": rej_base},
    "models": {"mock": {"name": "Mock Model"}},
}}
(rej_dir / "opencode.json").write_text(json.dumps(rej_config, indent=2))

rej_env = {**env, "OPENCODE_EVOLVE_WORKSPACE": str(rej_dir),
           "EVOLVE_HEARTBEAT_MS": "999999"}  # disable heartbeat noise

rej_proc = subprocess.Popen(
    [*OPENCODE_CMD, "run", "--print-logs", "--log-level", "INFO", "write a note"],
    cwd=str(rej_dir), env=rej_env,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)

# wait for a second tools-bearing request (the one carrying the tool result)
rej_deadline = time.time() + 60
followup = None
while time.time() < rej_deadline:
    with rej_lock:
        tools_reqs = [c for c in rej_captured
                      if "chat/completions" in c["path"] and c["body"].get("tools")
                      and not is_heartbeat_request(c["body"])]
        if len(tools_reqs) >= 2:
            followup = tools_reqs[1]
            break
    if rej_proc.poll() is not None:
        break
    time.sleep(0.2)

if rej_proc.poll() is None:
    rej_proc.terminate()
try:
    rej_stdout, rej_stderr = rej_proc.communicate(timeout=15)
except subprocess.TimeoutExpired:
    rej_proc.kill()
    rej_stdout, rej_stderr = rej_proc.communicate()
rej_server.shutdown()

(ARTIFACTS / "opencode_rejection.stdout.log").write_text(rej_stdout or "")
(ARTIFACTS / "opencode_rejection.stderr.log").write_text(rej_stderr or "")
(ARTIFACTS / "opencode_rejection.captured.json").write_text(
    json.dumps(rej_captured, indent=2, default=str))

check("rejection: follow-up chat/completions captured", followup is not None,
      f"captured: {len(rej_captured)} reqs, tools-bearing: "
      f"{sum(1 for c in rej_captured if c['body'].get('tools') and not is_heartbeat_request(c['body']))}")

if followup:
    # find the tool-result message that opencode sent back
    msgs = followup["body"].get("messages", [])
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    tool_text = " ".join(
        (m.get("content") if isinstance(m.get("content"), str)
         else "".join(p.get("text","") for p in (m.get("content") or []) if isinstance(p, dict)))
        for m in tool_msgs
    )
    check("rejection: tool-result message present",
          len(tool_msgs) > 0, f"roles: {[m.get('role') for m in msgs]}")
    # the error should reference either the bad value or the field — zod error
    # text typically includes both. accept any substring that proves rejection.
    err_markers = ("urgent", "priority", "enum", "invalid")
    check("rejection: tool-result carries an error about the invalid enum",
          any(mk in tool_text.lower() for mk in err_markers),
          f"tool_text: {tool_text[:400]}")

shutil.rmtree(rej_project, ignore_errors=True)

# --- cleanup ---

shutil.rmtree(workdir, ignore_errors=True)
shutil.rmtree(fake_home, ignore_errors=True)

# --- hard-fail: every tool parameter must carry a description ---
# zod `.describe()` → JSON schema `description` must survive all the way into
# the outgoing request, otherwise the LLM sees nameless/untyped knobs.
missing_param_desc = []
for t in tools:
    tname = t["function"]["name"]
    props = (t["function"].get("parameters", {}).get("properties") or {})
    for pname, pspec in props.items():
        if not (isinstance(pspec, dict) and pspec.get("description")):
            missing_param_desc.append(f"{tname}.{pname}")
check("every tool parameter has a description",
      not missing_param_desc,
      f"missing descriptions on {len(missing_param_desc)} params: {missing_param_desc[:10]}"
      + (f" ... (+{len(missing_param_desc)-10} more)" if len(missing_param_desc) > 10 else ""))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
