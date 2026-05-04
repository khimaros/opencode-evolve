"""shared mock for openai-compatible chat-completions endpoints.

used by integration tests in opencode-evolve and pi-evolve to capture the
LLM HTTP request a coding-agent harness issues when a hook plugin is loaded.
captures every POST body keyed by path and replies with a minimal SSE stream
("ok\n[DONE]"). optionally stalls the first tools-bearing request to give a
heartbeat tick room to fire inside the still-alive harness process.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# minimal SSE chat-completions response: one delta with "ok", then [DONE].
SSE_RESPONSE = (
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
    'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"mock",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    'data: [DONE]\n\n'
).encode()


def is_heartbeat_request(body):
    """detect evolve heartbeat requests by the [heartbeat] sentinel in user
    content. heartbeats fire on a timer inside the harness and are
    distinguishable from build/title-generation requests."""
    for m in body.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, str) and "[heartbeat]" in c:
            return True
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and "[heartbeat]" in (p.get("text") or ""):
                    return True
    return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockOpenAIServer:
    """threading http server that mimics openai chat-completions.

    args:
      stall_first_with_tools: if True, sleeps `stall_seconds` on the first
          tools-bearing non-heartbeat request — useful for keeping the harness
          alive long enough for a heartbeat tick to fire.
      stall_seconds: stall duration when stall_first_with_tools is True.
      heartbeat_predicate: callable(body) -> bool used to identify heartbeat
          requests (which bypass the stall). defaults to is_heartbeat_request.
    """

    def __init__(self, *, stall_first_with_tools=False, stall_seconds=5,
                 heartbeat_predicate=None):
        self.captured = []
        self.lock = threading.Lock()
        self._stalled_once = False
        self._stall_first = stall_first_with_tools
        self._stall_seconds = stall_seconds
        self._is_heartbeat = heartbeat_predicate or is_heartbeat_request
        self._server = None
        self._port = None

    def start(self):
        self._port = _free_port()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except Exception:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with outer.lock:
                    outer.captured.append({"path": self.path,
                                           "headers": dict(self.headers),
                                           "body": body})
                if (outer._stall_first
                        and not outer._stalled_once
                        and "chat/completions" in self.path
                        and body.get("tools")
                        and not outer._is_heartbeat(body)):
                    outer._stalled_once = True
                    time.sleep(outer._stall_seconds)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(SSE_RESPONSE)
                except BrokenPipeError:
                    pass
            def do_GET(self):
                # some providers probe /models; return empty list
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(b'{"object":"list","data":[]}')
                except BrokenPipeError:
                    pass
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self._port

    def shutdown(self):
        if self._server is not None:
            self._server.shutdown()

    @property
    def port(self):
        return self._port

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._port}/v1"
