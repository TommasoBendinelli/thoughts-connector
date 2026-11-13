# SPDX-License-Identifier: GPL-2.0-or-later
"""thoughts-watch-replies (the server-side reply monitor) against a fake Thoughts server.

Needs the desktop's tools/mcp/agent_notes.py (THOUGHTS_AGENT_NOTES=<path>), as on the server
(~/connectors/agent_notes.py); skipped without it.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

AGENT_NOTES = os.environ.get("THOUGHTS_AGENT_NOTES")
pytestmark = pytest.mark.skipif(not AGENT_NOTES or not Path(AGENT_NOTES).is_file(),
                                reason="set THOUGHTS_AGENT_NOTES to the desktop's tools/mcp/agent_notes.py")
SCRIPT = Path(__file__).resolve().parents[1] / "thoughts-watch-replies"


def reply(sequence, message, photo=None):
    return {"reply_id": f"r{sequence}", "sequence": sequence, "note_id": "n1", "stroke_count": 0, "message": message,
            "photo": photo, "note": {"note_id": "n1", "title": "Grades 17:01"}}


class Feed:
    def __init__(self):
        self.replies = [reply(i, "old") for i in range(1, 61)]  # more than one page: none is announced
        self.queries = []
        self.lock = threading.Lock()
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                with feed.lock:
                    feed.queries.append(query)
                    after = int(query.get("after") or 0)
                    found = [r for r in feed.replies if r["sequence"] > after][:50]
                if not found and float(query.get("wait") or 0) > 0:
                    time.sleep(0.2)
                body = json.dumps({"replies": found, "cursor": max([r["sequence"] for r in found] + [after])}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_listens_for_the_connectors_notes_and_names_the_photo(tmp_path):
    feed = Feed()
    environment = {**os.environ, "THOUGHTS_SERVER_URL": feed.url, "THOUGHTS_TOKEN": "test-token",
                   "THOUGHTS_AGENT_NOTES_DIR": str(tmp_path / "notes"), "THOUGHTS_AGENT_NOTES": AGENT_NOTES}
    process = subprocess.Popen([sys.executable, str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=environment)
    lines = []
    threading.Thread(target=lambda: [lines.append(line.rstrip("\n")) for line in process.stdout], daemon=True).start()
    try:
        deadline = time.monotonic() + 10
        while not any(q.get("listen") == "true" for q in feed.queries) and time.monotonic() < deadline:
            time.sleep(0.05)
        with feed.lock:
            feed.replies.append(reply(61, "Redraw it", {"capture": "IMG_20261111_170138_123_ab12"}))
        while not any(line.startswith("REPLY") for line in lines) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
    finally:
        process.kill()
        process.wait(5)
    assert lines[0] == "LISTENING for replies to every note the connector sent", (lines, process.stderr.read())
    replies = [line for line in lines if line.startswith("REPLY")]
    assert replies == ["REPLY note=n1 title='Grades 17:01' strokes=0 photo=IMG_20261111_170138_123_ab12 "
                       "message='Redraw it'"]
    waits = [q for q in feed.queries if float(q.get("wait") or 0) > 0]
    assert waits and all(q.get("listen") == "true" and "conversation_id" not in q for q in waits)
