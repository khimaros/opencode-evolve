#!/usr/bin/env python3
"""capture the actual openai chat/completions request opencode sends when a
given workspace's hooks are loaded. dumps the request body and prints any
requested tool's schema so you can inspect exactly what the model sees.

usage: capture-llm-request.py <workspace_dir> [--tool TOOL_NAME] [--prompt TEXT]

env:
  OPENCODE_BIN — path to opencode binary (defaults to ../../anomalyco/opencode/...)
"""

import argparse, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVOLVE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BIN = EVOLVE_ROOT.parent.parent / "anomalyco" / "opencode" / "packages" / "opencode" / "dist" / "opencode-linux-x64" / "bin" / "opencode"

captured = []
lock = threading.Lock()

SSE_RESPONSE = (
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    'data: [DONE]\n\n'
).encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(n)
        try: body = json.loads(raw)
        except: body = {"_raw": raw.decode("utf-8", "replace")}
        with lock:
            captured.append({"path": self.path, "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(SSE_RESPONSE)
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{"object":"list","data":[]}')

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", help="path to a hooks workspace (must contain hooks/)")
    ap.add_argument("--tool", help="print this tool's schema after capture")
    ap.add_argument("--prompt", default="hello", help="prompt sent to opencode run")
    ap.add_argument("--out", default="/tmp/llm_capture.json", help="where to dump the captured request")
    args = ap.parse_args()

    workspace_src = Path(args.workspace).resolve()
    assert (workspace_src / "hooks").is_dir(), f"no hooks/ under {workspace_src}"

    opencode_bin = Path(os.environ.get("OPENCODE_BIN", DEFAULT_BIN))
    assert opencode_bin.exists(), f"no opencode binary at {opencode_bin} (set OPENCODE_BIN)"

    print("building plugin...")
    r = subprocess.run(["npx", "tsc"], cwd=EVOLVE_ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("build failed:", r.stderr); sys.exit(1)
    plugin_path = EVOLVE_ROOT / "dist" / "index.js"
    assert plugin_path.exists(), f"no plugin at {plugin_path}"

    workdir = Path(tempfile.mkdtemp(prefix="evolve-capture-"))
    project = workdir / "project"
    shutil.copytree(workspace_src, project)
    for f in (project / "hooks").iterdir():
        if f.is_file(): f.chmod(0o755)

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/v1"
    print(f"mock at {base_url}")

    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"mock": {
            "name": "Mock", "options": {"apiKey": "test", "baseURL": base_url},
            "models": {"mock": {"name": "Mock"}},
        }},
        "model": "mock/mock",
        "small_model": "mock/mock",
        "plugin": [plugin_path.as_uri()],
    }
    (project / "opencode.json").write_text(json.dumps(config, indent=2))

    env = {**os.environ,
        "OPENCODE_EVOLVE_WORKSPACE": str(project),
        "EVOLVE_HEARTBEAT_MS": "999999",
        "EVOLVE_MODEL": "mock/mock",
        "OPENAI_API_KEY": "test",
        "CI": "1"}

    print(f"running opencode (prompt: {args.prompt!r})...")
    proc = subprocess.Popen(
        [str(opencode_bin), "run", args.prompt],
        cwd=str(project), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    deadline = time.time() + 60
    got = None
    while time.time() < deadline:
        with lock:
            for c in captured:
                if "chat/completions" in c["path"] and c["body"].get("tools"):
                    got = c; break
        if got: break
        time.sleep(0.5)

    proc.terminate()
    try: proc.wait(timeout=5)
    except: proc.kill()

    if not got:
        print("no tools-bearing request captured")
        print("captured paths:", [c["path"] for c in captured])
        sys.exit(1)

    Path(args.out).write_text(json.dumps(got, indent=2, default=str))
    print(f"dumped to {args.out}")

    if args.tool:
        for t in got["body"].get("tools", []):
            fn = t.get("function", {})
            if fn.get("name") == args.tool:
                print(f"\n=== {args.tool} ===")
                print("description:", fn.get("description"))
                print("parameters:")
                print(json.dumps(fn.get("parameters"), indent=2))
                return
        print(f"tool {args.tool!r} not found in request; available:",
              [t.get("function", {}).get("name") for t in got["body"].get("tools", [])])

if __name__ == "__main__":
    main()
