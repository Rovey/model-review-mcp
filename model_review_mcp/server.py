"""Model Review MCP server.

Shows 3D models in a local browser viewer where the user rotates the model, pins notes to exact
spots and sends them back. The agent receives each note with the part name, the position in model
units, the surface normal and a screenshot of the view with the numbered pins.

Tools:
    review_show_model(paths, title)        open or replace the model in the viewer
    review_get_feedback(wait, timeout_s)   read the notes the user sent

Run as a stdio MCP server:  uvx --from git+https://github.com/Rovey/model-review-mcp model-review-mcp
"""
import asyncio, base64, binascii, json, os, re, threading, time, webbrowser, zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated

import numpy as np
import trimesh
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

HERE = Path(__file__).parent
PORT = int(os.environ.get("MODEL_REVIEW_PORT", "8765"))
REVIEWS = Path(os.environ.get("MODEL_REVIEW_DIR", Path.home() / ".model-review" / "reviews"))
MAX_UPLOAD = 64 * 1024 * 1024  # screenshots are ~100-300 kB each

state = {"version": 0, "title": "", "glb": b"", "parts": [], "colours": {}, "offsets": {}, "dir": None, "feedback": None,
         "submissions": 0, "port": PORT, "last_seen": 0.0}
submitted = threading.Event()        # for get_feedback(wait=True)
new_feedback = threading.Condition()  # for GET /wait, which counts submissions instead of clearing a flag
mcp = MCPServer(
    "model_review_mcp",
    version="1.0.0",
    instructions="Let the user review 3D models by pinning notes to exact spots. Workflow: review_show_model "
                 "with absolute file paths, ask the user to add notes and press 'Send feedback', then read them with "
                 "review_get_feedback(wait=true) or a background `curl -s <viewer url>wait`. Read the note images.",
)

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


def load_3mf(path: Path) -> list[tuple[str, trimesh.Trimesh, str | None]]:
    """Return (name, mesh in plate coordinates, filament colour) for every part of every build item.

    A small reader of our own, because trimesh resolves every component that lives in a shared
    object file (the way Bambu Studio stores multi-part objects) to the first mesh in that file.
    Part names and filament colours come from the Bambu Studio / OrcaSlicer metadata when present.
    """
    z = zipfile.ZipFile(path)
    docs = {}

    def objects(p):
        if p not in docs:
            root = ET.fromstring(z.read(p.lstrip("/")))
            docs[p] = {o.get("id"): o for o in root.iter(CORE + "object")}, root
        return docs[p][0]

    def matrix(s):
        m = np.eye(4)
        if s:  # 3MF stores a row-vector 4x3 matrix: "m00 m01 m02 m10 ... m32"
            m[:3, :] = np.array(s.split(), dtype=float).reshape(4, 3).T
        return m

    names, parts, extruders, colours = {}, {}, {}, []
    if "Metadata/model_settings.config" in z.namelist():
        meta = lambda e, key: next((m.get("value") for m in e.findall("metadata") if m.get("key") == key), None)
        for o in ET.fromstring(z.read("Metadata/model_settings.config")).iter("object"):
            oid = o.get("id")
            names[oid] = meta(o, "name")
            parts[oid] = {pt.get("id"): meta(pt, "name") for pt in o.iter("part")}
            # a part without its own extruder prints with the object's
            extruders[oid] = {pt.get("id"): meta(pt, "extruder") or meta(o, "extruder") for pt in o.iter("part")}
            extruders[oid][None] = meta(o, "extruder")
    if "Metadata/project_settings.config" in z.namelist():
        colours = json.loads(z.read("Metadata/project_settings.config")).get("filament_colour", [])

    out = []

    def walk(p, oid, t, part_id):
        o = objects(p)[oid]
        mesh = o.find(CORE + "mesh")
        if mesh is not None:
            v = np.array([[float(e.get(a)) for a in "xyz"] for e in mesh.iter(CORE + "vertex")])
            f = np.array([[int(e.get(a)) for a in ("v1", "v2", "v3")] for e in mesh.iter(CORE + "triangle")])
            out.append((part_id, trimesh.Trimesh(v, f, process=False).apply_transform(t)))
            return
        for c in o.iter(CORE + "component"):
            walk(c.get(PROD + "path") or p, c.get("objectid"), t @ matrix(c.get("transform")), c.get("objectid"))

    main = "/3D/3dmodel.model"
    objects(main)
    for i, item in enumerate(docs[main][1].iter(CORE + "item")):
        start, oid = len(out), item.get("objectid")
        walk(main, oid, matrix(item.get("transform")), None)
        object_name = names.get(oid) or f"Object {i + 1}"
        for k in range(start, len(out)):
            part_name = parts.get(oid, {}).get(out[k][0]) or f"part {out[k][0]}"
            if len(out) - start == 1:
                label = object_name
            elif part_name.startswith(object_name):  # Bambu often names parts after the object
                label = part_name
            else:
                label = f"{object_name} / {part_name}"
            ext = extruders.get(oid, {}).get(out[k][0]) or extruders.get(oid, {}).get(None)
            colour = colours[int(ext) - 1] if ext and 0 < int(ext) <= len(colours) else None
            out[k] = (label, out[k][1], colour)
    return out


GAP = 20.0  # mm between files shown side by side


