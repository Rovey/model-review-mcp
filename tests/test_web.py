"""Feedback validation and the Host/Origin guard of the local HTTP server."""
import base64
import binascii
import http.client

import pytest

from model_review_mcp import server
from model_review_mcp.server import MAX_TEXT, clean_note, save_image

JPEG = b"\xff\xd8\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n"


def data_url(kind, raw):
    return f"data:{kind};base64," + base64.b64encode(raw).decode()


@pytest.mark.parametrize("empty", [None, ""])
def test_save_image_without_image(tmp_path, empty):
    assert save_image(empty, tmp_path / "x.jpg") is None
    assert not (tmp_path / "x.jpg").exists()


@pytest.mark.parametrize("kind, raw", [("image/jpeg", JPEG), ("image/png", PNG)])
def test_save_image_accepts_jpeg_and_png(tmp_path, kind, raw):
    path = tmp_path / "view.jpg"
    assert save_image(data_url(kind, raw), path) == str(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("url", [
    data_url("image/gif", b"GIF89a"),
    data_url("text/html", b"<h1>"),
    data_url("image/svg+xml", b"<svg/>"),
    "data:image/png;base64,AAAA\nAAAA",
    "data:image/jpeg,raw",
    "http://example.invalid/a.jpg",
])
def test_save_image_rejects_other_data(tmp_path, url):
    with pytest.raises(ValueError, match="^images must be base64 JPEG or PNG data URLs$"):
        save_image(url, tmp_path / "x.jpg")
    assert not (tmp_path / "x.jpg").exists()


def test_save_image_rejects_bad_base64_padding(tmp_path):
    with pytest.raises(binascii.Error):
        save_image("data:image/png;base64,abc", tmp_path / "x.jpg")


def test_clean_note_shape(tmp_path):
    note = clean_note({"n": "3", "text": "thicker wall", "part": "Bracket", "point": [1, "2", 3.5],
                       "normal": [0, 0, 1], "image": data_url("image/jpeg", JPEG), "extra": "dropped"}, tmp_path)
    assert note == {"n": 3, "text": "thicker wall", "part": "Bracket", "point": [1.0, 2.0, 3.5],
                    "normal": [0.0, 0.0, 1.0], "image": str(tmp_path / "note_3.jpg")}
    assert (tmp_path / "note_3.jpg").read_bytes() == JPEG


def test_clean_note_defaults_and_truncation(tmp_path):
    note = clean_note({"n": 1.9, "text": "x" * (MAX_TEXT + 5), "part": "p" * 400}, tmp_path)
    assert note == {"n": 1, "text": "x" * MAX_TEXT, "part": "p" * 300, "point": None, "normal": None, "image": None}
    assert MAX_TEXT == 10_000
    assert clean_note({"n": 2}, tmp_path) == {"n": 2, "text": "", "part": "", "point": None, "normal": None, "image": None}


@pytest.mark.parametrize("vector", [[1, 2], [1, 2, 3, 4], (1, 2, 3), "1,2,3", {"x": 1}, None])
def test_clean_note_drops_vectors_that_are_not_three_item_lists(tmp_path, vector):
    note = clean_note({"n": 1, "point": vector, "normal": vector}, tmp_path)
    assert note["point"] is None and note["normal"] is None


def test_clean_note_rejects_bad_input(tmp_path):
    with pytest.raises(KeyError):
        clean_note({"text": "no number"}, tmp_path)
    with pytest.raises(ValueError):
        clean_note({"n": 1, "point": [1, 2, "x"]}, tmp_path)
    with pytest.raises(ValueError):
        clean_note({"n": 1, "image": data_url("text/html", b"<h1>")}, tmp_path)


def test_clean_note_png_is_stored_under_a_jpg_name(tmp_path):
    note = clean_note({"n": 4, "image": data_url("image/png", PNG)}, tmp_path)
    # Current behavior, see tidy-project report: PNG bytes are written to note_4.jpg
    assert note["image"] == str(tmp_path / "note_4.jpg")
    assert (tmp_path / "note_4.jpg").read_bytes() == PNG


def test_clean_note_turns_null_text_into_the_word_none(tmp_path):
    note = clean_note({"n": 1, "text": None, "part": None}, tmp_path)
    # Current behavior, see tidy-project report: JSON null becomes the string "None"
    assert (note["text"], note["part"]) == ("None", "None")


@pytest.fixture(scope="module")
def port(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "PORT", 0)  # free port; never the live viewer's 8765
        mp.setattr(server, "REVIEWS", tmp_path_factory.mktemp("reviews"))
        mp.setattr(server.webbrowser, "open", lambda url: None)
        mp.setitem(server.state, "dir", None)
        yield server.start_http()


def request(port, method, path, headers, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_start_http_uses_a_free_port(port):
    assert port not in (0, 8765, 8766)
    assert server.state["port"] == port


@pytest.mark.parametrize("host, origin", [
    ("127.0.0.1:{port}", None),
    ("localhost:{port}", None),
    ("127.0.0.1:{port}", "http://127.0.0.1:{port}"),
    ("127.0.0.1:{port}", "http://localhost:{port}"),
])
def test_guard_allows_local_host_and_origin(port, host, origin):
    headers = {"Host": host.format(port=port)}
    if origin:
        headers["Origin"] = origin.format(port=port)
    status, body = request(port, "GET", "/meta", headers)
    assert status == 200 and body.startswith(b'{"version": ')


@pytest.mark.parametrize("host, origin", [
    ("evil.example:{port}", None),               # DNS rebinding
    ("127.0.0.1:1", None),                       # other port
    ("127.0.0.1", None),                         # no port
    (None, None),                                # no Host header
    ("127.0.0.1:{port}", "https://evil.example"),
    ("127.0.0.1:{port}", "https://127.0.0.1:{port}"),  # only the http:// prefix is accepted
    ("127.0.0.1:{port}", "null"),
])
def test_guard_rejects_foreign_host_or_origin(port, host, origin):
    headers = {}
    if host:
        headers["Host"] = host.format(port=port)
    if origin:
        headers["Origin"] = origin.format(port=port)
    assert request(port, "GET", "/meta", headers) == (403, b"Forbidden")
    assert request(port, "POST", "/comments", headers, b'{"notes": []}') == (403, b"Forbidden")


def test_guard_passes_post_from_the_viewer(port):
    local = f"127.0.0.1:{port}"
    # passes the guard and stops at the next check: no model is open
    status, body = request(port, "POST", "/comments", {"Host": local, "Origin": f"http://{local}"}, b'{"notes": []}')
    assert (status, body) == (409, b"No model is open")
