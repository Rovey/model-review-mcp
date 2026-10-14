"""Local HTTP server for the browser viewer: serves the model and receives the user's notes."""
import base64, binascii, json, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = int(os.environ.get("MODEL_REVIEW_PORT", "8765"))
MAX_UPLOAD = 64 * 1024 * 1024  # screenshots are ~100-300 kB each

state = {"version": 0, "title": "", "glb": b"", "parts": [], "colours": {}, "offsets": {}, "dir": None, "feedback": None,
         "submissions": 0, "port": PORT, "last_seen": 0.0}
submitted = threading.Event()        # for get_feedback(wait=True)
new_feedback = threading.Condition()  # for GET /wait, which counts submissions instead of clearing a flag

MAX_NOTES, MAX_TEXT = 200, 10_000


def save_image(data_url, path: Path) -> str | None:
    """Store a `data:image/jpeg|png;base64,...` URL from the viewer; anything else is rejected."""
    if not data_url:
        return None
    m = re.fullmatch(r"data:image/(?:jpeg|png);base64,([A-Za-z0-9+/=]+)", data_url)
    if not m:
        raise ValueError("images must be base64 JPEG or PNG data URLs")
    path.write_bytes(base64.b64decode(m.group(1), validate=True))
    return str(path)


def clean_note(n: dict, out: Path) -> dict:
    """Validate one note from the viewer; the text reaches the agent, so keep the shape strict."""
    vec = lambda v: [float(x) for x in v] if isinstance(v, list) and len(v) == 3 else None
    note = {
        "n": int(n["n"]),
        "text": str(n.get("text", ""))[:MAX_TEXT],
        "part": str(n.get("part", ""))[:300],
        "point": vec(n.get("point")),
        "normal": vec(n.get("normal")),
    }
    note["image"] = save_image(n.get("image"), out / f"note_{note['n']}.jpg")
    return note


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # stdout carries the MCP protocol; keep it clean
        pass

    def reply(self, body: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self) -> bool:
        """Block DNS rebinding (foreign Host) and cross-site requests (foreign Origin).

        Browsers send Origin on cross-origin requests, so a web page elsewhere cannot read the model
        or inject notes. Local tools such as curl send no Origin and are allowed.
        """
        local = {f"127.0.0.1:{state['port']}", f"localhost:{state['port']}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in local or (origin and origin.removeprefix("http://") not in local):
            self.reply(b"Forbidden", "text/plain", 403)
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        if self.path == "/":
            self.reply((HERE / "viewer.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/model.glb"):
            self.reply(state["glb"], "model/gltf-binary")
        elif self.path == "/meta":
            state["last_seen"] = time.time()
            self.reply(json.dumps({k: state[k] for k in ("version", "title", "parts", "colours", "offsets")}).encode(), "application/json")
        elif self.path.startswith("/wait"):
            # Long poll for agents that can run a background command and get notified when it exits
            # (e.g. `curl http://127.0.0.1:8765/wait`): answers with the feedback as soon as a submission
            # newer than ?after=N arrives (default: the current count), or 204 after an hour.
            m = re.search(r"after=(\d+)", self.path)
            after = int(m.group(1)) if m else state["submissions"]
            with new_feedback:
                new_feedback.wait_for(lambda: state["submissions"] > after, timeout=3600)
            if state["submissions"] > after:
                self.reply(json.dumps(state["feedback"], indent=2, ensure_ascii=False).encode(), "application/json")
            else:
                self.reply(b"", "application/json", 204)
        else:
            self.reply(b"Not found", "text/plain", 404)

    def do_POST(self):
        if not self.trusted():
            return
        if self.path != "/comments":
            return self.reply(b"Not found", "text/plain", 404)
        if not state["dir"]:
            return self.reply(b"No model is open", "text/plain", 409)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self.reply(b"Request too large", "text/plain", 413)
        try:
            data = json.loads(self.rfile.read(length))
            out = Path(state["dir"])
            raw = data.get("notes", [])
            if not isinstance(raw, list) or len(raw) > MAX_NOTES:
                raise ValueError(f"notes must be a list of at most {MAX_NOTES}")
            feedback = {
                "model": state["title"],
                "overall": str(data.get("overall", ""))[:MAX_TEXT],
                "view": save_image(data.get("view"), out / "view.jpg"),
                "notes": [clean_note(n, out) for n in raw],
            }
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as e:
            return self.reply(f"Invalid feedback: {e}".encode(), "text/plain", 400)
        (out / "feedback.json").write_text(json.dumps(feedback, indent=2, ensure_ascii=False), encoding="utf-8")
        state["feedback"] = feedback
        submitted.set()
        with new_feedback:
            state["submissions"] += 1
            new_feedback.notify_all()
        self.reply(b'{"ok": true}', "application/json")


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows, reuse would let a second session silently share the port


def start_http() -> int:
    """Serve the viewer in a background thread on PORT, or on a free port when PORT is taken."""
    try:
        httpd = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        httpd = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["port"] = httpd.server_address[1]
    return state["port"]