def build_scene(paths: list[str]) -> tuple[bytes, list[str], dict[str, str], dict[str, list[float]]]:
    """Load the files into one scene.

    Returns the GLB, the part names, the known part colours and, per part, the offset that was added
    to lay several files out side by side (slicer projects all sit in the middle of the plate and
    would overlap). The viewer subtracts the offset, so notes keep each file's own coordinates.
    """
    scene, colours, offsets, cursor = trimesh.Scene(), {}, {}, None
    for p in map(Path, paths):
        if p.suffix.lower() == ".3mf":
            meshes = load_3mf(p)
        else:
            loaded = trimesh.load(p)
            meshes = [(p.stem, loaded, None)] if isinstance(loaded, trimesh.Trimesh) else \
                [(f"{p.stem} / {g.metadata.get('name', i)}", g, None) for i, g in enumerate(loaded.dump())]
        lo = np.min([m.bounds[0] for _, m, _ in meshes], axis=0)
        hi = np.max([m.bounds[1] for _, m, _ in meshes], axis=0)
        shift = np.zeros(3) if cursor is None else np.array([cursor - lo[0], 0.0, 0.0])
        cursor = hi[0] + shift[0] + GAP
        for name, mesh, colour in meshes:
            name = re.sub(r"\.(stl|obj|ply|step|stp)$", "", name, flags=re.I)
            if len(paths) > 1 and name != p.stem:
                name = f"{p.stem}: {name}"
            scene.add_geometry(mesh.copy().apply_translation(shift), node_name=name, geom_name=name)
            offsets[name] = shift.round(4).tolist()
            if colour:
                colours[name] = colour[:7]  # "#RRGGBB" (drop a possible alpha byte)
    return scene.export(file_type="glb"), list(scene.graph.nodes_geometry), colours, offsets


MODEL_TYPES = {".stl", ".3mf", ".obj", ".ply", ".glb", ".off"}
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


@mcp.tool(annotations=ToolAnnotations(title="Show 3D model for review", read_only_hint=True,
                                      destructive_hint=False, idempotent_hint=True, open_world_hint=False))
def review_show_model(
    paths: Annotated[list[str], Field(min_length=1, max_length=20, description="Absolute paths of the model files to show")],
    title: Annotated[str, Field(max_length=200, description="Heading in the viewer; defaults to the file names")] = "",
) -> str:
    """Open 3D model files in a browser viewer where the user pins notes to exact spots on the model.

    Use this when you want the user's feedback on a 3D design, or when they ask to see a model.
    Supports STL, 3MF (including Bambu Studio / OrcaSlicer projects: part names, plate positions and
    filament colours are kept), OBJ, PLY and GLB. Several files are laid out side by side.
    Calling it again replaces the model and reloads an open viewer tab.
    Next step: wait for the user's notes with review_get_feedback(wait=true), or run
    `curl -s <viewer url>wait` as a background command if your client can.
    """
    bad = [p for p in paths if Path(p).suffix.lower() not in MODEL_TYPES]
    if bad:
        raise ToolError(f"Unsupported file type: {', '.join(bad)}. Use one of: {', '.join(sorted(MODEL_TYPES))}.")
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        raise ToolError(f"File not found: {', '.join(missing)}. Pass absolute paths to existing files.")
    try:
        glb, parts, colours, offsets = build_scene(paths)
    except Exception as e:
        raise ToolError(f"Could not read the model ({type(e).__name__}: {e}). Check that the file is a valid, "
                        "non-empty mesh; for slicer projects, save the project in the slicer again.") from e
    state.update(glb=glb, parts=parts, colours=colours, offsets=offsets, feedback=None,
                 title=title or ", ".join(Path(p).stem for p in paths),
                 dir=REVIEWS / time.strftime("%Y%m%d-%H%M%S"))
    state["dir"].mkdir(parents=True, exist_ok=True)
    state["version"] += 1
    submitted.clear()
    url = f"http://127.0.0.1:{state['port']}/"
    tab_open = time.time() - state["last_seen"] < 5
    if not tab_open:
        webbrowser.open(url)
    return (f"Showing {len(parts)} part(s) at {url} ({'reloaded the open tab' if tab_open else 'opened a browser tab'}): "
            f"{', '.join(parts)}.\nAsk the user to click spots on the model, add notes and press 'Send feedback'. "
            f"To be notified when they do, run `curl -s {url}wait` as a background command if your client "
            "supports that (it prints the feedback JSON); otherwise call review_get_feedback(wait=true).")


@mcp.tool(annotations=ToolAnnotations(title="Get review feedback", read_only_hint=True,
                                      destructive_hint=False, idempotent_hint=True, open_world_hint=False))
async def review_get_feedback(
    wait: Annotated[bool, Field(description="Block until the user presses 'Send feedback'")] = False,
    timeout_s: Annotated[int, Field(ge=1, le=3600, description="Maximum wait in seconds when wait=true")] = 600,
) -> str:
    """Return the notes the user sent from the viewer for the model shown by review_show_model.

    Result (JSON): model, overall (free-text note), view (path to a screenshot of the final view) and
    notes, each with n, text, part, point [x, y, z] and normal [x, y, z] in the file's own units (mm for
    printer files), and image (path to a screenshot with the numbered pins as the user saw them).
    Read the images to see what the user pointed at. Each submission holds the full set of notes.
    """
    if state["version"] == 0:
        raise ToolError("No model is open. Call review_show_model first.")
    if wait and not submitted.is_set():
        await asyncio.to_thread(submitted.wait, timeout_s)
    if not state["feedback"]:
        waited = f" within {timeout_s} s" if wait else ""
        return (f"No feedback yet: the user has not pressed 'Send feedback'{waited}. "
                "Ask them to review the model, then call this again with wait=true.")
    submitted.clear()
    return json.dumps(state["feedback"], indent=2, ensure_ascii=False)


def main():
    start_http()
    mcp.run()


if __name__ == "__main__":
    main()
