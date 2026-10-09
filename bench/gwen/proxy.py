import json
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


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
        body = self.rfile.read(content_length)
        if content_length > 0:
            headers["Content-Length"] = str(content_length)

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
                except json.JSONDecodeError:
                    pass

        # Try JSON parsing if it's not SSE
        content_type = resp.getheader("Content-Type", "")
        if "application/json" in content_type:
            resp_body = resp.read()
            self.wfile.write(resp_body)
            self.wfile.flush()
            check_usage(resp_body.decode("utf-8", errors="ignore"))
        else:
            # Stream SSE
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
                if line.startswith(b"data: "):
                    check_usage(line.decode("utf-8", errors="ignore"))

        self._log(start_ts, content_length, usage)
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
                    usage = entry.get("usage")
                    if usage:
                        summary["prompt_tokens"] += usage.get("prompt_tokens", 0)
                        summary["completion_tokens"] += usage.get("completion_tokens", 0)
                        summary["cached_tokens"] += usage.get("cached_tokens", 0)
                        summary["max_prompt_tokens"] = max(summary["max_prompt_tokens"], usage.get("prompt_tokens", 0))
                except json.JSONDecodeError:
                    pass

        return summary
