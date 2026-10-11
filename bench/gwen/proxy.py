import json
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def _ask_for_stream_usage(path: str, body: bytes) -> bytes:
    """OpenAI-style servers send usage in a stream only when asked; ask, so every run records its tokens."""
    if not path.endswith("/chat/completions") or not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or not data.get("stream"):
        return body
    opts = data.get("stream_options") or {}
    if opts.get("include_usage"):
        return body
    data["stream_options"] = {**opts, "include_usage": True}
    return json.dumps(data).encode()


class RecordingProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._forward()

    def do_GET(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_PATCH(self):
        self._forward()

    def _forward(self):
        start_ts = time.time()
        upstream = self.server.upstream_url
        parsed = urlparse(upstream)

        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                headers[k] = v

        content_length = int(self.headers.get("Content-Length", 0))
        body = _ask_for_stream_usage(self.path, self.rfile.read(content_length))
        if body:
            headers["Content-Length"] = str(len(body))

        conn = HTTPConnection(parsed.hostname, parsed.port or 80)

        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception:
            self.send_response(502)
            self.end_headers()
            self._log(start_ts, content_length, None, error="Upstream connection failed")
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        usage = None

        def check_usage(text):
            nonlocal usage
            if "usage" in text:
                try:
                    if text.startswith("data: "):
                        text = text[6:]
                    data = json.loads(text)
                    if "usage" in data:
                        if usage is None:
                            usage = data["usage"]
                        elif isinstance(usage, dict) and isinstance(data["usage"], dict):
                            # Anthropic sums
                            for k, v in data["usage"].items():
                                usage[k] = usage.get(k, 0) + v
                    elif "message" in data and "usage" in data["message"]:
                        if usage is None:
                            usage = data["message"]["usage"]
                        elif isinstance(usage, dict) and isinstance(data["message"]["usage"], dict):
                            for k, v in data["message"]["usage"].items():
                                usage[k] = usage.get(k, 0) + v
                    elif data.get("type") == "response.completed" and isinstance(data.get("response"), dict):
                        # OpenAI Responses API: usage arrives once, nested, in the final event.
                        if usage is None and isinstance(data["response"].get("usage"), dict):
                            usage = data["response"]["usage"]
                except json.JSONDecodeError:
                    pass

        # Try JSON parsing if it's not SSE
        content_type = resp.getheader("Content-Type", "")
        error = None
        try:
            if "application/json" in content_type:
                resp_body = resp.read()
                check_usage(resp_body.decode("utf-8", errors="ignore"))
                self.wfile.write(resp_body)
                self.wfile.flush()
            else:
                # Stream SSE
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.startswith(b"data: "):
                        check_usage(line.decode("utf-8", errors="ignore"))
                    self.wfile.write(line)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The harness went away mid-response (killed at its timeout, or it gave up).
            error = "client disconnected"

        self._log(start_ts, content_length, usage, error=error)
        conn.close()

    def _log(self, ts, req_bytes, usage, error=None):
        if usage:
            norm_usage = {
                "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
                "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
                "cached_tokens": usage.get("cache_read_input_tokens", 0),
            }
            if "prompt_tokens_details" in usage and "cached_tokens" in usage["prompt_tokens_details"]:
                norm_usage["cached_tokens"] += usage["prompt_tokens_details"]["cached_tokens"]
            if "input_tokens_details" in usage and "cached_tokens" in usage["input_tokens_details"]:
                norm_usage["cached_tokens"] += usage["input_tokens_details"]["cached_tokens"]
        else:
            norm_usage = None

        entry = {
            "ts": ts,
            "method": self.command,
            "path": self.path,
            "status": 502 if error else 200,  # Simplified for error cases
            "request_bytes": req_bytes,
            "usage": norm_usage,
        }
        if error:
            entry["error"] = error

        with open(self.server.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


class RecordingProxy:
    def __init__(self, upstream: str, log_path: Path):
        self.upstream = upstream
        self.log_path = log_path
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingProxyHandler)
        self.server.upstream_url = upstream
        self.server.log_path = log_path
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def summary(self) -> dict:
        summary = {
            "requests": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "max_prompt_tokens": 0,
            "ok_responses": 0,
            "upstream_failures": 0,
        }
        if not self.log_path.exists():
            return summary

        with open(self.log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    summary["requests"] += 1
                    if "error" in entry:
                        summary["errors"] += 1
                    if entry.get("error") == "Upstream connection failed":
                        summary["upstream_failures"] += 1
                    if 200 <= (entry.get("status") or 0) < 300:
                        summary["ok_responses"] += 1
                    usage = entry.get("usage")
                    if usage:
                        summary["prompt_tokens"] += usage.get("prompt_tokens", 0)
                        summary["completion_tokens"] += usage.get("completion_tokens", 0)
                        summary["cached_tokens"] += usage.get("cached_tokens", 0)
                        summary["max_prompt_tokens"] = max(summary["max_prompt_tokens"], usage.get("prompt_tokens", 0))
                except json.JSONDecodeError:
                    pass

        return summary
