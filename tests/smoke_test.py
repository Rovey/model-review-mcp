"""End-to-end check without an MCP client or browser: load a model, hit the HTTP endpoints, post
feedback the way the viewer does, read it back through the tools, and check the guards.

    uv run python tests/smoke_test.py <model file>
"""
import asyncio, base64, json, os, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

os.environ.setdefault("MODEL_REVIEW_DIR", tempfile.mkdtemp(prefix="model-review-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_review_mcp import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

server.webbrowser.open = lambda url: None  # no browser during the test
port = server.start_http()
base = f"http://127.0.0.1:{port}"


def status(url, data=None, headers=None):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data, headers or {}), timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def raises_tool_error(fn, *args):
    try:
        asyncio.run(fn(*args)) if asyncio.iscoroutinefunction(fn) else fn(*args)
    except ToolError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} did not raise ToolError")


# tool errors carry a next step for the agent
assert "review_show_model first" in raises_tool_error(server.review_get_feedback)
assert "File not found" in raises_tool_error(server.review_show_model, [str(Path(sys.argv[1]).with_name("missing.stl"))])
assert "Unsupported file type" in raises_tool_error(server.review_show_model, [__file__])

print(server.review_show_model([sys.argv[1]], "smoke test"))
meta = json.load(urllib.request.urlopen(base + "/meta"))
assert meta["version"] == 1 and meta["parts"], meta
assert urllib.request.urlopen(base + "/model.glb").read()[:4] == b"glTF"
assert b"three@" in urllib.request.urlopen(base + "/").read()

# feedback round trip, through both the tool and the long poll
jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xd9").decode()
note = {"n": 1, "text": "make this wall thicker", "part": meta["parts"][0], "point": [1, 2, 3], "normal": [0, 0, 1], "image": jpeg}
body = json.dumps({"overall": "looks good", "view": jpeg, "notes": [note]}).encode()
assert status(base + "/comments", body, {"Content-Type": "application/json", "Origin": base}) == 200

feedback = json.loads(asyncio.run(server.review_get_feedback(wait=True, timeout_s=5)))
assert feedback["notes"][0]["text"] == "make this wall thicker", feedback
assert Path(feedback["notes"][0]["image"]).is_file() and Path(feedback["view"]).is_file(), feedback
assert feedback["overall"] == "looks good"
waited = json.load(urllib.request.urlopen(base + "/wait?after=0", timeout=5))  # 1 submission > 0: answers at once
assert waited == feedback, waited

# guards: malformed input, other websites, DNS rebinding
assert status(base + "/comments", b"{not json", {"Content-Type": "application/json"}) == 400
fake_image = json.dumps({"notes": [{**note, "image": "data:text/html;base64,PGgxPg=="}]}).encode()
assert status(base + "/comments", fake_image, {"Content-Type": "application/json"}) == 400
assert status(base + "/comments", body, {"Content-Type": "application/json", "Origin": "https://evil.example"}) == 403
assert status(base + "/meta", headers={"Host": f"evil.example:{port}"}) == 403
print("ok:", meta["parts"], "colours:", meta["colours"])
