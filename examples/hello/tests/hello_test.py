#!/usr/bin/env python3
"""end-to-end tests for hello evolve hook (JSONL IPC)."""

import json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

PASS = FAIL = 0

def call_hook(hook_path, name, ctx=None):
    """call a hook and return (merged_result, logs, exit_code)."""
    input_data = json.dumps(ctx or {})
    proc = subprocess.run(
        [hook_path, name], input=input_data, capture_output=True, text=True,
    )
    result, logs = {}, []
    for line in proc.stdout.strip().splitlines():
        if not line:
            continue
        obj = json.loads(line)
        if "log" in obj:
            logs.append(obj["log"])
        else:
            result.update(obj)
    return result, logs, proc.returncode

def call_tool(hook_path, name, args=None):
    """shorthand for calling a tool via execute_tool hook."""
    return call_hook(hook_path, "execute_tool", {"tool": name, "args": args or {}})

def check(desc, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {desc}")
        if detail:
            print(f"  {detail}")

def has_key(result, key):
    return key in result

def has_value(result, key, substring):
    return key in result and substring in str(result[key])

# --- setup ---

# workspace is either passed as arg or derived from OPENCODE_EVOLVE_WORKSPACE
if len(sys.argv) > 1:
    workspace = sys.argv[1]
else:
    workspace = os.environ.get("OPENCODE_EVOLVE_WORKSPACE", os.environ.get("OPENCODE_SIDECAR_WORKSPACE", ""))
    if not workspace:
        workspace = str(Path(__file__).resolve().parent.parent)

tmp = tempfile.mkdtemp()

try:
    # copy hook into temp workspace
    for d in ("hooks", "traits", "prompts"):
        os.makedirs(os.path.join(tmp, d))
    shutil.copy2(os.path.join(workspace, "hooks", "evolve.py"), os.path.join(tmp, "hooks", "evolve.py"))
    hook = os.path.join(tmp, "hooks", "evolve.py")
    for name, content in [("preamble.md", "preamble"), ("chat.md", "chat"),
                          ("heartbeat.md", "heartbeat"), ("compaction.md", "compaction")]:
        open(os.path.join(tmp, "prompts", name), "w").write(content)

    # --- error handling ---

    r, _, rc = call_hook(hook, "nonexistent")
    check("unknown hook returns error", has_key(r, "error"))

    proc = subprocess.run([hook], capture_output=True, text=True)
    check("no args returns error", proc.returncode != 0 or "error" in proc.stdout)

    # --- discover ---

    r, logs, _ = call_hook(hook, "discover")
    check("discover returns tools key", has_key(r, "tools"))
    check("discover has no typo keys", not has_key(r, "tool"))
    names = [t["name"] for t in r["tools"]]
    for expected in ("note_list", "note_read", "note_write", "note_delete"):
        check(f"discover includes {expected}", expected in names, f"got: {names}")
    check("discover returns at least 4 tools", len(r["tools"]) >= 4, f"got: {len(r['tools'])}")
    check("discover logs tool names", any("tools:" in l for l in logs))

    # --- discover tool parameter schemas ---

    tools_by_name = {t["name"]: t for t in r["tools"]}
    expected_counts = {"note_list": 0, "note_read": 1, "note_write": 2, "note_delete": 1}
    for name, count in expected_counts.items():
        actual = len(tools_by_name[name]["parameters"])
        check(f"{name} has {count} params", actual == count, f"got: {actual}")

    # --- mutate_request ---

    r, logs, _ = call_hook(hook, "mutate_request")
    check("request returns system key", has_key(r, "system"))
    check("request has no tools key", not has_key(r, "tools"))
    system_text = "\n".join(r.get("system", []))
    check("request includes preamble", "preamble" in system_text)
    check("request includes chat prompt", "chat" in system_text)
    check("request logs notes", any("notes:" in l for l in logs))

    # --- mutate_request env block ---

    env_match = re.search(r"<env>(.*?)</env>", system_text, re.DOTALL)
    check("request has env block", env_match is not None)
    if env_match:
        env_content = env_match.group(1).strip()
        env_lines = [l.strip() for l in env_content.splitlines() if l.strip()]
        check("env block has exactly 1 line", len(env_lines) == 1, f"got {len(env_lines)}: {env_lines}")
        check("env line is session start time", env_lines[0].startswith("Session start time:") if env_lines else False,
              f"got: {env_lines[0] if env_lines else '(empty)'}")
        ts_match = re.search(r"Session start time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC", env_lines[0]) if env_lines else None
        if ts_match:
            reported = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            drift = abs((utc_now - reported).total_seconds())
            check("env timestamp is UTC (not local)", drift < 5, f"drift: {drift:.0f}s")

    # --- heartbeat ---

    r, logs, _ = call_hook(hook, "heartbeat")
    check("heartbeat returns system key", has_key(r, "system"))
    check("heartbeat returns user key", has_key(r, "user"))
    system_text = "\n".join(r.get("system", []))
    check("heartbeat includes preamble", "preamble" in system_text)
    check("heartbeat includes heartbeat prompt", "heartbeat" in system_text)
    check("heartbeat logs notes", any("notes:" in l for l in logs))

    # --- recover ---

    r, logs, _ = call_hook(hook, "recover", {"failed_hook": "mutate_request", "error": "boom"})
    check("recover returns system key", has_key(r, "system"))
    check("recover returns user key", has_key(r, "user"))
    check("recover logs context", any("recovering from mutate_request" in l for l in logs))

    # --- observe_message ---

    _, logs, _ = call_hook(hook, "observe_message", {"session": {"id": "abc", "agent": "evolve"}})
    check("observe_message logs session", any("session=abc" in l for l in logs))
    check("observe_message logs agent", any("agent=evolve" in l for l in logs))

    # --- idle ---

    r, logs, _ = call_hook(hook, "idle", {"session": {"id": "s1", "agent": "evolve"}, "answer": "hello"})
    check("idle returns empty by default", not has_key(r, "continue"))
    check("idle logs session", any("session=s1" in l for l in logs))
    check("idle logs answer length", any("answer_len=5" in l for l in logs))

    # --- compacting ---

    r, logs, _ = call_hook(hook, "compacting")
    check("compacting returns prompt key", has_key(r, "prompt"))
    check("compacting logs notes", any("notes:" in l for l in logs))

    # --- note_list ---

    open(os.path.join(tmp, "traits", "todo.md"), "w").write("buy milk")
    open(os.path.join(tmp, "traits", "ideas.md"), "w").write("build a robot")

    r, logs, _ = call_tool(hook, "note_list")
    check("note_list returns result key", has_key(r, "result"))
    check("note_list result is str", isinstance(r.get("result"), str))
    check("note_list includes todo", "todo.md" in r["result"])
    check("note_list includes ideas", "ideas.md" in r["result"])
    check("note_list logs tool name", any("tool=note_list" in l for l in logs))

    for f in ("todo.md", "ideas.md"):
        os.remove(os.path.join(tmp, "traits", f))

    # --- note_list empty ---

    r, _, _ = call_tool(hook, "note_list")
    check("note_list empty returns no notes", "no notes" in r["result"])

    # --- note_read ---

    open(os.path.join(tmp, "traits", "A.md"), "w").write("test content")

    r, _, _ = call_tool(hook, "note_read", {"name": "A.md"})
    check("note_read returns result key", has_key(r, "result"))
    check("note_read returns content", "test content" in r["result"])

    r, _, _ = call_tool(hook, "note_read", {"name": "MISSING.md"})
    check("note_read missing returns not found", "not found" in r["result"])

    os.remove(os.path.join(tmp, "traits", "A.md"))

    # --- note_write ---

    r, _, _ = call_tool(hook, "note_write", {"name": "NEW.md", "content": "hello world"})
    check("note_write returns result key", has_key(r, "result"))
    check("note_write returns success", "wrote" in r["result"])
    check("note_write reports modified", has_key(r, "modified"))
    check("note_write modified list correct", r.get("modified") == ["NEW.md"])
    content = open(os.path.join(tmp, "traits", "NEW.md")).read()
    check("note_write wrote file", content == "hello world")

    # --- note_write returns notify ---

    check("note_write returns notify", has_key(r, "notify"))
    check("note_write notify is list", isinstance(r.get("notify"), list))
    check("note_write notify has note_changed", any(n.get("type") == "note_changed" for n in r.get("notify", [])))

    os.remove(os.path.join(tmp, "traits", "NEW.md"))

    # --- note_delete ---

    open(os.path.join(tmp, "traits", "DEL.md"), "w").write("delete me")

    r, logs, _ = call_tool(hook, "note_delete", {"name": "DEL.md"})
    check("note_delete returns result key", has_key(r, "result"))
    check("note_delete returns success", "deleted" in r["result"])
    check("note_delete reports modified", r.get("modified") == ["DEL.md"])
    check("note_delete removed file", not os.path.exists(os.path.join(tmp, "traits", "DEL.md")))
    check("note_delete logs tool name", any("tool=note_delete" in l for l in logs))

    r, _, _ = call_tool(hook, "note_delete", {"name": "DEL.md"})
    check("note_delete not found", "not found" in r["result"])

    # --- note_delete returns notify ---

    open(os.path.join(tmp, "traits", "N.md"), "w").write("x")
    r, _, _ = call_tool(hook, "note_delete", {"name": "N.md"})
    check("note_delete returns notify", has_key(r, "notify"))
    check("note_delete notify has note_changed", any(n.get("type") == "note_changed" for n in r.get("notify", [])))

    # --- format_notification ---

    r, _, _ = call_hook(hook, "format_notification", {
        "notifications": [{"type": "note_changed", "files": ["FOO.md", "BAR.md"]}],
    })
    check("format_notification returns message", has_key(r, "message"))
    check("format_notification message has note-update", "note-update" in r.get("message", ""))
    check("format_notification message includes files", "BAR.md" in r.get("message", "") and "FOO.md" in r.get("message", ""))

    r, _, _ = call_hook(hook, "format_notification", {"notifications": []})
    check("format_notification empty returns no message", not has_key(r, "message"))

    r, _, _ = call_hook(hook, "format_notification", {})
    check("format_notification missing key returns no message", not has_key(r, "message"))

    # --- unknown tool ---

    r, _, _ = call_tool(hook, "nonexistent")
    check("unknown tool returns result key", has_key(r, "result"))
    check("unknown tool returns error", "unknown tool" in r["result"])

    # --- bad args ---

    r, _, _ = call_tool(hook, "note_read", {"wrong": "param"})
    check("bad args returns result key", has_key(r, "result"))
    check("bad args returns tool error", "tool error" in r["result"])

    # --- empty stdin ---

    r, _, _ = call_hook(hook, "mutate_request")
    check("empty context returns system key", has_key(r, "system"))

    # --- history passthrough ---

    sample_history = [
        {"role": "user", "agent": "evolve", "parts": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "agent": "evolve", "parts": [{"type": "text", "text": "hi there"}]},
    ]

    r, _, _ = call_hook(hook, "mutate_request", {"history": sample_history})
    check("mutate_request with history returns system", has_key(r, "system"))

    r, logs, _ = call_hook(hook, "observe_message", {
        "session": {"id": "h1", "agent": "evolve"}, "history": sample_history,
    })
    check("observe_message with history logs session", any("session=h1" in l for l in logs))

    r, logs, _ = call_hook(hook, "idle", {
        "session": {"id": "h2", "agent": "evolve"}, "answer": "ok", "history": sample_history,
    })
    check("idle with history returns ok", not has_key(r, "error"))
    check("idle with history logs session", any("session=h2" in l for l in logs))

    r, _, _ = call_hook(hook, "heartbeat", {"history": sample_history})
    check("heartbeat with history returns system", has_key(r, "system"))

    r, _, _ = call_hook(hook, "compacting", {"history": sample_history})
    check("compacting with history returns prompt", has_key(r, "prompt"))

    r, _, _ = call_hook(hook, "recover", {"failed_hook": "test", "error": "x", "history": sample_history})
    check("recover with history returns system", has_key(r, "system"))

    r, _, _ = call_hook(hook, "format_notification", {
        "notifications": [{"type": "note_changed", "files": ["X.md"]}], "history": sample_history,
    })
    check("format_notification with history returns message", has_key(r, "message"))

    r, _, _ = call_hook(hook, "tool_before", {
        "session": {"id": "h3"}, "tool": "t", "callID": "c", "args": {}, "history": sample_history,
    })
    check("tool_before with history ok", not has_key(r, "error"))

    r, _, _ = call_hook(hook, "tool_after", {
        "session": {"id": "h3"}, "tool": "t", "callID": "c", "title": "", "output": "", "history": sample_history,
    })
    check("tool_after with history ok", not has_key(r, "error"))

finally:
    shutil.rmtree(tmp)

# --- summary ---

total = PASS + FAIL
print(f"\n{total} tests, {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
