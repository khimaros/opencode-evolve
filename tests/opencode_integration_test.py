#!/usr/bin/env python3
"""end-to-end conformance test for opencode-evolve.

drives the shared hcp conformance suite (../hcp-spec/conformance) against the
real opencode binary with the evolve plugin loaded, then adds the scenarios
unique to opencode. the protocol-level build and heartbeat assertions live in
the shared driver; this file owns the opencode seam (opencode.json provider +
plugin registration, the binary launch, xdg/home isolation) and opencode's own
scenarios:

  1. build + heartbeat (shared driver): hello tools, system-prompt fidelity, the
     heartbeat tick fired while the build was stalled
  2. enum rejection: a tool_call with an out-of-enum value is rejected end-to-end
  3. abstain: a hook returning {} must not clobber opencode's own system prompt
  4. permission: a deny rule blocks a hook-defined tool before its side effects
  5. compaction: the compaction request is a byte-identical prefix of the chat
     request (KV-cache stability)

OPENCODE_BIN / OPENCODE_SRC select the binary; see resolve_opencode_cmd.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = PROJECT_ROOT / "tests" / ".artifacts"
PLUGIN_PATH = PROJECT_ROOT / "dist" / "index.js"

sys.path.insert(0, str(PROJECT_ROOT.parent / "hcp-spec" / "conformance"))
import hcpconform as hc

HEARTBEAT_MS = 500
STALL_SECONDS = 5


def resolve_opencode_cmd():
    """OPENCODE_BIN=<command> (shlex-split) or OPENCODE_SRC=<checkout> (run via
    bun), else `opencode` on PATH."""
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
    found = shutil.which("opencode")
    if not found:
        print("error: `opencode` not found on PATH; set OPENCODE_BIN or OPENCODE_SRC.",
              file=sys.stderr)
        sys.exit(2)
    return [found]


OPENCODE_CMD = resolve_opencode_cmd()
print(f"opencode command: {' '.join(OPENCODE_CMD)}")


# --- opencode-specific setup helpers (shared across this file's scenarios) ---

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home():
    """a throwaway HOME/xdg tree so the user's ~/.config/opencode never leaks
    its providers/plugins/mcp servers into the test."""
    home = Path(tempfile.mkdtemp(prefix="evolve-home-"))
    for sub in (".config/opencode", ".local/share/opencode",
                ".cache/opencode", ".local/state/opencode"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def make_env(workspace, home, **extra):
    # strip opencode/xdg vars and PWD (opencode run resolves root from PWD).
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("OPENCODE_", "XDG_")) and k != "PWD"}
    env = {
        **base, "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
        "OPENCODE_TEST_HOME": str(home), "OPENCODE_EVOLVE_WORKSPACE": str(workspace),
        "EVOLVE_HEARTBEAT_MS": str(HEARTBEAT_MS), "EVOLVE_MODEL": "mock/fake-model",
        "EVOLVE_HEARTBEAT_SKIP_ACTIVE": "false", "EVOLVE_HEARTBEAT_AGENT": "build",
        "OPENAI_API_KEY": "test", "CI": "1",
    }
    env.update(extra)
    return env


def make_config(base_url, **extra):
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"mock": {"name": "Mock", "options": {"apiKey": "test", "baseURL": base_url},
                              "models": {"fake-model": {"name": "Fake Model"}}}},
        "model": "mock/fake-model", "small_model": "mock/fake-model",
        "plugin": [PLUGIN_PATH.as_uri()],
    }
    cfg.update(extra)
    return cfg


def seed_project(fixture, base_url, **config_extra):
    """temp workspace seeded from the fixture with an opencode.json written.
    returns (parent_tmp, project_dir) -- caller removes parent_tmp."""
    parent = Path(tempfile.mkdtemp(prefix="evolve-oc-test-"))
    project = hc.seed_workspace(parent / "project", fixture)
    (project / "opencode.json").write_text(json.dumps(make_config(base_url, **config_extra), indent=2))
    return parent, project


def run_opencode(project, env, prompt, *args, deadline_s=60):
    """run `opencode run [...] <prompt>` to completion, terminating once the
    deadline passes. returns (proc, stdout, stderr)."""
    proc = subprocess.Popen(
        [*OPENCODE_CMD, "run", *args, "--print-logs", "--log-level", "INFO", prompt],
        cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc


def drain(proc, timeout=15):
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


# --- jsonValue-union helpers (opencode commit fa5b1d7) ---

def resolve_schema(node, full):
    """follow a single $ref; opencode emits the table under `definitions`
    even though refs use `#/$defs/...`, so check both."""
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
            defs = full.get("$defs") or full.get("definitions") or {}
            return defs.get(ref.split("/")[-1], {})
    return node


def has_primitive_union(node, full):
    """true if node (possibly via $ref) is an anyOf/oneOf covering the json
    primitive union (string|number|boolean|null), proving it is not a bare any."""
    node = resolve_schema(node, full)
    if not isinstance(node, dict):
        return False
    types = {v.get("type") for v in (node.get("anyOf") or node.get("oneOf") or [])
             if isinstance(v, dict)}
    return {"string", "number", "boolean", "null"}.issubset(types)


class OpencodeAdapter(hc.HostAdapter):
    name = "opencode-evolve"
    wants_heartbeat = True
    builtin_tools = {"evolve_datetime", "evolve_prompt_list", "evolve_hook_list"}

    def __init__(self):
        self.stderr = ""

    def build(self, runner):
        r = subprocess.run(["npx", "tsc"], cwd=PROJECT_ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            runner.check("plugin built", False, (r.stdout or "") + (r.stderr or ""))
            return False
        runner.check("plugin built", PLUGIN_PATH.exists(), f"missing: {PLUGIN_PATH}")
        return PLUGIN_PATH.exists()

    def run_build(self, fixture):
        fake = hc.start_fake_openai("--stall-first-with-tools",
                                    "--stall-seconds", str(STALL_SECONDS))
        print(f"mock server on {fake.base_url}")
        parent, project = seed_project(fixture, fake.base_url)
        home = make_home()
        env = make_env(project, home)
        print("running opencode...")
        proc = run_opencode(project, env, "hello world", "--agent", "hello")
        # wait up to 90s for both the stalled build request and the heartbeat
        # tick that fires inside the plugin while the build is stalled.
        def both():
            caps = fake.captures()
            b, hb = hc.find_build_request(caps), hc.find_heartbeat_request(caps)
            return (b, hb) if (b and hb) else None
        found = hc.poll_for(both, proc, 90) or (None, None)
        stdout, self.stderr = drain(proc, timeout=30)
        caps = fake.captures()
        fake.stop()
        shutil.rmtree(parent, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
        build = found[0] or hc.find_build_request(caps)
        heartbeat = found[1] or hc.find_heartbeat_request(caps)
        return hc.RunResult(build, heartbeat, caps, stdout, self.stderr)

    def extra_build_checks(self, body, fixture, runner):
        hc.assert_builtin_tools(body, self.builtin_tools, runner)
        hc.assert_note_tags_array(body, runner)
        runner.check("note_list.include_hidden has description",
                     "hidden" in (hc.prop(body, "hello_note_list", "include_hidden")
                                  .get("description") or "").lower())
        # every tool exposes a parameters schema.
        no_params = sorted(n for n in hc.tool_names(body) if not hc.tool_params(body, n))
        runner.check("all tools have parameters schema", not no_params, f"missing: {no_params}")
        hc.assert_param_descriptions(body, runner, prefixes=("evolve_", "hello_"))
        hc.assert_system_preamble_chat(body, fixture, runner)
        self._check_prompt_enums(body, runner)
        self._check_jsonvalue_unions(body, runner)

    def _check_prompt_enums(self, body, runner):
        for t in ("evolve_prompt_read", "evolve_prompt_write", "evolve_prompt_edit"):
            p = hc.prop(body, t, "prompt")
            runner.check(f"{t}.prompt has enum",
                         hc.enum_values(p) == set(hc.CONTRACT_PROMPTS), f"got: {p.get('enum')}")
            runner.check(f"{t}.prompt description mentions contract",
                         "contract" in (p.get("description") or "").lower(),
                         f"got: {p.get('description')}")

    def _check_jsonvalue_unions(self, body, runner):
        schema = hc.tool_params(body, "hello_note_write")
        props = schema.get("properties") or {}
        addl = (props.get("metadata") or {}).get("additionalProperties")
        runner.check("note_write.metadata values are jsonValue union (not bare any)",
                     addl is not None and has_primitive_union(addl, schema), f"got: {addl}")
        runner.check("note_write.extras (type=any) is jsonValue union",
                     has_primitive_union(props.get("extras") or {}, schema),
                     f"got: {props.get('extras')}")
        raw = props.get("raw_list") or {}
        runner.check("note_write.raw_list is array", raw.get("type") == "array", f"got: {raw}")
        runner.check("note_write.raw_list items are jsonValue union (not bare any)",
                     raw.get("items") is not None and has_primitive_union(raw.get("items"), schema),
                     f"got items: {raw.get('items')}")
        for opt in ("extras", "raw_list"):
            runner.check(f"note_write.{opt} is optional",
                         opt not in hc.required(body, "hello_note_write"))


# --- opencode-unique scenarios ---

def _tool_result_text(req):
    msgs = req["body"].get("messages", [])
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    text = " ".join(hc._content_text(m.get("content")) for m in tool_msgs)
    return tool_msgs, text


def _wait_for_followup(fake, proc, deadline_s=60):
    """wait for a second tools-bearing non-heartbeat request (the tool-result)."""
    def followup():
        reqs = [c for c in fake.chat_captures()
                if c["body"].get("tools") and not hc.is_heartbeat_request(c["body"])]
        return reqs[1] if len(reqs) >= 2 else None
    return hc.poll_for(followup, proc, deadline_s)


def scenario_rejection(runner, fixture):
    """a tool_call with priority not in the enum is rejected before execution;
    the follow-up tool-result carries an error about the invalid value."""
    bad = {"name": "x.md", "content": "y", "priority": "urgent"}
    fake = hc.start_fake_openai("--consume-only-with-tools")
    hc.program(fake.admin_url, [
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "hello_note_write", "arguments": json.dumps(bad)}}],
         "finish_reason": "tool_calls"},
        {"content": "done", "finish_reason": "stop"}])
    parent, project = seed_project(fixture, fake.base_url)
    home = make_home()
    env = make_env(project, home, EVOLVE_HEARTBEAT_MS="999999")
    proc = run_opencode(project, env, "write a note", "--agent", "hello")
    followup = _wait_for_followup(fake, proc)
    drain(proc)
    fake.stop()
    shutil.rmtree(parent, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    runner.check("rejection: follow-up chat/completions captured", followup is not None)
    if followup:
        tool_msgs, text = _tool_result_text(followup)
        runner.check("rejection: tool-result message present", len(tool_msgs) > 0)
        runner.check("rejection: tool-result carries an error about the invalid enum",
                     any(m in text.lower() for m in ("urgent", "priority", "enum", "invalid")),
                     f"tool_text: {text[:400]}")


def scenario_abstain(runner, fixture):
    """a hook returning {} must not let evolve impose a synthesized system
    prompt; opencode's own system must survive."""
    pre, chat = "EVOLVE_DEFAULT_PREAMBLE_SENTINEL_ZZZ", "EVOLVE_DEFAULT_CHAT_SENTINEL_QQQ"
    fake = hc.start_fake_openai()
    parent = Path(tempfile.mkdtemp(prefix="evolve-abstain-test-"))
    project = parent / "project"
    (project / "hooks").mkdir(parents=True)
    (project / "prompts").mkdir()
    (project / "prompts" / "preamble.md").write_text(pre)
    (project / "prompts" / "chat.md").write_text(chat)
    hook = project / "hooks" / "hello.py"
    hook.write_text("#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "name = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                    "try: ctx = json.loads(sys.stdin.read() or '{}')\n"
                    "except Exception: ctx = {}\n"
                    "if name == 'discover':\n"
                    "    print(json.dumps({'name': 'abstain'}), flush=True)\n")
    hook.chmod(0o755)
    (project / "opencode.json").write_text(json.dumps(make_config(fake.base_url), indent=2))
    home = make_home()
    env = make_env(project, home, EVOLVE_HEARTBEAT_MS="999999")
    proc = run_opencode(project, env, "hi")
    hc.poll_for(lambda: hc.find_build_request(fake.captures()), proc, 60)
    drain(proc)
    main = hc.find_build_request(fake.captures())
    fake.stop()
    shutil.rmtree(parent, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    runner.check("abstain: main chat/completions request captured", main is not None)
    if main:
        s = hc.system_text(main["body"])
        runner.check("abstain: preamble.md sentinel not injected into main request",
                     pre not in s, s[:400])
        runner.check("abstain: chat.md sentinel not injected into main request",
                     chat not in s, s[:400])
        runner.check("abstain: opencode's own system prompt preserved", len(s) > 0)


def scenario_permission(runner, fixture):
    """a deny rule for a hook-defined tool blocks execution before side effects
    and surfaces opencode's denial wording (not an InstanceRef defect)."""
    denied = {"name": "blocked.md", "content": "should not be written"}
    fake = hc.start_fake_openai("--consume-only-with-tools")
    hc.program(fake.admin_url, [
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "hello_note_write", "arguments": json.dumps(denied)}}],
         "finish_reason": "tool_calls"},
        {"content": "done", "finish_reason": "stop"}])
    parent, project = seed_project(fixture, fake.base_url,
                                   permission={"hello_note_write": {"*": "allow", "blocked.md": "deny"}})
    home = make_home()
    env = make_env(project, home, EVOLVE_HEARTBEAT_MS="999999")
    proc = run_opencode(project, env, "write a note to blocked.md", "--agent", "hello")
    followup = _wait_for_followup(fake, proc)
    drain(proc)
    fake.stop()
    blocked_existed = (project / "traits" / "blocked.md").exists()
    shutil.rmtree(parent, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    runner.check("permission: denied tool did not create file", not blocked_existed)
    runner.check("permission: follow-up chat/completions captured", followup is not None)
    if followup:
        tool_msgs, text = _tool_result_text(followup)
        runner.check("permission: tool-result message present", len(tool_msgs) > 0)
        runner.check("permission: tool-result does not report a successful write",
                     "wrote blocked.md" not in text, f"tool_text: {text[:400]}")
        runner.check("permission: tool-result carries opencode's denial wording",
                     "rule which prevents" in text.lower(), f"tool_text: {text[:400]}")
        runner.check("permission: tool-result does not leak InstanceRef defect",
                     "instanceref" not in text.lower(), f"tool_text: {text[:400]}")


def scenario_compaction(runner, fixture):
    """the compaction request's system + history prefix is byte-identical to the
    preceding chat request (KV-cache stability); only the final user turn differs.
    driven over `opencode serve`'s http api."""
    sentinel = "EVOLVE_COMPACTION_SENTINEL_ABCDEFG"
    fake = hc.start_fake_openai()
    parent, project = seed_project(fixture, fake.base_url)
    (project / "prompts" / "compaction.md").write_text(sentinel + "\n")
    home = make_home()
    env = make_env(project, home, EVOLVE_HEARTBEAT_MS="999999")
    port = free_port()
    print(f"starting opencode serve on port {port}...")
    proc = subprocess.Popen(
        [*OPENCODE_CMD, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"

    def http(method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
        return json.loads(txt) if txt else None

    # the http app binds the port ~1.5s before it can serve requests, so a
    # tcp-connect check is a false-ready. poll a real GET until the app answers.
    def serve_ready():
        try:
            with urllib.request.urlopen(url + "/config", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False
    ready = hc.poll_for(serve_ready, proc, 60) or False

    runner.check("compaction: opencode serve is ready", bool(ready))
    session_id = chat_ok = summarize_ok = None
    err = None
    if ready:
        try:
            session_id = (http("POST", "/session", {}) or {}).get("id")
        except Exception as e:
            err = f"session create: {e}"
        runner.check("compaction: session created", bool(session_id), f"err: {err}")
    if session_id:
        try:
            http("POST", f"/session/{session_id}/message",
                 {"agent": "hello", "parts": [{"type": "text", "text": "hello world"}]})
            chat_ok = True
        except Exception as e:
            err = f"session prompt: {e}"
        runner.check("compaction: chat prompt succeeded", bool(chat_ok), f"err: {err}")
    if chat_ok:
        try:
            http("POST", f"/session/{session_id}/summarize",
                 {"providerID": "mock", "modelID": "fake-model"})
            summarize_ok = True
        except Exception as e:
            err = f"session summarize: {e}"
        runner.check("compaction: summarize call succeeded", bool(summarize_ok), f"err: {err}")
    sout, serr = drain(proc)
    caps = fake.chat_captures()
    fake.stop()
    # serve logs are the only way to diagnose a serve that never became ready.
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "opencode_compaction.stdout.log").write_text(sout or "")
    (ARTIFACTS / "opencode_compaction.stderr.log").write_text(serr or "")
    (ARTIFACTS / "opencode_compaction.captured.json").write_text(
        json.dumps(caps, indent=2, default=str))
    shutil.rmtree(parent, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)

    def has_sentinel(body):
        return any(sentinel in hc._content_text(m.get("content")) for m in body.get("messages", []))

    chat_cap = compact_cap = None
    for c in caps:
        if has_sentinel(c["body"]):
            compact_cap = compact_cap or c
        elif c["body"].get("tools"):
            chat_cap = c
    runner.check("compaction: chat request captured", chat_cap is not None)
    runner.check("compaction: compaction request captured (found sentinel)", compact_cap is not None)
    if chat_cap and compact_cap:
        chat_msgs = chat_cap["body"].get("messages", [])
        cmp_msgs = compact_cap["body"].get("messages", [])
        chat_sys = [m for m in chat_msgs if m.get("role") == "system"]
        runner.check("compaction: chat system is hello's composed prompt (gate fired)",
                     fixture.preamble in "\n".join(hc._content_text(m.get("content")) for m in chat_sys))
        runner.check("compaction: system messages byte-identical to chat (KV cache stable)",
                     chat_sys == [m for m in cmp_msgs if m.get("role") == "system"])
        runner.check("compaction: history prefix byte-identical to chat (KV cache stable)",
                     len(cmp_msgs) >= len(chat_msgs) + 1 and cmp_msgs[:len(chat_msgs)] == chat_msgs,
                     f"chat={len(chat_msgs)}, compaction={len(cmp_msgs)}")
        last = cmp_msgs[-1] if cmp_msgs else {}
        runner.check("compaction: last message is user turn carrying the compaction prompt",
                     last.get("role") == "user" and sentinel in hc._content_text(last.get("content")))
        runner.check("compaction: sentinel absent from chat request",
                     not has_sentinel(chat_cap["body"]))
        runner.check("compaction: sentinel absent from compaction prefix (only in final user turn)",
                     not has_sentinel({"messages": cmp_msgs[:-1]}))


def main():
    adapter = OpencodeAdapter()
    if not hc.preflight(adapter):
        return 0
    fixture = hc.Fixture()
    runner = hc.CheckRunner()
    if not adapter.build(runner):
        runner.summary()
        return 1
    result = hc.run_conformance(adapter, runner, fixture)
    hc.dump_artifacts(ARTIFACTS, "opencode_integration", result)
    scenario_rejection(runner, fixture)
    scenario_abstain(runner, fixture)
    scenario_permission(runner, fixture)
    scenario_compaction(runner, fixture)
    runner.summary()
    return runner.exit_code()


if __name__ == "__main__":
    sys.exit(main())
