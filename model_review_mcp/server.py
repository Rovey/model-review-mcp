"""Model Review MCP server.

Shows 3D models in a local browser viewer where the user rotates the model, pins notes to exact
spots and sends them back. The agent receives each note with the part name, the position in model
units, the surface normal and a screenshot of the view with the numbered pins.

Tools:
    review_show_model(paths, title)        open or replace the model in the viewer
    review_get_feedback(wait, timeout_s)   read the notes the user sent

Run as a stdio MCP server:  uvx --from git+https://github.com/Rovey/model-review-mcp model-review-mcp
"""
import asyncio, json, os, time, webbrowser
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from model_review_mcp.scene import build_scene
from model_review_mcp.web import start_http, state, submitted

REVIEWS = Path(os.environ.get("MODEL_REVIEW_DIR", Path.home() / ".model-review" / "reviews"))
mcp = MCPServer(
    "model_review_mcp",
    version="1.0.0",
    instructions="Let the user review 3D models by pinning notes to exact spots. Workflow: review_show_model "
                 "with absolute file paths, ask the user to add notes and press 'Send feedback', then read them with "
                 "review_get_feedback(wait=true) or a background `curl -s <viewer url>wait`. Read the note images.",
)

MODEL_TYPES = {".stl", ".3mf", ".obj", ".ply", ".glb", ".off"}


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
