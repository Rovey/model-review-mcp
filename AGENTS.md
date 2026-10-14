# AGENTS.md

Notes for coding agents working on this repository. User documentation is in README.md.

## Layout

| Path | Contents |
|---|---|
| `model_review_mcp/server.py` | MCP tools, model loading (incl. the 3MF reader), local HTTP server |
| `model_review_mcp/viewer.html` | The whole browser viewer: one file, three.js from a CDN, no build step |
| `tests/smoke_test.py` | End-to-end test: tools, HTTP endpoints, feedback round trip, security guards |
| `docs/viewer.png` | Screenshot used by the README |

## Commands

```sh
uv run python tests/smoke_test.py path/to/model.3mf   # must print "ok: ..."
uv run model-review-mcp                               # stdio MCP server, viewer on 127.0.0.1:8765
uv build                                              # wheel must contain viewer.html
```

Run the smoke test with a multi-part Bambu Studio 3MF and with a plain STL after any change to `server.py`.

## Rules

- **stdout is the MCP protocol.** Never print or log to stdout in the server; `Handler.log_message` is silenced for that reason.
- **Tool errors:** raise `ToolError` with a message that says what to do next. Do not return error strings.
- **Tool names** keep the `review_` prefix. Keep descriptions accurate to behaviour and keep the annotations true.
- **Notes reach the agent**, so they are untrusted input. Keep the `Host`/`Origin` check in `Handler.trusted` and the size and type checks in `clean_note` / `save_image`.
- **Coordinates:** files shown side by side are shifted on the X axis. The server sends the per-part offset in `/meta` and the viewer subtracts it, so notes stay in each file's own coordinates.
- **3MF:** use `load_3mf`, not `trimesh.load`. trimesh maps every component in a shared object file (how Bambu Studio saves multi-part objects) to the first mesh.
- **UI copy** is English and assistant-neutral ("your assistant", "Send feedback"). It never names a specific AI.
- **Viewer** stays a single file without a bundler. Colours are CSS custom properties with light and dark sets; the theme button cycles Auto, Light, Dark.

## Verifying UI changes

Start the server, open the viewer in a browser, click the model, add a note and press Send feedback. Check light and dark themes, a window narrower than 820 px, and an empty browser console.
