import json  # noqa: E501
import threading  # noqa: E501
import time  # noqa: E501
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E501


# noqa: E501
# noqa: E501
class FakeUpstreamHandler(BaseHTTPRequestHandler):  # noqa: E501
    def do_POST(self):  # noqa: E501
        content_length = int(self.headers.get("Content-Length", 0))  # noqa: E501
        body = self.rfile.read(content_length)  # noqa: E501
        self.server.requests.append(
            {  # noqa: E501
                "path": self.path,  # noqa: E501
                "headers": dict(self.headers),  # noqa: E501
                "body": body,  # noqa: E501
            }
        )  # noqa: E501
        # noqa: E501
        if self.path == "/v1/chat/completions_json":  # noqa: E501
            self.send_response(200)  # noqa: E501
            self.send_header("Content-Type", "application/json")  # noqa: E501
            self.end_headers()  # noqa: E501
            resp = {  # noqa: E501
                "id": "chatcmpl-123",  # noqa: E501
                "object": "chat.completion",  # noqa: E501
                "created": 1677652288,  # noqa: E501
                "model": "gpt-3.5-turbo-0125",  # noqa: E501
                "system_fingerprint": "fp_44709d6fcb",  # noqa: E501
                "choices": [
                    {  # noqa: E501
                        "index": 0,  # noqa: E501
                        "message": {  # noqa: E501
                            "role": "assistant",  # noqa: E501
                            "content": "Hello there.",  # noqa: E501
                        },  # noqa: E501
                        "logprobs": None,  # noqa: E501
                        "finish_reason": "stop",  # noqa: E501
                    }
                ],  # noqa: E501
                "usage": {  # noqa: E501
                    "prompt_tokens": 9,  # noqa: E501
                    "completion_tokens": 12,  # noqa: E501
                    "total_tokens": 21,  # noqa: E501
                    "prompt_tokens_details": {  # noqa: E501
                        "cached_tokens": 5  # noqa: E501
                    },  # noqa: E501
                },  # noqa: E501
            }  # noqa: E501
            self.wfile.write(json.dumps(resp).encode("utf-8"))  # noqa: E501
        # noqa: E501
        elif self.path == "/v1/chat/completions_sse":  # noqa: E501
            self.send_response(200)  # noqa: E501
            self.send_header("Content-Type", "text/event-stream")  # noqa: E501
            self.end_headers()  # noqa: E501
            events = [  # noqa: E501
                b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',  # noqa: E501
                b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',  # noqa: E501
                b'data: {"usage":{"prompt_tokens":10,"completion_tokens":15,"total_tokens":25,"prompt_tokens_details":{"cached_tokens":4}}}\n\n',  # noqa: E501
                b"data: [DONE]\n\n",  # noqa: E501
            ]  # noqa: E501
            for ev in events:  # noqa: E501
                self.wfile.write(ev)  # noqa: E501
                self.wfile.flush()  # noqa: E501
                time.sleep(0.3)  # noqa: E501
        # noqa: E501
        elif self.path == "/v1/messages_sse":  # noqa: E501
            self.send_response(200)  # noqa: E501
            self.send_header("Content-Type", "text/event-stream")  # noqa: E501
            self.end_headers()  # noqa: E501
            events = [  # noqa: E501
                b'event: message_start\ndata: {"message":{"usage":{"input_tokens":20,"output_tokens":1,"cache_read_input_tokens":10}}}\n\n',  # noqa: E501
                b'event: content_block_delta\ndata: {"delta":{"text":"Hi"}}\n\n',  # noqa: E501
                b'event: message_delta\ndata: {"usage":{"output_tokens":14}}\n\n',  # noqa: E501
                b"event: message_stop\ndata: {}\n\n",  # noqa: E501
            ]  # noqa: E501
            for ev in events:  # noqa: E501
                self.wfile.write(ev)  # noqa: E501
                self.wfile.flush()  # noqa: E501
        else:  # noqa: E501
            self.send_response(404)  # noqa: E501
            self.end_headers()  # noqa: E501


# noqa: E501
class FakeServer:  # noqa: E501
    def __init__(self):  # noqa: E501
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)  # noqa: E501
        self.server.requests = []  # noqa: E501
        self.url = f"http://127.0.0.1:{self.server.server_port}"  # noqa: E501
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)  # noqa: E501

    # noqa: E501
    def start(self):  # noqa: E501
        self.thread.start()  # noqa: E501

    # noqa: E501
    def stop(self):  # noqa: E501
        self.server.shutdown()  # noqa: E501
        self.server.server_close()  # noqa: E501
        self.thread.join()  # noqa: E501
