# SPDX-License-Identifier: GPL-2.0-or-later
"""The Thoughts connector against a fake Thoughts server that records every request.

Checks the tools end to end through OAuth and MCP, and that the connector only ever calls the
endpoints it is allowed to (reads, agent notes, the source publications of thoughts-cli, and the
two Library-project actions and the build inputs of create_document) with its own token. The proposal tools need thoughts-cli: THOUGHTS_CLI, or a thoughts-server checkout
with clients/thoughts-cli next to this repository.
"""
import base64
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("mcp")
import httpx  # noqa: E402

os.environ.setdefault("CONNECTOR_EXTRA_REDIRECTS", r"^http://localhost:\d+/callback$")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connector_core  # noqa: E402
import thoughts_connector  # noqa: E402

CLI = Path(os.environ.get("THOUGHTS_CLI", Path(__file__).resolve().parents[2] / "thoughts-server" / "clients" / "thoughts-cli"))
HAVE_CLI = (CLI / "thoughts_cli" / "__init__.py").is_file()
thoughts_connector.THOUGHTS_CLI = CLI


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT, THOUGHTS_PORT = free_port(), free_port()
BASE = f"http://127.0.0.1:{PORT}"
TMP = Path(tempfile.mkdtemp(prefix="thoughts-connector-test-"))
TOKEN = "t" * 43
PASSPHRASE = "wxyz-2345-abcd-efgh-jkmn"
REDIRECT = "http://localhost:5555/callback"
CARD = "00aabbcc-d1e2-4f3a-8b4c-5d6e7f8a9b0c"
GONE = "11111111-1111-4111-8111-111111111111"
RETIRED = "33333333-3333-4333-8333-333333333333"
UNPUBLISHED = "44444444-4444-4444-8444-444444444444"
NOTE = "8fabcdef-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
PHOTO_NOTE = "5d0c1a2b-3e4f-4a5b-8c6d-7e8f9a0b1c2d"
JPEG = b"\xff\xd8\xff\xe0" + b"glasses photo"
QUERIES: list[tuple[str, dict]] = []  # (path, query) of agent-note reads
SEND_ANSWER: dict = {}  # extra keys of the next POST /v1/agent-notes answer
LIKED, DISLIKED, REMOVED, OTHER = (f"{n}d2f4a51-7c0e-4b8a-a6f1-3e5b2c7d8e90" for n in "1234")
GONE_DOC = "c1abcdef-2a3b-4c4d-9e5f-6a7b8c9d0e1f"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def feedback(feedback_id: str, document_id: str, pdf_sha256: str, created_at: str, *, polarity="positive",
             comment="", page=1, deleted=False) -> dict:
    """A document_feedback projection (thoughts-server protocol/document-feedback.md)."""
    return {"entity_type": "document_feedback", "entity_id": feedback_id, "collection": "document-feedback",
            "deleted": deleted, "payload": {
                "schema_version": 1, "feedback_id": feedback_id, "polarity": polarity, "comment": comment,
                "anchor": {"version": 1, "quote": {"exact": f"passage {feedback_id[0]}", "prefix": "", "suffix": ""},
                           "pdf": {"document_id": document_id, "pdf_sha256": pdf_sha256,
                                   "regions": [{"page": page, "rect": [0.1, 0.5, 0.9, 0.52]},
                                               {"page": page + 1, "rect": [0.1, 0.0, 0.5, 0.02]}]}},
                "revision": 1, "created_at": created_at, "updated_at": created_at, "created_origin_id": "android-example-phone",
                "origin_id": "android-example-phone", "deleted": deleted}}


def obj(text: str) -> tuple[str, bytes]:
    data = text.encode()
    return hashlib.sha256(data).hexdigest(), data


MAIN = obj("\\begin{document}\n\\docTitle{FindingsPaper}\n\\input{tex/fragments/results}\nThe end.\n\\end{document}\n")
RESULTS = obj("\\section{Results}\n\\ReviewCardMaskStart{" + CARD + "}{0}%\nAccuracy falls as noise grows.\n"
              "\\ReviewCardMaskEnd{" + CARD + "}{0}%\nMore text about bananas.\n")
NOTES_MD = obj("# Ideas\n\nA note about two techniques.\n")
PAPER = obj("%PDF-1.4 an imported paper")
OBJECTS = dict([MAIN, RESULTS, NOTES_MD, PAPER])
SOURCES = [
    {"path": "docs-rp/tex/source/main.tex", "revision": 1, "sha256": MAIN[0], "object_sha256": MAIN[0], "deleted": False},
    {"path": "docs-rp/tex/fragments/results.tex", "revision": 1, "sha256": RESULTS[0], "object_sha256": RESULTS[0],
     "deleted": False},
    {"path": "docs-ideas/markdown/source/ideas.md", "revision": 1, "sha256": NOTES_MD[0], "object_sha256": NOTES_MD[0],
     "deleted": False},
]
EMPTY = hashlib.sha256(b"").hexdigest()
NOTES_FOLDER = ".thoughts/projects/" + hashlib.sha256(b"p3").hexdigest() + "/default"
PROJECTS = {"schema_version": 1, "projects": [
    {"id": "p1", "name": "Research", "documents": [
        {"document_id": "doc-rp", "name": "FindingsPaper", "source_file": "docs-rp/tex/source/main.tex",
         "source_project": "docs-rp", "sha256": "a" * 64, "page_count": 9, "included": True}],
     # Unpublished: kept in its Project, its cards not offered for review.
     "unpublished_documents": [
        {"document_id": "doc-hidden", "name": "Hidden", "filename": "hidden.pdf", "imported": True,
         "sha256": "c" * 64, "page_count": 2, "included": False, "published": False}]},
    {"id": "p2", "name": "ideas", "documents": [
        {"document_id": "doc-ideas", "name": "Ideas", "filename": "ideas.pdf", "source_file": "docs-ideas/markdown/source/ideas.md",
         "source_project": "docs-ideas", "sha256": "b" * 64, "page_count": 1, "included": True}]},
    # Where create_document adds notes: its default folder, from its manifest, with an imported paper.
    {"id": "p3", "name": "Notes", "revision": 4, "documents": [], "unpublished_documents": [],
     "project_configuration": {"id": "p3", "name": "Notes", "default_folder_id": "f3", "document_names": {},
                               "source_folders": [{"folder_id": "f3", "path": NOTES_FOLDER}],
                               "documents": [{"document_id": "paper", "format": "pdf", "included": True,
                                              "source": {"folder_id": "f3", "path": "Paper_p3.pdf"}}],
                               "rendering": {"paper": {"sha256": PAPER[0]}}}},
]}
PROJECTIONS = {"projections": [
    {"entity_type": "review_card", "entity_id": UNPUBLISHED, "deleted": False, "payload": {
        "card_id": UNPUBLISHED, "created_at": "2026-10-13T00:00:00Z", "primary": {"document_id": "doc-hidden", "question": "Hidden one?"}}},
    {"entity_type": "review_card", "entity_id": CARD, "deleted": False, "payload": {
        "card_id": CARD, "created_at": "2026-10-15T17:26:39Z", "primary": {
            "document_id": "doc-rp", "page": 7, "question": "What are the current results?",
            "source_anchor": {"segments": [{"segment_id": "0", "source_file": "docs-rp/tex/fragments/results.tex"}]}}}},
    {"entity_type": "review_card", "entity_id": GONE, "deleted": False, "payload": {
        "card_id": GONE, "created_at": "2026-10-16T00:00:00Z", "primary": {"document_id": "doc-rp", "question": "Deleted one?"}}},
    {"entity_type": "review_event", "entity_id": "e1", "deleted": False, "payload": {
        "card_id": CARD, "rating": "good", "reviewed_at": "2026-10-16T09:00:00Z", "review_tags": ["Partly", "Recalled"],
        "review_group_notes": {"note": "mixed up the axes"}}},
    {"entity_type": "review_event", "entity_id": "e2", "deleted": False, "payload": {
        "card_id": CARD, "rating": "easy", "reviewed_at": "2026-10-20T09:00:00Z", "review_tags": ["I get it", "Know by heart"]}},
    {"entity_type": "review_event", "entity_id": "e3", "deleted": False, "payload": {"card_id": GONE, "rating": "delete"}},
    # CARD was retired and the retirement undone; RETIRED was retired, restored and retired again.
    {"entity_type": "review_event", "entity_id": "e4", "deleted": False, "payload": {
        "event_id": "e4", "card_id": CARD, "queue_action": "retire", "reviewed_at": "2026-10-21T09:00:00Z"}},
    {"entity_type": "review_event", "entity_id": "e5", "deleted": False, "payload": {
        "event_id": "e5", "card_id": CARD, "queue_action": "undo_retire", "undone_event_id": "e4",
        "reviewed_at": "2026-10-21T09:00:02Z"}},
    {"entity_type": "review_card", "entity_id": RETIRED, "deleted": False, "payload": {
        "card_id": RETIRED, "created_at": "2026-10-14T00:00:00Z", "primary": {"document_id": "doc-rp", "question": "Retired one?"}}},
    {"entity_type": "review_event", "entity_id": "e8", "deleted": False, "payload": {
        "event_id": "e8", "card_id": RETIRED, "queue_action": "retire", "reviewed_at": "2026-10-27T11:00:00Z"}},
    {"entity_type": "review_event", "entity_id": "e6", "deleted": False, "payload": {
        "event_id": "e6", "card_id": RETIRED, "rating": "good", "reviewed_at": "2026-10-20T09:00:00Z", "review_tags": ["Recalled"]}},
    {"entity_type": "review_event", "entity_id": "e7", "deleted": False, "payload": {
        "event_id": "e7", "card_id": RETIRED, "queue_action": "restore", "reviewed_at": "2026-10-26T09:00:00Z"}},
    {"entity_type": "review_event", "entity_id": "e9", "deleted": False, "payload": {
        "event_id": "e9", "card_id": RETIRED, "queue_action": "retire", "reviewed_at": "2026-10-25T09:00:00Z"}},
    {"entity_type": "logical_comment", "entity_id": "c1", "deleted": False, "payload": {}},
    # Document feedback: listed oldest first, whatever the offset of created_at.
    feedback(DISLIKED, "doc-rp", "a" * 64, "2026-10-28T10:30:00.000+02:00", polarity="negative",
             comment="Wrong sign.", page=3),
    feedback(LIKED, "doc-rp", "f" * 64, "2026-10-28T08:52:03.512Z"),
    feedback(REMOVED, "doc-rp", "a" * 64, "2026-10-27T08:00:00.000Z", deleted=True),
    feedback(OTHER, GONE_DOC, "e" * 64, "2026-10-27T08:00:00.000Z"),
]}
MEASURED = TMP / "measured_links.json"  # what the stub renderer measures: {"css_height", "links"}
REQUESTS: list[tuple[str, str, str]] = []  # method, path, Authorization
POSTED: list[dict] = []
PUBLISHED: list[dict] = []
LIBRARY_CHANGES: list[dict] = []
URL_IMPORTS: list[dict] = []  # bodies of POST /v1/library/url-imports
OPERATIONS: list[dict] = []  # thoughts-cli's review-card operations
CAPABILITIES = ["client_review_sources_v1", "client_review_source_replacement_v1", "client_review_card_move_v1",
                "library_import_pdf_v1", "review_card_merge_v1", "review_card_pending_moves_v1", "agent_note_links_v1",
                "review_card_move_pairs_v1", "review_card_merge_pdf_v1",
                "agent_note_claims_v1", "agent_note_owners_v1", "agent_note_rules_v1", "mental_model_v1",
                "library_project_source_folder_v1"]
# Who answers: claims (path, body), how the fake answers each item, topic owners posted, deletions.
CLAIMS: list[tuple[str, dict]] = []
CLAIM_OUTCOMES: dict[str, str] = {}  # item id -> "yours" (default), "responder", "not_covering"
OWNERS_POSTED: list[dict] = []
DELETES: list[tuple[str, dict]] = []
RULES_TEXT = "# Answering rules\nAnswer in plain words.\n"
MENTAL_MODEL_TEXT = "# Mental Model\n\n## Firm ground to build on\n\nSorting [0190a00c](thoughts-card:0190a00c-0000-7000-8000-00000000000c).\n"
CLAIM_NOTE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
CLAIM_REPLY = "4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a"
CLAIM_PAGE = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
PENDING_MOVES: dict[str, tuple[dict, dict]] = {}  # request_id -> (request, current view); POST /v1/review-card-pending-moves
MERGES: list[dict] = []  # bodies of POST /v1/review-card-merges
MERGE_RECEIPTS: dict[str, tuple[dict, dict]] = {}  # request_id -> (request, first answer)
SNAPSHOTS: dict[str, dict] = {}  # target -> its latest build-input snapshot
REFUSE_SNAPSHOTS: list[bool] = []  # non-empty: the server refuses build-input snapshots
CONFLICTS: list[dict] = []
HTML_PAGES: dict[str, dict] = {}  # path -> {path, folder, sha256, size_bytes}
HTML_CHANGES: list[dict] = []
RENDER_STATUS: dict[str, dict] = {}  # target -> GET /v1/render-status answer (default: synced)
RESOLVED: list[tuple[str, dict]] = []
NOTE_RECORD = {"note_id": NOTE, "title": "Whiteboard", "message": "your plot", "created_at": "2026-10-21T14:34:39Z",
               "sender_device_id": "thoughts-mac", "media_type": "image/png", "opened_at": None,
               "replies": [{"reply_id": "r1", "created_at": "2026-10-21T15:00:00Z", "message": "like this?",
                            "image_url": "/v1/agent-notes/replies/r1/image", "media_type": "image/png", "stroke_count": 12}]}
CLAIM_NOTE_RECORD = {"note_id": CLAIM_NOTE, "title": "Eigenvalues", "message": None, "created_at": "2026-01-05T10:00:00Z",
                     "sender_device_id": "connector", "media_type": "image/png", "opened_at": None,
                     "origin": {"agent": "connector", "conversation_id": "conversation-1"},
                     "replies": [{"reply_id": CLAIM_REPLY, "created_at": "2026-01-05T10:05:00Z", "message": "Why 3?",
                                  "image_url": f"/v1/agent-notes/replies/{CLAIM_REPLY}/image", "media_type": "image/png",
                                  "stroke_count": 1}]}
CLAIM_PAGE_RECORD = {"note_id": CLAIM_PAGE, "title": "Linear algebra · page 2", "message": "What next?",
                     "created_at": "2026-01-05T10:06:00Z", "sender_device_id": "phone", "media_type": "image/png",
                     "opened_at": None, "origin": {"kind": "phone_page", "page": 2}, "replies": []}
PHOTO_NOTE_RECORD = {"note_id": PHOTO_NOTE, "title": "Sketch", "message": None, "created_at": "2026-11-11T13:51:58Z",
                     "sender_device_id": "connector", "media_type": "image/png", "opened_at": "2026-11-11T13:52:00Z",
                     "replies": [{"reply_id": "r2", "created_at": "2026-11-11T16:01:38Z",
                                  "message": "Redraw it from this picture",
                                  "image_url": "/v1/agent-notes/replies/r2/image", "media_type": "image/png", "stroke_count": 0,
                                  "sources": "linked_pdf",
                                  "photo": {"url": "/v1/agent-notes/replies/r2/photo", "media_type": "image/jpeg",
                                            "capture": "IMG_20261111_170138_123_ab12"}}]}


def fake_folder(project: dict, label: str, operation_id: str) -> str:
    """A docs folder of the Project's own, as the server's add_source_folder names and lists it."""
    folder = "docs-" + label.lower().replace(" ", "-") + "-" + operation_id.replace("-", "")[:6]
    project["project_configuration"]["source_folders"].append(
        {"folder_id": "f-" + folder, "path": folder, "document_id": operation_id})
    project.setdefault("source_folders", []).append({"project": folder, "folder": label})
    return folder


def fake_plan(origin: dict, body: dict) -> dict:
    """What plan_move_source_document answers for a note of origin's docs folder."""
    document = next(d for d in origin["documents"] if d["document_id"] == body["document_id"])
    destination = next(p for p in PROJECTS["projects"] if p["id"] == body["destination_project_id"])
    configuration = destination["project_configuration"]
    folders = [f["path"] for f in configuration["source_folders"] if f["folder_id"] != configuration["default_folder_id"]]
    folder = folders[0] if folders else "docs-" + destination["name"].lower() + "-faf6e6"
    relative = document["source_file"][len(document["source_project"]) + 1:]
    page = "tex/support/html/explainer.html"
    return {"action": "move_source_document", "document_id": document["document_id"], "name": document["name"],
            "source": {"project_id": origin["id"], "project_name": origin["name"], "folder": document["source_project"],
                       "path": relative, "revision": origin["revision"]},
            "destination": {"project_id": destination["id"], "project_name": destination["name"], "folder": folder,
                            "path": body.get("destination_path") or relative, "revision": destination["revision"],
                            "collision": False, "suggested_path": None,
                            **({} if folders else {"new_folder": True, "folder_name": destination["name"]})},
            "files": [{"source": "tex/support/manual_style.sty", "destination": "tex/support/manual_style.sty",
                       "action": "copy", "transfer": "fedora", "reason": "the new folder has no project style yet"}],
            "link_edits": {document["source_file"]: [], document["source_project"] + "/tex/source/matrices.tex": []},
            "html_pages": [{"source": document["source_project"] + "/" + page, "destination": folder + "/" + page,
                            "sha256": "e" * 64, "bytes": 10}],
            "markers": {"review_cards": 1}, "warnings": [], "plan_digest": "digest-" + folder}


class FakeThoughts(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, method="GET"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def handle_any(self, method):
        url = urlparse(self.path)
        REQUESTS.append((method, url.path, self.headers.get("Authorization", "")))
        if url.path.startswith("/v1/agent-notes"):
            QUERIES.append((url.path, {key: values[0] for key, values in parse_qs(url.query).items()}))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"detail": "Invalid device token"})
        path = url.path
        # What thoughts-cli uses: one source, object upload, the device sequence, publications.
        if method == "GET" and path.startswith("/v1/sources/"):
            record = next((s for s in SOURCES if s["path"] == path[len("/v1/sources/"):]), None)
            return self.reply(200, record) if record else self.reply(404, {"detail": "Source not found"})
        if method == "POST" and path == "/v1/objects":
            data = self.rfile.read(int(self.headers["Content-Length"]))
            digest = hashlib.sha256(data).hexdigest()
            if self.headers.get("X-Content-SHA256") != digest:
                return self.reply(422, {"detail": "digest mismatch"})
            OBJECTS[digest] = data
            return self.reply(200, {"sha256": digest, "size_bytes": len(data), "media_type": "text/plain"})
        if method == "GET" and path == "/v1/device-sequence":
            return self.reply(200, {"device_id": "connector", "latest_device_sequence": len(PUBLISHED),
                                    "next_device_sequence": len(PUBLISHED) + 1})
        if method == "POST" and path == "/v1/source-publications":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # The note first, then any figures thoughts-cli publishes with it (new sources), or the other
            # source of a card move (an existing one: both are published or neither).
            mutation, *others = body["mutations"]
            existing = [(other, next((s for s in SOURCES if s["path"] == other["path"]), None)) for other in others]
            for other, known in existing:
                if known is not None and (other["base_revision"], other["base_sha256"]) != (known["revision"], known["sha256"]):
                    return self.reply(409, {"detail": f"source publication conflicts at {other['path']}; no source was published"})
            figures = [other for other, known in existing if known is None]
            record = next((s for s in SOURCES if s["path"] == mutation["path"]), None)
            if record is None and (mutation["base_revision"], mutation["base_sha256"]) == (0, EMPTY):
                # A new note (create_document).
                record = {"path": mutation["path"], "revision": 0, "sha256": EMPTY, "object_sha256": EMPTY, "deleted": False}
                SOURCES.append(record)
            if record is None or (mutation["base_revision"], mutation["base_sha256"]) != (record["revision"], record["sha256"]):
                return self.reply(409, {"detail": f"source publication conflicts at {mutation['path']}; no source was published"})
            PUBLISHED.append(body)
            moved = []
            for change, known in [(mutation, record)] + [(other, known) for other, known in existing if known is not None]:
                known.update(revision=known["revision"] + 1, sha256=change["content_sha256"],
                             object_sha256=change["content_sha256"])
                moved.append({"outcome": "applied", "path": known["path"], "revision": known["revision"],
                              "sha256": known["sha256"], "object_sha256": known["sha256"]})
            return self.reply(200, {"publication_id": body["publication_id"], "source_frontier": len(PUBLISHED) + 1,
                                    "results": moved
                                    + [{"outcome": "applied", "path": figure["path"], "revision": 1,
                                        "sha256": figure["content_sha256"], "object_sha256": figure["content_sha256"]}
                                       for figure in figures]})
        if method == "POST" and path == "/v1/library/projects":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            LIBRARY_CHANGES.append(body)
            if body["action"] == "create":
                # As the server: new:<id> with its default folder, and its own docs folder when asked.
                if any(p["name"].casefold() == body["name"].casefold() for p in PROJECTS["projects"]):
                    return self.reply(409, {"detail": "A project with this name already exists"})
                default = {"folder_id": "default-" + body["project_id"], "path": ".thoughts/projects/x/default",
                           "document_id": body["operation_id"]}
                created = {"id": body["project_id"], "name": body["name"], "revision": 1, "documents": [],
                           "unpublished_documents": [], "source_folders": [], "project_configuration": {
                               "id": body["project_id"], "name": body["name"], "default_folder_id": default["folder_id"],
                               "document_names": {}, "source_folders": [default], "documents": [], "rendering": {}}}
                if "source_folder" in body:
                    fake_folder(created, body["source_folder"] or body["name"], body["operation_id"])
                PROJECTS["projects"].append(created)
                return self.reply(200, PROJECTS)
            project = next(p for p in PROJECTS["projects"] if p["id"] == body["project_id"])
            if body["action"] == "plan_move_source_document":
                return self.reply(200, {"plan": fake_plan(project, body)})
            if body["base_revision"] != project.setdefault("revision", 0):
                return self.reply(409, {"detail": "This project changed on another device. Sync and try again"})
            if body["action"] == "include_source":
                output = "document_" + hashlib.sha256(body["source_file"].encode()).hexdigest()[:20] + ".pdf"
                project["documents"].append({
                    "document_id": body["new_document_id"], "name": Path(body["source_file"]).stem,
                    "source_file": body["source_file"], "source_project": NOTES_FOLDER, "sha256": "", "page_count": 0,
                    "included": True, "filename": output})
                configuration = project["project_configuration"]
                configuration["documents"].append({
                    "document_id": body["new_document_id"], "format": "markdown", "included": True,
                    "source": {"folder_id": "f3", "path": body["source_file"][len(NOTES_FOLDER) + 1:]}})
                configuration["rendering"][body["new_document_id"]] = {"output": output}
            elif body["action"] == "rename_document":
                document = next(d for d in project["documents"] + project["unpublished_documents"]
                                if d["document_id"] == body["document_id"])
                document["name"] = project["project_configuration"]["document_names"][body["document_id"]] = body["name"]
            elif body["action"] == "set_document_published":
                lists = (project["unpublished_documents"], project["documents"])
                document = next(d for d in lists[0] + lists[1] if d["document_id"] == body["document_id"])
                for listed in lists:
                    if document in listed:
                        listed.remove(document)
                lists[body["published"]].append({**document, "included": body["published"]})
                next(d for d in project["project_configuration"]["documents"]
                     if d["document_id"] == body["document_id"])["included"] = body["published"]
            elif body["action"] == "move_document":
                destination = next(p for p in PROJECTS["projects"] if p["id"] == body["destination_project_id"])
                if body["destination_revision"] != destination.setdefault("revision", 0):
                    return self.reply(409, {"detail": "The destination project changed on another device. Sync and try again"})
                lists = [project.setdefault(field, []) for field in ("documents", "unpublished_documents")]
                document = next(d for d in lists[0] + lists[1] if d["document_id"] == body["document_id"])
                for listed in lists:
                    if document in listed:
                        listed.remove(document)
                destination.setdefault("unpublished_documents" if not document.get("included", True) else "documents",
                                       []).append(document)
                destination["revision"] += 1
            elif body["action"] == "add_source_folder":
                labels = [f["folder"] for f in project.get("source_folders", [])]
                if (body.get("name") or project["name"]) in labels:
                    return self.reply(409, {"detail": "Another source folder in this project already has this name"})
                fake_folder(project, body.get("name") or project["name"], body["operation_id"])
            elif body["action"] == "move_source_document":
                plan = fake_plan(project, body)
                destination = next(p for p in PROJECTS["projects"] if p["id"] == body["destination_project_id"])
                if body["plan_digest"] != plan["plan_digest"] or body["destination_revision"] != destination["revision"]:
                    return self.reply(409, {"detail": "The files this move needs changed after you reviewed it."})
                if plan["destination"].get("new_folder"):
                    fake_folder(destination, destination["name"], "faf6e6")
                document = next(d for d in project["documents"] if d["document_id"] == body["document_id"])
                project["documents"].remove(document)
                destination["documents"].append({**document, "source_project": plan["destination"]["folder"],
                                                 "source_file": plan["destination"]["folder"] + "/"
                                                 + plan["destination"]["path"]})
                destination["revision"] += 1
            else:
                return self.reply(409, {"detail": "Unsupported project action"})
            project["revision"] += 1
            return self.reply(200, PROJECTS)
        if method == "HEAD" and path.startswith("/v1/objects/"):
            data = OBJECTS.get(path.rsplit("/", 1)[1])
            return self.reply(200, data, "HEAD") if data is not None else self.reply(404, b"", "HEAD")
        if method == "GET" and path == "/v1/build-input-snapshots/latest":
            snapshot = SNAPSHOTS.get(parse_qs(url.query)["target"][0])
            return self.reply(200, snapshot) if snapshot else self.reply(404, {"detail": "Build input snapshot not found"})
        if method == "POST" and path == "/v1/build-input-snapshots":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if REFUSE_SNAPSHOTS:
                return self.reply(409, {"detail": "build input object is unavailable: x"})
            manifest = json.dumps({**body, "entries": sorted(body["entries"], key=lambda e: e["path"])},
                                  sort_keys=True, separators=(",", ":")) + "\n"
            SNAPSHOTS[body["target"]] = {**body, "sha256": hashlib.sha256(manifest.encode()).hexdigest(),
                                         "manifest_object_sha256": hashlib.sha256(manifest.encode()).hexdigest()}
            return self.reply(200, SNAPSHOTS[body["target"]])
        if method == "POST" and path == "/v1/library/url-imports":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            URL_IMPORTS.append(body)
            if "intranet" in body["url"]:
                return self.reply(422, {"accepted": False, "error_code": "address_refused",
                                        "detail": "The host intranet.example.org is a loopback, private or link-local address"})
            project = next((p for p in PROJECTS["projects"]
                            if body["project"] in (p["id"],) or body["project"].casefold() == p["name"].casefold()), None)
            if project is None:
                return self.reply(404, {"accepted": False, "error_code": "project_not_found",
                                        "detail": f"No Library project is named {body['project']!r}"})
            digest = hashlib.sha256(body["url"].split("?")[0].encode()).hexdigest()
            existing = next((d for d in project["documents"] if d.get("sha256") == digest), None)
            document = existing or {"document_id": "11111111-2222-4333-8444-" + digest[:12], "name": body["name"],
                                    "filename": f"Paper_{digest[:12]}_p.pdf", "imported": True, "sha256": digest,
                                    "page_count": 3, "included": True}
            if existing is None:
                project["documents"].append(document)
                project["revision"] = project.get("revision", 0) + 1
            return self.reply(200, {"accepted": True, "replayed": existing is not None, "project_id": project["id"],
                                    "project": project["name"], "document_id": document["document_id"],
                                    "filename": document["filename"], "name": document["name"], "sha256": digest,
                                    "size_bytes": 1234, "page_count": 3, "source_url": body["url"]})
        if method == "POST" and path == "/v1/review-card-merges":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            MERGES.append(body)
            if body["request_id"] in MERGE_RECEIPTS:
                first, answer = MERGE_RECEIPTS[body["request_id"]]
                if first != body:
                    return self.reply(409, {"accepted": False, "error_code": "request_conflict",
                                            "detail": "This request ID was already used with different arguments."})
                return self.reply(200, {**answer, "replayed": True})
            if "source" not in body:  # cards of an imported PDF
                kept = body.get("keep_card_id", body["card_ids"][0])
                answer = {"schema_version": 1, "accepted": True, "terminal": True, "state": "merged", "kind": "pdf",
                          "request_id": body["request_id"], "kept_card_id": kept,
                          "deleted_card_ids": [c for c in body["card_ids"] if c != kept],
                          "delete_event_ids": ["e-" + c[:8] for c in body["card_ids"] if c != kept],
                          "card_order": body["card_ids"], "question": body.get("question", "Joined?"),
                          "source": None, "source_sha256": None, "passages": 2 * len(body["card_ids"]),
                          "card_revision": 2, "document": "scan.pdf", "replayed": False, "message": "Merged."}
                MERGE_RECEIPTS[body["request_id"]] = (body, answer)
                return self.reply(200, answer)
            current = next(s for s in SOURCES if s["path"] == body["source"])["sha256"]
            if body["expected_source_sha256"] != current:
                return self.reply(409, {"accepted": False, "error_code": "source_conflict", "current_source_sha256": current,
                                        "detail": "The source changed; read it again and retry with its current SHA-256."})
            if UNPUBLISHED in body["card_ids"]:
                return self.reply(409, {"accepted": False, "error_code": "merge_refused",
                                        "detail": "Only cards of one document can be merged. The selected cards belong to "
                                                  "2 documents: FindingsPaper.pdf, hidden.pdf."})
            kept = body.get("keep_card_id", body["card_ids"][0])
            answer = {"schema_version": 1, "accepted": True, "terminal": True, "state": "merged",
                      "request_id": body["request_id"], "kept_card_id": kept,
                      "deleted_card_ids": [c for c in body["card_ids"] if c != kept],
                      "delete_event_ids": ["e-" + c[:8] for c in body["card_ids"] if c != kept],
                      "card_order": body["card_ids"], "question": body.get("question", "Joined?"),
                      "source": body["source"], "source_sha256": "9" * 64, "passages": len(body["card_ids"]),
                      "card_revision": 3, "document": "FindingsPaper.pdf", "replayed": False, "message": "Merged."}
            MERGE_RECEIPTS[body["request_id"]] = (body, answer)
            return self.reply(200, answer)
        if path.startswith("/v1/review-card-pending-moves"):
            return self.pending_moves(method, path, url)
        if method == "GET" and path == "/v1/health":
            return self.reply(200, {"status": "ok", "version": "test", "capabilities": CAPABILITIES,
                                    "storage": {"total_bytes": 1, "used_bytes": 0, "available_bytes": 1}})
        if method == "POST" and path == "/v1/operations":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            OPERATIONS.extend(body["operations"])
            return self.reply(200, {"latest_server_sequence": len(OPERATIONS), "results": [
                {"operation_id": op["operation_id"], "server_sequence": len(OPERATIONS), "outcome": "applied",
                 "entity_revision": op["payload"].get("revision", 1)} for op in body["operations"]]})
        if method == "GET" and path == "/v1/html-pages":
            folder = parse_qs(url.query).get("folder", [None])[0]
            listed = [page for page in HTML_PAGES.values() if folder is None or page["folder"] == folder]
            return self.reply(200, {"pages": listed, **({"unlisted_pages": []} if folder else {})})
        if method == "POST" and path == "/v1/html-pages":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            HTML_CHANGES.append(body)
            changed = []
            for change in body["changes"]:
                current = HTML_PAGES.get(change["path"], {}).get("sha256")
                if current == change["sha256"]:
                    continue
                if current != change["base_sha256"]:
                    return self.reply(409, {"detail": change["path"] + " changed on another device; read it again",
                                            "error_code": "html_page_changed", "path": change["path"],
                                            "current_sha256": current})
                HTML_PAGES[change["path"]] = {"path": change["path"], "folder": body["folder"], "sha256": change["sha256"],
                                              "size_bytes": len(OBJECTS[change["sha256"]])}
                changed.append(change["path"])
            return self.reply(200, {"folder": body["folder"], "changed": changed,
                                    "pages": [p for p in HTML_PAGES.values() if p["folder"] == body["folder"]]})
        if method == "GET" and path == "/v1/render-status":
            target = parse_qs(url.query)["target"][0]
            return self.reply(200, RENDER_STATUS.get(target) or {
                "status": "synced", "reason": "The PDF matches.", "source_frontier": 1, "build": None, "publication": None})
        if method == "GET" and path.startswith("/v1/builds/") and path.endswith("/source-diff"):
            return self.reply(422, {"detail": "The source of the last successful PDF before this build is unavailable."})
        if method == "POST" and path == "/v1/render-checks":
            return self.reply(200, {"status": "building", "library_revision": 2, "build": {"id": "b1"}, "documents": []})
        if method == "POST" and path.startswith("/v1/agent-notes/handling/") and path.endswith("/claim"):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            CLAIMS.append((path, body))
            outcome = CLAIM_OUTCOMES.get(path.split("/")[4], "yours")
            if outcome == "yours":
                return self.reply(201, {"claimed": True, "claim": {"device_id": "connector", "role": "sender",
                                                                  "conversation_id": body.get("conversation_id")}})
            if outcome == "responder":
                return self.reply(409, {"claimed": False, "error_code": "agent_note_claimed", "detail": "taken",
                                        "claim": {"device_id": "server", "conversation_id": "s", "role": "responder"}})
            return self.reply(409, {"claimed": False, "error_code": "agent_note_not_covering", "detail": "no",
                                    "claim": None})
        if method == "GET" and path == "/v1/mental-model":
            return self.reply(200, {"text": MENTAL_MODEL_TEXT, "sha256": "m", "revision": 3, "others": [],
                                    "source_path": ".thoughts/projects/p/default/mental-model.md",
                                    "updated_at": "2026-11-16T10:45:00Z"})
        if method == "GET" and path == "/v1/agent-notes/rules":
            return self.reply(200, {"text": RULES_TEXT, "sha256": "r", "source_path": "docs-x/agent-note-rules.md",
                                    "revision": 2, "updated_at": "2026-01-05T09:00:00Z", "others": []})
        if method == "POST" and path == "/v1/agent-notes/owners":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            OWNERS_POSTED.append(body)
            return self.reply(201, {"owner_id": "b" * 24, **body, "live": True})
        if method == "DELETE" and path.startswith("/v1/agent-notes/owners"):
            DELETES.append((path, {key: values[0] for key, values in parse_qs(url.query).items()}))
            return self.reply(204, b"")
        if method == "POST" and path == "/v1/agent-notes":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            POSTED.append(body)
            # A server with links keeps them in the note record (always a list); one without ignores the field.
            links = {"links": body.get("links") or []} if "agent_note_links_v1" in CAPABILITIES else {}
            return self.reply(201, {"note": {"note_id": body["note_id"], **links}, "delivery": {"live": True, "listeners": []},
                                    **SEND_ANSWER})
        if method == "POST" and path.startswith("/v1/conflicts/"):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            RESOLVED.append((path, body))
            CONFLICTS[:] = [c for c in CONFLICTS if c["id"] != path.split("/")[3]]
            return self.reply(200, {"outcome": "applied", "path": "docs-x/note.tex", "revision": 8, "sha256": body["content_sha256"]})
        routes = {"/v1/library/projects": PROJECTS, "/v1/sources": {"sources": SOURCES}, "/v1/projections": PROJECTIONS,
                  "/v1/conflicts": CONFLICTS,
                  "/v1/agent-notes": {"notes": [NOTE_RECORD, PHOTO_NOTE_RECORD]}, f"/v1/agent-notes/{NOTE}": NOTE_RECORD,
                  f"/v1/agent-notes/{PHOTO_NOTE}": PHOTO_NOTE_RECORD, "/v1/agent-notes/replies/r2/image": PNG,
                  f"/v1/agent-notes/{CLAIM_NOTE}": CLAIM_NOTE_RECORD, f"/v1/agent-notes/{CLAIM_PAGE}": CLAIM_PAGE_RECORD,
                  f"/v1/agent-notes/replies/{CLAIM_REPLY}/image": PNG,
                  "/v1/agent-notes/replies/r2/photo": JPEG,
                  "/v1/agent-notes/replies/r1/image": PNG, "/v1/agent-notes/replies": {"replies": [], "cursor": 0}}
        if path.startswith("/v1/objects/") and path.rsplit("/", 1)[1] in OBJECTS:
            return self.reply(200, OBJECTS[path.rsplit("/", 1)[1]])
        if path in routes:
            return self.reply(200, routes[path])
        return self.reply(404, {"detail": "not found"})

    def pending_moves(self, method, path, url):
        """The server's pending card moves (thoughts-server pending_card_moves.py), as far as the connector uses them."""
        parts = path.split("/")
        if method == "POST" and len(parts) == 3:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body["request_id"] in PENDING_MOVES:
                first, view = PENDING_MOVES[body["request_id"]]
                if first != body:
                    return self.reply(409, {"accepted": False, "error_code": "request_conflict",
                                            "detail": "This request ID was already used with different arguments."})
                return self.reply(200, {**view["first"], "replayed": True, "pending_move": view})
            proposal = next(s for s in SOURCES if s["path"] == body["source"])
            text = OBJECTS[proposal["object_sha256"]].decode()
            pid = re.search(r"id: (agent-[0-9a-f-]+)", text).group(1)
            answer = {"schema_version": 1, "accepted": True, "terminal": False, "state": "waiting_for_proposal",
                      "pending_move_id": body["request_id"], "request_id": body["request_id"], "card_id": body["card_id"],
                      "proposal_id": pid, "source": body["source"], "source_sha256": proposal["sha256"],
                      "document": "ideas.pdf", "document_id": "doc-ideas", "from_document": "Paper_p3.pdf",
                      "target_text": "Which two techniques?\n", "replayed": False, "message": "Nothing changed yet."}
            view = {key: answer[key] for key in ("pending_move_id", "request_id", "card_id", "proposal_id", "source",
                                                 "document", "document_id", "from_document", "target_text")}
            view.update(state="waiting_for_proposal", created_at="2026-11-12T10:00:00Z", settled_at=None, first=answer)
            PENDING_MOVES[body["request_id"]] = (body, view)
            return self.reply(200, answer)
        views = [{k: v for k, v in view.items() if k != "first"} for _body, view in PENDING_MOVES.values()]
        if method == "GET" and len(parts) == 3:
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            state = query.get("state", "waiting_for_proposal")
            return self.reply(200, {"pending_moves": [v for v in views if (state == "all" or v["state"] == state)
                                                      and query.get("card_id") in (None, v["card_id"])]})
        found = PENDING_MOVES.get(parts[3])
        if found is None:
            return self.reply(404, {"accepted": False, "error_code": "not_found", "detail": "No pending card move has this ID."})
        view = found[1]
        if method == "POST" and parts[4:] == ["cancel"]:
            settled = view["state"] != "waiting_for_proposal"
            if not settled:
                view.update(state="cancelled", reason="cancelled", settled_at="2026-11-12T11:00:00Z")
            return self.reply(200, {"accepted": True, "already_settled": settled,
                                    **{k: v for k, v in view.items() if k != "first"}})
        return self.reply(200, {k: v for k, v in view.items() if k != "first"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def do_DELETE(self):
        self.handle_any("DELETE")


@pytest.fixture(scope="module")
def server():
    (TMP / "token").write_text(TOKEN + "\n")
    stub = TMP / "agent_notes_stub.py"
    # The renderer's links: what MEASURED holds (written by measured_links), else none measured at all, as a
    # renderer from before links.
    stub.write_text("import base64, json\nfrom pathlib import Path\nfrom types import SimpleNamespace\nRENDERED = []\n"
                    "def render(*, markdown):\n    RENDERED.append(markdown)\n"
                    f"    measured = Path({str(MEASURED)!r})\n"
                    "    extra = json.loads(measured.read_text()) if measured.exists() else {}\n"
                    f"    return SimpleNamespace(png=base64.b64decode({base64.b64encode(PNG).decode()!r}), warnings=[], **extra)\n")
    thoughts_connector.AGENT_NOTES = stub
    fake = ThreadingHTTPServer(("127.0.0.1", THOUGHTS_PORT), FakeThoughts)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    connector_core.set_passphrase(TMP / "passphrase.json", PASSPHRASE)
    service = connector_core.Service(name="Thoughts", prefix="/connector", port=PORT, state=TMP / "state",
                                     what="your Thoughts Library", public=BASE, passphrase_file=TMP / "passphrase.json")
    app = thoughts_connector.create_app(service, f"http://127.0.0.1:{THOUGHTS_PORT}", TMP / "token")
    import uvicorn
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=runner.run, daemon=True).start()
    for _ in range(100):
        if runner.started:
            break
        time.sleep(0.05)
    yield app
    runner.should_exit = True
    fake.shutdown()


@pytest.fixture(scope="module")
def token(server):
    """Connect as ChatGPT would (registration, passphrase page, PKCE code, token)."""
    with httpx.Client(base_url=BASE, timeout=10) as http:
        client = http.post("/connector/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                                        "token_endpoint_auth_method": "none",
                                                        "grant_types": ["authorization_code", "refresh_token"],
                                                        "response_types": ["code"]}).json()
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        login = http.get("/connector/authorize", params={
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": f"{BASE}/connector/mcp"}).headers["location"]
        txn = parse_qs(urlparse(login).query)["txn"][0]
        assert "Thoughts Library" in http.get("/connector/login", params={"txn": txn}).text
        back = http.post("/connector/login", data={"txn": txn, "passphrase": PASSPHRASE}).headers["location"]
        code = parse_qs(urlparse(back).query)["code"][0]
        return http.post("/connector/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                                   "client_id": client["client_id"], "code_verifier": verifier}).json()["access_token"]


def rpc(token, method, params=None):
    response = httpx.post(f"{BASE}/connector/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call(token, tool, **arguments):
    result = rpc(token, "tools/call", {"name": tool, "arguments": arguments})
    assert not result.get("isError"), result
    return result


def test_discovery_is_its_own(server):
    with httpx.Client(base_url=BASE) as http:
        assert http.get("/.well-known/oauth-protected-resource/connector/mcp").json()["resource"] == f"{BASE}/connector/mcp"
        assert http.get("/.well-known/oauth-authorization-server/connector").json()["issuer"] == f"{BASE}/connector"


def test_tools_and_annotations(token):
    tools = {t["name"]: t for t in rpc(token, "tools/list")["tools"]}
    writers = {"send_note", "add_diary_entry", "create_proposals", "revise_proposal", "remove_proposal",
               "link_conversation", "create_document", "create_review_card", "move_review_card", "resolve_source_conflict",
               "add_html_page", "move_pdf", "import_pdf", "merge_review_cards", "cancel_pending_card_move",
               "own_topic", "release_topic", "create_project", "add_source_folder", "move_note"}
    assert set(tools) == {"list_documents", "read_document", "read_source", "search_library", "review_cards",
                          "get_review_card", "list_notes", "get_note", "document_feedback", "knowledge_profile", "source_conflicts",
                          "read_source_conflict", "html_pages", "build_status", "pending_card_moves", "note_rules",
                          "mental_model"} | writers
    assert all(not tools[name]["annotations"]["readOnlyHint"] for name in writers)
    assert all(t["annotations"]["readOnlyHint"] for name, t in tools.items() if name not in writers)
    edit = tools["create_proposals"]["inputSchema"]
    assert set(edit["required"]) == {"source", "expected_source_sha256", "proposals", "conversation_id", "request_id"}
    assert "figures" in json.dumps(edit) and "figures" in tools["revise_proposal"]["inputSchema"]["properties"]
    document = tools["create_document"]
    assert document["title"] == "Create a new note in a Project"
    assert set(document["inputSchema"]["required"]) == {"project", "name", "filename", "content", "conversation_id",
                                                        "request_id"}
    assert document["inputSchema"]["properties"]["content"]["maxLength"] == 4 * 1024 * 1024
    for phrase in ("only when the user asks", "never overwrites", "create_proposals", "own line"):
        assert phrase in document["description"], phrase
    # The rules thoughts-cli enforces are in every authoring tool's description.
    for name in ("create_proposals", "revise_proposal", "create_document"):
        for phrase in ("\\PdfLink{pdfKey}{anchor}{text}", "ANY Project", "never \\providecommand it",
                       "proposal_in_argument", "original_braces_balanced", "build_status"):
            assert phrase in tools[name]["description"], (name, phrase)
    assert "Only reads" in tools["build_status"]["description"]
    assert set(tools["move_pdf"]["inputSchema"]["required"]) == {"pdf", "project", "request_id"}
    assert set(tools["create_project"]["inputSchema"]["required"]) == {"name", "request_id"}
    assert set(tools["create_project"]["inputSchema"]["properties"]) == {"name", "request_id", "source_folder",
                                                                        "folder_name"}
    assert set(tools["add_source_folder"]["inputSchema"]["required"]) == {"project", "request_id"}
    move = tools["move_note"]
    assert set(move["inputSchema"]["required"]) == {"document", "project", "request_id"}
    for phrase in ("review cards and comments follow", "interactive pages", "needs_mac", "dry_run",
                   "\\SourceLink <-> \\CrossSourceLink"):
        assert phrase in move["description"], phrase
    imports = tools["import_pdf"]
    assert set(imports["inputSchema"]["required"]) == {"project", "name", "url"}
    assert set(imports["inputSchema"]["properties"]) == {"project", "name", "url"}
    assert imports["annotations"]["openWorldHint"] and imports["annotations"]["idempotentHint"]
    for phrase in ("exactly as Import document", "replayed: true", "document_id", "filename", "\\PdfLink"):
        assert phrase in imports["description"], phrase
    merge = tools["merge_review_cards"]
    schema = merge["inputSchema"]
    assert set(schema["required"]) == {"card_ids", "request_id"}
    assert set(schema["properties"]) == {"card_ids", "source", "expected_source_sha256", "request_id", "keep_card_id",
                                         "question"}
    assert (schema["properties"]["card_ids"]["minItems"], schema["properties"]["card_ids"]["maxItems"]) == (2, 20)
    assert merge["annotations"]["destructiveHint"] and merge["annotations"]["idempotentHint"]
    for phrase in ("as Merge cards… in the Thoughts app does", "keeps its ID, schedule and review history",
                   "Use it only when the user asked", "nothing for the user to accept piece by piece",
                   "Cards of an imported PDF", "leave out source"):
        assert phrase in merge["description"], phrase


def test_documents_and_reading(token):
    docs = call(token, "list_documents")["structuredContent"]["documents"]
    assert [(d["project"], d["name"], d["kind"], d["published"]) for d in docs] == [
        ("ideas", "Ideas", "md", True), ("Research", "FindingsPaper", "tex", True), ("Research", "Hidden", "pdf", False)]
    # The Library filename of each PDF, which links to it name.
    assert [d["filename"] for d in docs] == ["ideas.pdf", None, "hidden.pdf"]
    text = call(token, "read_document", document_id="doc-rp")["structuredContent"]["text"]
    assert "Accuracy falls as noise grows." in text and "% ---- tex/fragments/results ----" in text  # \input inlined
    assert "ReviewCardMask" not in text  # markers removed
    whole = call(token, "read_document", document_id="doc-rp")["structuredContent"]
    assert whole["next_line"] is None and whole["words"] > 10  # the whole document by default
    first = call(token, "read_document", document_id="doc-rp", max_words=200)["structuredContent"]
    assert first["next_line"] is None  # 200 words is the minimum; this document is shorter
    by_name = call(token, "read_document", document_id="findingspaper")["structuredContent"]
    assert by_name["document_id"] == "doc-rp"
    found = call(token, "search_library", query="BANANAS")["structuredContent"]["results"]
    assert [(r["name"], r["match"]) for r in found] == [("FindingsPaper", "text")] and "bananas" in found[0]["passage"]
    assert call(token, "search_library", query="ideas")["structuredContent"]["results"][0]["match"] == "name"


def test_document_feedback(token):
    listed = call(token, "document_feedback", document_id="findingspaper")
    data = listed["structuredContent"]
    assert (data["document_id"], data["current_pdf_sha256"], data["liked"], data["disliked"], data["total"]) == (
        "doc-rp", "a" * 64, 1, 1, 2)
    # Oldest first (08:30Z before 08:52Z); the deleted record is left out.
    assert data["feedback"] == [
        {"feedback_id": DISLIKED, "polarity": "negative", "quote": "passage 2", "comment": "Wrong sign.", "page": 3,
         "pdf_sha256": "a" * 64, "snapshot": "current", "created_at": "2026-10-28T10:30:00.000+02:00",
         "updated_at": "2026-10-28T10:30:00.000+02:00", "origin": "android-example-phone"},
        {"feedback_id": LIKED, "polarity": "positive", "quote": "passage 1", "comment": "", "page": 1,
         "pdf_sha256": "f" * 64, "snapshot": "earlier", "created_at": "2026-10-28T08:52:03.512Z",
         "updated_at": "2026-10-28T08:52:03.512Z", "origin": "android-example-phone"}]
    text = listed["content"][0]["text"]
    assert "FindingsPaper: 1 liked, 1 disliked" in text
    assert "Disliked, p. 3 (current PDF), 2026-10-28: \u201cpassage 2\u201d \u2014 Wrong sign." in text
    empty = call(token, "document_feedback", document_id="doc-ideas")["structuredContent"]
    assert (empty["total"], empty["feedback"]) == (0, [])
    # A document no longer in the catalogue: found by its id, its current PDF is unknown.
    gone = call(token, "document_feedback", document_id=GONE_DOC)["structuredContent"]
    assert gone["name"] is None and [(f["feedback_id"], f["snapshot"]) for f in gone["feedback"]] == [(OTHER, None)]
    assert rpc(token, "tools/call", {"name": "document_feedback", "arguments": {"document_id": "nope"}})["isError"]
    # read_document tells how much feedback there is.
    read = call(token, "read_document", document_id="doc-rp")
    assert read["structuredContent"]["feedback"] == {"liked": 1, "disliked": 1}
    assert "1 liked and 1 disliked passage(s) here (document_feedback)" in read["content"][0]["text"]
    plain = call(token, "read_document", document_id="doc-ideas")
    assert plain["structuredContent"]["feedback"] == {"liked": 0, "disliked": 0}
    assert "document_feedback" not in plain["content"][0]["text"]


def test_review_cards(token):
    cards = call(token, "review_cards", state="active")["structuredContent"]["cards"]
    assert [c["card_id"] for c in cards] == [CARD]  # the deleted card is left out
    assert cards[0]["status"] == "active" and "retired_at" not in cards[0]  # its retirement was undone
    assert cards[0]["reviews"] == 2 and cards[0]["last_tags"] == ["I get it", "Know by heart"]
    assert cards[0]["document"] == "FindingsPaper"
    detail = call(token, "get_review_card", card_id=CARD)["structuredContent"]
    assert detail["answer"] == "Accuracy falls as noise grows."
    assert detail["history"][0]["review_group_notes"] == {"note": "mixed up the axes"}
    assert call(token, "review_cards", query="nothing like this")["structuredContent"]["total"] == 0


def test_questions_and_other_comments_are_named():
    asked = {"feedback_id": "q", "polarity": "neutral", "kind": "question", "comment": "Why?", "created_at": "2026-11-04T10:00:00.000Z",
             "anchor": {"quote": {"exact": "the bound"}, "pdf": {"pdf_sha256": "a" * 64, "regions": [{"page": 2}]}}}
    closed = {**asked, "kind": "other", "comment": "Fix it.", "resolved_at": "2026-11-04T11:00:00.000Z"}
    summary = thoughts_connector.feedback_summary(asked, "a" * 64)
    assert summary["kind"] == "question" and summary["resolved_at"] is None
    assert thoughts_connector.feedback_line(summary).startswith("- Question, p. 2 (current PDF)")
    assert thoughts_connector.feedback_line(thoughts_connector.feedback_summary(closed, "a" * 64)).startswith("- Comment (resolved), p. 2")
    assert thoughts_connector.feedback_counts([asked, closed]) == {"liked": 0, "disliked": 0, "open_comments": 1}


def test_retired_review_cards(token):
    every = call(token, "review_cards")["structuredContent"]["cards"]
    assert [c["card_id"] for c in every] == [CARD, RETIRED, UNPUBLISHED]  # newest first; deleted left out
    retired = call(token, "review_cards", state="retired")["structuredContent"]
    assert [c["card_id"] for c in retired["cards"]] == [RETIRED]
    card = retired["cards"][0]
    assert (card["status"], card["retired_at"], card["reviews"]) == ("retired", "2026-10-27T11:00:00Z", 1)
    detail = call(token, "get_review_card", card_id=RETIRED)
    assert detail["structuredContent"]["status"] == "retired"
    assert [h.get("action") or h.get("rating") for h in detail["structuredContent"]["history"]] == [
        "good", "retire", "restore", "retire"]
    assert "(retired 2026-10-27)" in detail["content"][0]["text"]
    assert rpc(token, "tools/call", {"name": "review_cards", "arguments": {"state": "gone"}})["isError"]


def test_notes(token):
    sent = call(token, "send_note", title="Bayes", markdown="# Bayes\n$p(x)$", review_card_id=CARD)
    assert sent["structuredContent"]["phone_listening"] is True
    body = POSTED[-1]
    assert body["title"] == "Bayes" and body["review_card_id"] == CARD and body["origin"] == {"agent": "connector"}
    assert base64.b64decode(body["image"]["data_base64"]) == PNG
    listed = call(token, "list_notes")["structuredContent"]["notes"]
    assert listed[0]["note_id"] == NOTE and listed[0]["replies"] == 1
    note = call(token, "get_note", note_id=NOTE)
    images = [c for c in note["content"] if c["type"] == "image"]
    assert len(images) == 1 and base64.b64decode(images[0]["data"]) == PNG  # the user's drawing, visible to the model
    assert note["structuredContent"]["replies"][0]["message"] == "like this?"
    assert note["structuredContent"]["replies"][0]["has_photo"] is False
    # A reply that does not say what the answer may use allows everything, as before.
    assert note["structuredContent"]["replies"][0]["sources"] == "library_and_mental_model"
    assert "Sources the user allows" not in note["content"][0]["text"]


def test_a_reply_photo_reaches_the_agent_and_the_watcher_sees_its_capture(token):
    listed = call(token, "list_notes")
    rows = {row["note_id"]: row for row in listed["structuredContent"]["notes"]}
    assert rows[PHOTO_NOTE]["photo_captures"] == ["IMG_20261111_170138_123_ab12"]
    assert rows[NOTE]["photo_captures"] == []
    assert "with photo IMG_20261111_170138_123_ab12" in listed["content"][0]["text"]

    note = call(token, "get_note", note_id=PHOTO_NOTE)
    images = [c for c in note["content"] if c["type"] == "image"]
    # The annotated note, then the glasses photo: the words are about the photo.
    assert [c["mimeType"] for c in images] == ["image/png", "image/jpeg"]
    assert base64.b64decode(images[1]["data"]) == JPEG
    reply = note["structuredContent"]["replies"][0]
    assert (reply["has_photo"], reply["photo_capture"]) == (True, "IMG_20261111_170138_123_ab12")
    assert "with a photo from the glasses: IMG_20261111_170138_123_ab12" in note["content"][0]["text"]
    # The user narrowed what the answer may use: the agent is told, in the record and the text.
    assert reply["sources"] == "linked_pdf"
    assert "only the PDF this note is linked to" in reply["sources_rule"]
    assert "Sources the user allows: Use only the PDF this note is linked to" in note["content"][0]["text"]


def test_waiting_for_a_reply_tells_the_phone_an_agent_listens(token):
    QUERIES.clear()
    call(token, "get_note", note_id=NOTE)  # it has a reply already: no wait, no listening
    assert not [q for path, q in QUERIES if path == "/v1/agent-notes/replies"]
    replies = NOTE_RECORD["replies"]
    NOTE_RECORD["replies"] = []  # no reply yet: get_note waits, and listens while it does
    try:
        call(token, "get_note", note_id=NOTE, wait_for_reply_s=1)
    finally:
        NOTE_RECORD["replies"] = replies
    polls = [q for path, q in QUERIES if path == "/v1/agent-notes/replies"]
    assert polls == [{"note_id": NOTE, "wait": "1", "listen": "true"}]


def test_send_note_says_when_nobody_listens_for_the_reply(token):
    SEND_ANSWER.update(reply_listening={"state": "not_listening", "listeners": []})
    try:
        sent = call(token, "send_note", title="Grades", markdown="# Grades")
    finally:
        SEND_ANSWER.clear()
    assert sent["structuredContent"]["reply_listening"] == "not_listening"
    assert sent["structuredContent"]["reply_agent_listening"] is False
    assert "thoughts-watch-replies --conversation" in sent["content"][0]["text"]
    assert "the phone tells the user nobody listens" in sent["content"][0]["text"]
    plain = call(token, "send_note", title="Grades", markdown="# Grades")  # a server that cannot tell
    assert plain["structuredContent"]["reply_listening"] == "unknown"
    assert "thoughts-watch-replies" not in plain["content"][0]["text"]


def test_send_note_asks_for_the_watcher_even_when_the_reply_agent_listens(token):
    SEND_ANSWER.update(reply_listening={"state": "listening", "listeners": [
        {"device_id": "connector", "conversation_id": None, "note_id": None, "last_seen_at": "2026-01-05T10:00:00Z",
         "responder": True, "role": "responder"}]})
    try:
        sent = call(token, "send_note", title="Grades", markdown="# Grades", conversation_id="conversation-1")
        SEND_ANSWER.update(reply_listening={"state": "listening", "listeners": [
            {"device_id": "connector", "conversation_id": "conversation-1", "note_id": None,
             "last_seen_at": "2026-01-05T10:00:00Z", "responder": False, "role": "sender"}]})
        heard = call(token, "send_note", title="Grades", markdown="# Grades", conversation_id="conversation-1")
    finally:
        SEND_ANSWER.clear()
    assert sent["structuredContent"]["reply_agent_listening"] is True
    assert "meanwhile the Thoughts reply agent covers it" in sent["content"][0]["text"]
    assert POSTED[-2]["origin"] == {"agent": "connector", "conversation_id": "conversation-1"}
    assert "thoughts-watch-replies" not in heard["content"][0]["text"]


def test_get_note_claims_for_the_conversation_and_hands_over_the_rules(token):
    CLAIMS.clear()
    plain = call(token, "get_note", note_id=CLAIM_NOTE)  # no conversation: nothing is claimed
    assert CLAIMS == [] and plain["structuredContent"]["replies"][0]["handling"] is None
    got = call(token, "get_note", note_id=CLAIM_NOTE, conversation_id="conversation-1")
    assert CLAIMS == [(f"/v1/agent-notes/handling/{CLAIM_REPLY}/claim",
                       {"conversation_id": "conversation-1", "only_if_covering": True})]
    text = got["content"][0]["text"]
    assert "yours to answer" in text and "Answer in plain words." in text
    assert got["structuredContent"]["replies"][0]["handling"]["state"] == "yours"

    CLAIM_OUTCOMES[CLAIM_REPLY] = "responder"
    try:
        taken = call(token, "get_note", note_id=CLAIM_NOTE, conversation_id="conversation-1")
    finally:
        CLAIM_OUTCOMES.clear()
    assert "the Thoughts reply agent answers it; do not answer it again" in taken["content"][0]["text"]
    assert "Answer in plain words." not in taken["content"][0]["text"]

    CLAIM_OUTCOMES[CLAIM_PAGE] = "not_covering"
    try:
        page = call(token, "get_note", note_id=CLAIM_PAGE, conversation_id="conversation-1")
    finally:
        CLAIM_OUTCOMES.clear()
    assert CLAIMS[-1][0] == f"/v1/agent-notes/handling/{CLAIM_PAGE}/claim"
    assert page["structuredContent"]["page_handling"]["state"] == "not_yours"
    assert "This page: not yours" in page["content"][0]["text"]


def test_the_mental_model_is_the_library_note(token):
    page = call(token, "mental_model")
    assert page["content"][0]["text"] == MENTAL_MODEL_TEXT
    assert page["structuredContent"]["source_path"] == ".thoughts/projects/p/default/mental-model.md"
    assert page["structuredContent"]["revision"] == 3


def test_note_rules_and_topic_owners(token):
    rules = call(token, "note_rules")
    assert rules["content"][0]["text"] == RULES_TEXT
    assert rules["structuredContent"]["source_path"] == "docs-x/agent-note-rules.md"
    OWNERS_POSTED.clear()
    owned = call(token, "own_topic", conversation_id="conversation-1", label="Linear algebra",
                 title_tags=["[Linear algebra]"], captures=True)
    assert OWNERS_POSTED == [{"conversation_id": "conversation-1", "label": "Linear algebra", "document_ids": [],
                              "review_card_ids": [], "title_tags": ["[Linear algebra]"], "captures": True}]
    assert "thoughts-watch-replies --conversation conversation-1" in owned["content"][0]["text"]
    DELETES.clear()
    call(token, "release_topic", owner_id="b" * 24)
    call(token, "release_topic", conversation_id="conversation-1")
    assert DELETES == [("/v1/agent-notes/owners/" + "b" * 24, {}),
                       ("/v1/agent-notes/owners", {"conversation_id": "conversation-1"})]


class measured_links:
    """While in use, the stub renderer measures these links ([href, [x, y, width, height] in CSS px, text])."""

    def __init__(self, links, css_height=400):
        self.value = {"links": links, "css_height": css_height}

    def __enter__(self):
        MEASURED.write_text(json.dumps(self.value))

    def __exit__(self, *exc):
        MEASURED.unlink(missing_ok=True)


def test_note_links_open_web_pages_and_library_pdfs(token):
    links = [["https://example.org/paper", [120, 40, 72, 20], "the paper"],
             # A link wrapped over two lines, to a Library PDF behind its folder: page 53 as printed is page 52.
             ["../../docs-ideas/ideas.pdf#page=53", [432, 80, 48, 20], "a long link"],
             ["../../docs-ideas/ideas.pdf#page=53", [24, 100, 96, 20], "a long link"],
             ["#page=2", [192, 200, 48, 48], ""],                  # an <a> in an SVG, a page of the note's own document
             ["HIDDEN.PDF#page=1", [24, 260, 48, 20], "hidden"],   # the Library's own spelling, unpublished or not
             ["Missing_example.pdf#page=3", [24, 300, 48, 20], "p. 3"],
             ["mailto:alex@example.org", [24, 320, 48, 20], "write"],
             ["https://example.org/below", [24, 390, 48, 20], "below"],   # half cut off by the page's height
             ["https://example.org/gone", [24, 420, 48, 20], "gone"]]     # entirely below it
    with measured_links(links):
        sent = call(token, "send_note", title="Sources", markdown="# Sources", document_id="doc-ideas")
    assert POSTED[-1]["links"] == [
        {"rect": [0.25, 0.1, 0.15, 0.05], "href": "https://example.org/paper", "label": "the paper"},
        {"rect": [0.9, 0.2, 0.1, 0.05], "href": "thoughts-pdf-link:ideas.pdf?page=52", "label": "a long link"},
        {"rect": [0.05, 0.25, 0.2, 0.05], "href": "thoughts-pdf-link:ideas.pdf?page=52", "label": "a long link"},
        {"rect": [0.4, 0.5, 0.1, 0.12], "href": "thoughts-pdf-link:ideas.pdf?page=1"},
        {"rect": [0.05, 0.65, 0.1, 0.05], "href": "thoughts-pdf-link:hidden.pdf?page=0", "label": "hidden"},
        {"rect": [0.05, 0.75, 0.1, 0.05], "href": "thoughts-pdf-link:Missing_example.pdf?page=2", "label": "p. 3"},
        {"rect": [0.05, 0.975, 0.1, 0.025], "href": "https://example.org/below", "label": "below"}]
    body = sent["structuredContent"]
    assert [link["href"] for link in body["links"]] == [link["href"] for link in POSTED[-1]["links"]]
    assert len(body["warnings"]) == 2  # one per doubtful or dropped link, not per rectangle
    assert "no Library PDF is named Missing_example.pdf" in body["warnings"][0]
    assert "mailto:alex@example.org is neither a web address" in body["warnings"][1]
    assert "with 7 tappable links" in sent["content"][0]["text"]


def test_note_links_wait_for_the_server_capability(server, token):
    CAPABILITIES.remove("agent_note_links_v1")
    server.thoughts.cached.pop("health", None)
    try:
        with measured_links([["https://example.org/", [24, 40, 48, 20], "a page"]]):
            sent = call(token, "send_note", title="Older server", markdown="[a page](https://example.org/)")
    finally:
        CAPABILITIES.append("agent_note_links_v1")
        server.thoughts.cached.pop("health", None)
    assert "links" not in POSTED[-1]
    body = sent["structuredContent"]
    assert body["links"] == [] and "does not support links yet (agent_note_links_v1)" in body["warnings"][0]
    assert "tappable" not in sent["content"][0]["text"]


def test_a_note_without_links_asks_for_nothing_more(token):
    """No links measured (or a renderer from before links): neither the catalogue nor the capabilities are read."""
    before = len(REQUESTS)
    with measured_links([]):
        call(token, "send_note", title="Plain", markdown="# Plain")
    sent = call(token, "send_note", title="Plain", markdown="# Plain")
    assert "links" not in POSTED[-1] and sent["structuredContent"]["links"] == []
    assert [path for _method, path, _auth in REQUESTS[before:]] == ["/v1/agent-notes", "/v1/agent-notes"]


def test_note_link_targets_are_what_the_server_takes():
    library = {"primer_example.pdf": "Primer_example.pdf"}
    target = lambda href, own=None, known=library: thoughts_connector.note_link_target(href, own, known)
    assert target("https://example.org/a?b=1#c") == ("https://example.org/a?b=1#c", None)
    assert target("https://example.org/" + "x" * 2100)[0] is None
    assert target("primer_EXAMPLE.pdf") == ("thoughts-pdf-link:Primer_example.pdf?page=0", None)
    assert target("Primer_example.pdf#page=0") == ("thoughts-pdf-link:Primer_example.pdf?page=0", None)
    assert target("thoughts-pdf-link:Primer_example.pdf?page=4&x=10.5&y=-3") == (
        "thoughts-pdf-link:Primer_example.pdf?page=4&x=10.5&y=-3", None)
    assert target("thoughts-pdf-link:Primer_example.pdf?page=x")[0] is None
    assert target("#page=7", own="Own_example.pdf", known=None) == ("thoughts-pdf-link:Own_example.pdf?page=6", None)
    assert "give document_id" in target("#page=7")[1] and target("#page=7")[0] is None
    # Not in the Library: kept for a page the phone may have, unless the server would refuse the whole note for it.
    assert target("Other_example.pdf#page=2")[0] == "thoughts-pdf-link:Other_example.pdf?page=1"
    assert target("Other_example.PDF#page=2")[0] is None
    assert target("Other_example.pdf#page=2", known=None) == ("thoughts-pdf-link:Other_example.pdf?page=1", None)
    for dropped in ("javascript:alert(1)", "file:///etc/hosts", "notes.md#intro", "", "ftp://example.org/x.pdf"):
        assert target(dropped)[0] is None, dropped
    many = [[f"https://example.org/{n}", [0, n, 10, 1], ""] for n in range(70)]
    links, warnings = thoughts_connector.note_links(many, 100)
    assert len(links) == 64 and "only the first 64" in warnings[0]
    assert links[0] == {"rect": [0.0, 0.0, 0.020833, 0.01], "href": "https://example.org/0"}


@pytest.mark.skipif(not HAVE_CLI, reason="thoughts-cli not found (set THOUGHTS_CLI)")
def test_read_source_and_create_proposals(token):
    source = call(token, "read_source", source="docs-rp/tex/fragments/results.tex")["structuredContent"]
    assert source["source_sha256"] == RESULTS[0] and source["lines"] == 5
    assert source["text"].startswith("\\section{Results}\n\\ReviewCardMaskStart")  # exact: markers kept
    main = call(token, "read_source", source="doc-rp")
    assert main["structuredContent"]["source"] == "docs-rp/tex/source/main.tex"
    assert main["structuredContent"]["includes"] == ["docs-rp/tex/fragments/results.tex"]
    assert "    3| \\input{tex/fragments/results}" in main["content"][0]["text"]
    conversation, request_id = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566", "1b6c5b7e-1d2a-4c3b-9f8e-112233445566"
    edit = {"start_line": 5, "end_line": 6, "replacement": "More text about apples. And pears.\n",
            "explanation": "Bananas were wrong."}
    created = call(token, "create_proposals", source=source["source"], expected_source_sha256=source["source_sha256"],
                   proposals=[edit], conversation_id=conversation, request_id=request_id)
    # thoughts-cli published the source with the desktop's wrapper, as this device.
    (publication,) = PUBLISHED
    (mutation,) = publication["mutations"]
    assert mutation["path"] == "docs-rp/tex/fragments/results.tex" and mutation["base_sha256"] == RESULTS[0]
    written = OBJECTS[mutation["content_sha256"]].decode()
    proposal = created["structuredContent"]["proposals"][0]
    assert written.endswith(f"\\agentProposedEdit[created={proposal['created_at']},conversation_id={conversation}]"
                            f"{{{proposal['proposal_id']}}}{{Bananas were wrong.\n}}{{More text about bananas.\n}}"
                            "{More text about apples. And pears.\n}")
    assert proposal["proposal_id"].startswith("agent-") and created["structuredContent"]["device_id"] == "connector"
    text = created["content"][0]["text"]
    assert f"{proposal['proposal_id']} at line 5" in text and "Warning:" in text  # two sentences on one line
    replay = call(token, "create_proposals", source=source["source"], expected_source_sha256=source["source_sha256"],
                  proposals=[edit], conversation_id=conversation, request_id=request_id)
    assert "replayed" in replay["content"][0]["text"] and len(PUBLISHED) == 1
    stale = rpc(token, "tools/call", {"name": "create_proposals", "arguments": {
        "source": source["source"], "expected_source_sha256": RESULTS[0], "proposals": [edit],
        "conversation_id": conversation, "request_id": "2b6c5b7e-1d2a-4c3b-9f8e-112233445566"}})
    assert stale["isError"] and stale["structuredContent"]["error_code"] == "source_conflict"
    assert mutation["content_sha256"] in stale["content"][0]["text"]
    invalid = rpc(token, "tools/call", {"name": "create_proposals", "arguments": {
        "source": source["source"], "expected_source_sha256": mutation["content_sha256"], "proposals": [{**edit, "extra": 1}],
        "conversation_id": conversation, "request_id": "3b6c5b7e-1d2a-4c3b-9f8e-112233445566"}})
    assert invalid["isError"] and len(PUBLISHED) == 1  # refused by the connector, never sent
    revised = call(token, "revise_proposal", source=source["source"], expected_source_sha256=mutation["content_sha256"],
                   proposal_id=proposal["proposal_id"], replacement="More text about apples.\n", explanation="Apples only.",
                   conversation_id=conversation, request_id="4b6c5b7e-1d2a-4c3b-9f8e-112233445566")
    assert revised["structuredContent"]["state"] == "proposal_revised" and len(PUBLISHED) == 2
    assert "Revised 1 pending proposal(s)" in revised["content"][0]["text"]


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "figures.py").is_file()),
                    reason="thoughts-cli with figures not found")
def test_a_proposal_adds_a_figure(token):
    source = call(token, "read_source", source="docs-rp/tex/source/main.tex")["structuredContent"]
    PUBLISHED.clear()
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'
    edit = {"start_line": source["lines"] + 1, "end_line": source["lines"] + 1,
            "replacement": "\\includegraphics[width=\\linewidth]{agent-figures/apple}\n",
            "explanation": "A drawing of the apple.", "figures": [{"name": "apple", "svg": svg}]}
    created = call(token, "create_proposals", source=source["source"], expected_source_sha256=source["source_sha256"],
                   proposals=[edit], conversation_id="0b6c5b7e-1d2a-4c3b-9f8e-112233445566",
                   request_id="5b6c5b7e-1d2a-4c3b-9f8e-112233445566")
    (publication,) = PUBLISHED
    paths = [mutation["path"] for mutation in publication["mutations"]]
    assert paths == [source["source"], "docs-rp/tex/source/agent-figures/apple.svg"]
    figure = OBJECTS[publication["mutations"][1]["content_sha256"]].decode()
    proposal = created["structuredContent"]["proposals"][0]["proposal_id"]
    assert figure == f'<!-- thoughts-agent-figure proposal="{proposal}" -->\n{svg}\n'
    assert "figure docs-rp/tex/source/agent-figures/apple.svg" in created["content"][0]["text"]
    unreferenced = rpc(token, "tools/call", {"name": "create_proposals", "arguments": {
        "source": source["source"], "expected_source_sha256": source["source_sha256"],
        "proposals": [{**edit, "replacement": "No picture.\n"}], "conversation_id": "0b6c5b7e-1d2a-4c3b-9f8e-112233445566",
        "request_id": "6b6c5b7e-1d2a-4c3b-9f8e-112233445566"}})
    assert unreferenced["isError"] and unreferenced["structuredContent"]["error_code"] == "invalid_figure"


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "documents.py").is_file()),
                    reason="thoughts-cli with create-document not found")
def test_create_document(token):
    PUBLISHED.clear()
    LIBRARY_CHANGES.clear()
    conversation = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
    arguments = {"project": "Notes", "name": "Attention, briefly", "filename": "ml/attention.md",
                 "content": "# Attention\n\nIt weighs every token against every other one.\n",
                 "conversation_id": conversation, "request_id": "7b6c5b7e-1d2a-4c3b-9f8e-112233445566"}
    created = call(token, "create_document", **arguments)
    path = NOTES_FOLDER + "/ml/attention.md"
    body = created["structuredContent"]
    assert body["state"] == "document_created" and body["source"] == path and body["device_id"] == "connector"
    (publication,) = PUBLISHED
    (mutation,) = publication["mutations"]
    assert (mutation["path"], mutation["base_revision"], mutation["base_sha256"]) == (path, 0, EMPTY)
    assert OBJECTS[mutation["content_sha256"]].decode() == arguments["content"]
    include, rename = LIBRARY_CHANGES
    assert (include["action"], include["project_id"], include["base_revision"], include["source_file"]) == (
        "include_source", "p3", 4, path)
    assert (rename["action"], rename["document_id"], rename["name"], rename["base_revision"]) == (
        "rename_document", include["new_document_id"], "Attention, briefly", 5)
    text = created["content"][0]["text"]
    assert text.startswith(f'Created document "Attention, briefly" in Project "Notes" ({path}')
    assert "Thoughts builds its PDF now" in text
    # The new PDF's build inputs, as the Mac publishes them: the Project's imported paper.
    target = "document_" + hashlib.sha256(path.encode()).hexdigest()[:20] + ".pdf"
    snapshot = SNAPSHOTS[target]
    assert {key: snapshot[key] for key in ("schema_version", "target", "project", "discovery_policy", "entries")} == {
        "schema_version": 3, "target": target, "project": NOTES_FOLDER, "discovery_policy": "source-closure-v2",
        "entries": [{"path": NOTES_FOLDER + "/Paper_p3.pdf", "sha256": PAPER[0], "size_bytes": len(PAPER[1]),
                     "media_type": "application/pdf", "role": "external_pdf"}]}
    assert body["build_inputs"]["status"] == "published" and body["build_inputs"]["snapshot_sha256"] == snapshot["sha256"]
    # The new document is listed at once (the cached catalogue was dropped).
    listed = call(token, "list_documents", project="Notes")["structuredContent"]["documents"]
    assert [(d["name"], d["kind"]) for d in listed] == [("Attention, briefly", "md")]
    replay = call(token, "create_document", **arguments)
    assert "replayed" in replay["content"][0]["text"] and len(PUBLISHED) == 1 and len(LIBRARY_CHANGES) == 2
    # Never over an existing note; refusals are errors with the reason.
    for change, code in (({"request_id": "8b6c5b7e-1d2a-4c3b-9f8e-112233445566"}, "source_exists"),
                         ({"request_id": "9b6c5b7e-1d2a-4c3b-9f8e-112233445566", "project": "Nope"}, "unknown_project"),
                         ({"request_id": "ab6c5b7e-1d2a-4c3b-9f8e-112233445566", "filename": "../x.md"}, "invalid_request"),
                         ({"request_id": "bb6c5b7e-1d2a-4c3b-9f8e-112233445566", "filename": "x.tex"}, "invalid_request")):
        refused = rpc(token, "tools/call", {"name": "create_document", "arguments": {**arguments, **change}})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == code, (change, refused)
        assert refused["content"][0]["text"].startswith(f"The note was not created ({code})")
    assert len(PUBLISHED) == 1 and len(LIBRARY_CHANGES) == 2


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "build_inputs.py").is_file()),
                    reason="thoughts-cli with build inputs not found")
def test_a_note_without_build_inputs_is_created_unpublished(token):
    """A published note without build inputs would stop every build of the Library."""
    LIBRARY_CHANGES.clear()
    REFUSE_SNAPSHOTS.append(True)
    try:
        created = call(token, "create_document", project="Notes", name="Refused", filename="refused.md",
                       content="# Refused\n\nIts inputs are refused.\n",
                       conversation_id="0b6c5b7e-1d2a-4c3b-9f8e-112233445566",
                       request_id="cb6c5b7e-1d2a-4c3b-9f8e-112233445566")
    finally:
        REFUSE_SNAPSHOTS.clear()
    body = created["structuredContent"]
    assert body["published"] is False and body["build_inputs"]["status"] == "refused"
    include, hide, rename = LIBRARY_CHANGES
    assert (include["action"], hide["action"], hide["published"], hide["document_id"], rename["action"]) == (
        "include_source", "set_document_published", False, body["document_id"], "rename_document")
    text = created["content"][0]["text"]
    assert "unpublished for now" in text and "document_unpublished" not in text and "Warning:" in text
    project = next(p for p in PROJECTS["projects"] if p["id"] == "p3")
    assert body["document_id"] in [d["document_id"] for d in project["unpublished_documents"]]


def test_only_the_new_note_actions_change_a_project(server):
    import asyncio
    thoughts_connector.check_request("POST", "/v1/library/projects", {"action": "include_source"})
    thoughts_connector.check_request("POST", "/v1/library/projects", {"action": "rename_document"})
    thoughts_connector.check_request("GET", "/v1/library/projects")
    thoughts_connector.check_request("POST", "/v1/library/projects", {"action": "move_document"})
    for action in ("create", "add_source_folder", "plan_move_source_document", "move_source_document"):
        thoughts_connector.check_request("POST", "/v1/library/projects", {"action": action})
    for method, route in (("GET", "/v1/render-status"), ("GET", f"/v1/builds/{CARD}"),
                          ("GET", f"/v1/builds/{CARD}/source-diff")):
        thoughts_connector.check_request(method, route)
    for method, route in (("POST", f"/v1/builds/{CARD}/cancel"), ("GET", "/v1/builds/x/output"),
                          ("POST", "/v1/render-status")):
        with pytest.raises(thoughts_connector.ThoughtsError, match="not allowed"):
            thoughts_connector.check_request(method, route)
    for body in ({"action": "remove_document"}, {"action": "remove"}, {"action": "set_document_published"}, None, []):
        with pytest.raises(thoughts_connector.ThoughtsError, match="not allowed"):
            thoughts_connector.check_request("POST", "/v1/library/projects", body)
    # Only unpublishing a note create_document added.
    unpublish = {"action": "set_document_published", "document_id": "d1", "published": False}
    thoughts_connector.check_request("POST", "/v1/library/projects", unpublish, {"d1"})
    for body, created in ((unpublish, {"d2"}), ({**unpublish, "published": True}, {"d1"})):
        with pytest.raises(thoughts_connector.ThoughtsError, match="not allowed"):
            thoughts_connector.check_request("POST", "/v1/library/projects", body, created)
    before = len(REQUESTS)
    with pytest.raises(thoughts_connector.ThoughtsError, match="not allowed"):
        asyncio.run(server.thoughts.request("POST", "/v1/library/projects", body={"action": "remove", "project_id": "p3"}))
    assert len(REQUESTS) == before  # refused before anything was sent


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "review_cards.py").is_file()),
                    reason="thoughts-cli with review cards not found")
def test_create_review_card_marks_the_passage_in_one_operation(token):
    source = call(token, "read_source", source="docs-ideas/markdown/source/ideas.md")["structuredContent"]
    request_id = "c5b6c5b7-1d2a-4c3b-9f8e-112233445566"
    arguments = dict(source=source["source"], expected_source_sha256=source["source_sha256"], start_line=3, end_line=4,
                     question="What is the note about?", due_at="2099-01-01T09:00:00+01:00", request_id=request_id)
    created = call(token, "create_review_card", **arguments)
    (operation,) = OPERATIONS
    payload = operation["payload"]
    assert (operation["entity_type"], operation["entity_id"], operation["base_revision"]) == ("review_card", request_id, 0)
    assert (payload["primary"]["pdf_stem"], payload["primary"]["document_id"]) == ("ideas", "doc-ideas")
    assert payload["status"] == "approved" and payload["source_authority"] == "client-v1"
    change = payload["source_change"]
    assert change["before_sha256"] == NOTES_MD[0] and change["path"] == source["source"]
    key = f"review:{request_id}:answer:0"
    assert OBJECTS[change["after_sha256"]].decode() == (
        f"# Ideas\n\n<!-- thoughts:v1:start {key} -->\nA note about two techniques.\n"
        f"<!-- thoughts:v1:end {key} -->\n")
    assert created["structuredContent"]["state"] == "card_created"
    assert f"Created card {request_id} in ideas.pdf, due 2099-01-01T08:00:00Z" in created["content"][0]["text"]
    replay = call(token, "create_review_card", **arguments)
    assert "replayed" in replay["content"][0]["text"] and len(OPERATIONS) == 1
    # Only a blank line: refused by thoughts-cli, never sent.
    refused = rpc(token, "tools/call", {"name": "create_review_card", "arguments": {
        **arguments, "start_line": 2, "end_line": 3, "request_id": "c6b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_selection"
    # CARD's projection is not an approved card (the fake keeps only what the readers need).
    moved = rpc(token, "tools/call", {"name": "move_review_card", "arguments": {
        "card_id": CARD, "source": source["source"], "expected_source_sha256": source["source_sha256"],
        "start_line": 3, "end_line": 4, "request_id": "c7b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert moved["isError"] and moved["structuredContent"]["error_code"] == "card_unavailable"
    assert len(OPERATIONS) == 1


SECTION_CLI = HAVE_CLI and (CLI / "thoughts_cli" / "review_card_sections.py").is_file()


@pytest.mark.skipif(not SECTION_CLI, reason="thoughts-cli with section cards not found")
def test_create_review_card_of_a_whole_section(token):
    source = call(token, "read_source", source="docs-ideas/markdown/source/ideas.md")["structuredContent"]
    request_id = "d5b6c5b7-1d2a-4c3b-9f8e-112233445566"
    arguments = dict(source=source["source"], expected_source_sha256=source["source_sha256"], section_heading="Ideas",
                     question="What is the section about?", due_at="2099-01-01T09:00:00+01:00", request_id=request_id)
    sent = len(OPERATIONS)
    created = call(token, "create_review_card", **arguments)
    assert len(OPERATIONS) == sent + 1
    operation = OPERATIONS[-1]
    key = f"review:{request_id}:answer:0"
    # The heading stays outside; the section's body is the card's answer.
    assert OBJECTS[operation["payload"]["source_change"]["after_sha256"]].decode() == (
        f"# Ideas\n\n<!-- thoughts:v1:start {key} -->\nA note about two techniques.\n"
        f"<!-- thoughts:v1:end {key} -->\n")
    section = created["structuredContent"]["section"]
    assert (section["title"], section["kind"], section["heading_line"], section["start_line"]) == ("Ideas", "section", 1, 3)
    assert "It hides the section “Ideas” (lines 3–3 before the markers)" in created["content"][0]["text"]
    # A passage and a section at once, or neither: refused before thoughts-cli is asked.
    for extra in ({"start_line": 3, "end_line": 4}, {"section_line": 1}):
        refused = rpc(token, "tools/call", {"name": "create_review_card", "arguments": {
            **arguments, **extra, "request_id": "d6b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_request"
    neither = {key: value for key, value in arguments.items() if key != "section_heading"}
    refused = rpc(token, "tools/call", {"name": "create_review_card", "arguments": {
        **neither, "request_id": "d7b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_request"
    # Not a heading: thoughts-cli refuses, nothing is sent.
    refused = rpc(token, "tools/call", {"name": "create_review_card", "arguments": {
        **neither, "section_line": 3, "request_id": "d8b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_selection"
    assert len(OPERATIONS) == sent + 1


MERGE_CLI = HAVE_CLI and (CLI / "thoughts_cli" / "card_merges.py").is_file()
RESULTS_PATH = "docs-rp/tex/fragments/results.tex"


def current_sha256(token, path):
    return call(token, "read_source", source=path)["structuredContent"]["source_sha256"]


@pytest.mark.skipif(not MERGE_CLI, reason="thoughts-cli with merge-review-cards not found")
def test_merge_review_cards_asks_the_server_once(token):
    digest = current_sha256(token, RESULTS_PATH)
    arguments = dict(card_ids=[CARD, RETIRED], source=RESULTS_PATH, expected_source_sha256=digest,
                     request_id="e5b6c5b7-1d2a-4c3b-9f8e-112233445566", keep_card_id=RETIRED, question="Both results?")
    before = len(REQUESTS)
    merged = call(token, "merge_review_cards", **arguments)
    assert MERGES[-1] == {key: value for key, value in arguments.items()}
    body = merged["structuredContent"]
    assert body["kept_card_id"] == RETIRED and body["deleted_card_ids"] == [CARD] and body["replayed"] is False
    assert body["render_check"]["status"] == "building"
    text = merged["content"][0]["text"]
    assert f"Merged 2 cards of FindingsPaper.pdf into card {RETIRED}" in text and "\u201cBoth results?\u201d" in text
    assert f"Deleted with their markers: {CARD}" in text and "next Sync" in text
    sent = [(method, path) for method, path, _auth in REQUESTS[before:]]
    assert sent.count(("POST", "/v1/review-card-merges")) == 1 and ("POST", "/v1/render-checks") in sent
    replay = call(token, "merge_review_cards", **arguments)
    assert replay["structuredContent"]["replayed"] is True and "replayed" in replay["content"][0]["text"]
    # Defaults are left to the server: only the given fields are sent.
    call(token, "merge_review_cards", card_ids=[CARD, RETIRED], source=RESULTS_PATH, expected_source_sha256=digest,
         request_id="e6b6c5b7-1d2a-4c3b-9f8e-112233445566")
    assert set(MERGES[-1]) == {"card_ids", "source", "expected_source_sha256", "request_id"}
    assert "merge_review_cards" in thoughts_connector.INSTRUCTIONS


@pytest.mark.skipif(not MERGE_CLI, reason="thoughts-cli with merge-review-cards not found")
def test_merge_review_cards_reports_refusals(token):
    digest = current_sha256(token, RESULTS_PATH)

    def refused(**changes):
        arguments = {**dict(card_ids=[CARD, RETIRED], source=RESULTS_PATH, expected_source_sha256=digest,
                            request_id=str(uuid.uuid4())), **changes}
        answer = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": arguments})
        assert answer["isError"], answer
        return answer

    other = refused(card_ids=[CARD, UNPUBLISHED])
    assert other["structuredContent"]["error_code"] == "merge_refused"
    assert "nothing changed" in other["content"][0]["text"] and "2 documents" in other["content"][0]["text"]
    stale = refused(expected_source_sha256="0" * 64)
    assert stale["structuredContent"]["error_code"] == "source_conflict"
    assert f"The current source_sha256 is {digest}" in stale["content"][0]["text"]
    # Refused by the tool's schema or by thoughts-cli: never sent.
    before = len(MERGES)
    refused(card_ids=[CARD])
    assert refused(keep_card_id=GONE)["structuredContent"]["error_code"] == "invalid_request"
    assert len(MERGES) == before


def test_merge_review_cards_needs_the_server_capability(server, token):
    CAPABILITIES.remove("review_card_merge_v1")
    server.thoughts.cached.pop("health", None)
    try:
        before = len(MERGES)
        refused = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": {
            "card_ids": [CARD, RETIRED], "source": RESULTS_PATH, "expected_source_sha256": RESULTS[0],
            "request_id": "e7b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "not_supported"
        assert "Merge cards" in refused["content"][0]["text"] and len(MERGES) == before
    finally:
        CAPABILITIES.append("review_card_merge_v1")
        server.thoughts.cached.pop("health", None)


def test_merge_review_cards_needs_a_thoughts_cli_that_merges(token, monkeypatch):
    from types import SimpleNamespace
    old_cli = SimpleNamespace(client=SimpleNamespace(COMMANDS=("create-review-card", "move-review-card")))
    monkeypatch.setattr(thoughts_connector, "load_thoughts_cli", lambda: old_cli)
    before = len(MERGES)
    refused = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": {
        "card_ids": [CARD, RETIRED], "source": RESULTS_PATH, "expected_source_sha256": RESULTS[0],
        "request_id": "e8b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "not_supported"
    assert "thoughts-cli" in refused["content"][0]["text"] and len(MERGES) == before


PDF_MERGE_CLI = MERGE_CLI and "is_pdf_merge" in (CLI / "thoughts_cli" / "card_merges.py").read_text()
PDF_ONE = "77777777-7777-4777-8777-777777777777"
PDF_TWO = "88888888-8888-4888-8888-888888888888"


@pytest.mark.skipif(not PDF_MERGE_CLI, reason="thoughts-cli that merges cards of a PDF (1.8.0) not found")
def test_merge_review_cards_of_a_pdf_sends_no_source(token):
    arguments = dict(card_ids=[PDF_ONE, PDF_TWO], request_id="e9b6c5b7-1d2a-4c3b-9f8e-112233445566",
                     keep_card_id=PDF_TWO)
    before = len(REQUESTS)
    merged = call(token, "merge_review_cards", **arguments)
    assert MERGES[-1] == arguments
    body = merged["structuredContent"]
    assert body["kind"] == "pdf" and body["kept_card_id"] == PDF_TWO and body["deleted_card_ids"] == [PDF_ONE]
    assert "render_check" not in body
    text = merged["content"][0]["text"]
    assert f"Merged 2 cards of scan.pdf into card {PDF_TWO}" in text and "4 area(s) on the PDF" in text
    assert f"Deleted: {PDF_ONE}" in text and "nothing in the PDF changed" in text and "next Sync" in text
    sent = [(method, path) for method, path, _auth in REQUESTS[before:]]
    # Nothing to build: the cards keep their areas.
    assert sent.count(("POST", "/v1/review-card-merges")) == 1 and ("POST", "/v1/render-checks") not in sent
    replay = call(token, "merge_review_cards", **arguments)
    assert replay["structuredContent"]["replayed"] is True and "replayed" in replay["content"][0]["text"]


def test_merge_review_cards_needs_both_source_fields_or_neither(token):
    before = len(MERGES)
    for extra in ({"source": RESULTS_PATH}, {"expected_source_sha256": RESULTS[0]}):
        refused = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": {
            "card_ids": [PDF_ONE, PDF_TWO], "request_id": str(uuid.uuid4()), **extra}})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_request"
        assert "leave both out" in refused["content"][0]["text"]
    assert len(MERGES) == before


def test_merge_review_cards_of_a_pdf_needs_the_server_and_thoughts_cli_to_support_it(server, token, monkeypatch):
    arguments = {"card_ids": [PDF_ONE, PDF_TWO], "request_id": "eab6c5b7-1d2a-4c3b-9f8e-112233445566"}
    before = len(MERGES)
    CAPABILITIES.remove("review_card_merge_pdf_v1")
    server.thoughts.cached.pop("health", None)
    try:
        refused = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": arguments})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "not_supported"
        assert "review_card_merge_pdf_v1" in refused["content"][0]["text"]
    finally:
        CAPABILITIES.append("review_card_merge_pdf_v1")
        server.thoughts.cached.pop("health", None)
    from types import SimpleNamespace
    older_cli = SimpleNamespace(client=SimpleNamespace(COMMANDS=("merge-review-cards",)),
                                card_merges=SimpleNamespace(CAPABILITY="review_card_merge_v1"))
    monkeypatch.setattr(thoughts_connector, "load_thoughts_cli", lambda: older_cli)
    refused = rpc(token, "tools/call", {"name": "merge_review_cards", "arguments": arguments})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "not_supported"
    assert "thoughts-cli 1.8.0" in refused["content"][0]["text"] and len(MERGES) == before

PENDING_CLI = HAVE_CLI and (CLI / "thoughts_cli" / "card_pending_moves.py").is_file()
PENDING_PID = "agent-5e0b6f7a-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
PENDING_CARD = "66666666-6666-4666-8666-666666666666"
IDEAS_PATH = "docs-ideas/markdown/source/ideas.md"
PENDING_NOTE = ("# Ideas\n\n<!-- thoughts:agent-proposal:v2:start\n" f"id: {PENDING_PID}\n"
                "created_at: 2026-11-12T09:00:00.000Z\nconversation_id: 01a0abcd-ef01-7abc-8def-0123abcdef45\n"
                "explanation:\n| Clearer.\noriginal_text:\n| A note about two techniques.\n-->\n"
                "Which two techniques?\n" f"<!-- thoughts:agent-proposal:v2:end {PENDING_PID} -->\n")


@pytest.fixture
def pending_note(server):
    """ideas.md with a pending proposal, and an approved card on an imported PDF (restored afterwards)."""
    record = next(s for s in SOURCES if s["path"] == IDEAS_PATH)
    saved = dict(record)
    digest, data = obj(PENDING_NOTE)
    OBJECTS[digest] = data
    record.update(sha256=digest, object_sha256=digest, revision=2)
    card = {"entity_type": "review_card", "entity_id": PENDING_CARD, "deleted": False, "revision": 1, "payload": {
        "card_id": PENDING_CARD, "status": "approved", "revision": 1, "created_at": "2026-11-01T00:00:00Z",
        "primary": {"document_id": "paper", "pdf_stem": "Paper_p3", "page": 2, "rect": [0.1, 0.1, 0.4, 0.2],
                    "question": "What does the second technique avoid?"}}}
    PROJECTIONS["projections"].append(card)
    for key in ("sources", "projections", "health"):
        server.thoughts.cached.pop(key, None)
    try:
        yield digest
    finally:
        record.clear()
        record.update(saved)
        PROJECTIONS["projections"].remove(card)
        PENDING_MOVES.clear()
        for key in ("sources", "projections", "health"):
            server.thoughts.cached.pop(key, None)


@pytest.mark.skipif(not PENDING_CLI, reason="thoughts-cli with pending card moves not found")
def test_move_review_card_onto_a_pending_proposal_waits_for_it(token, pending_note):
    arguments = dict(card_id=PENDING_CARD, source=IDEAS_PATH, expected_source_sha256=pending_note, start_line=12,
                     end_line=13, request_id="f5b6c5b7-1d2a-4c3b-9f8e-112233445566")
    before = len(OPERATIONS)
    waiting = call(token, "move_review_card", **arguments)
    body = waiting["structuredContent"]
    assert (body["state"], body["proposal_id"], body["pending_move_id"]) == (
        "waiting_for_proposal", PENDING_PID, arguments["request_id"])
    text = waiting["content"][0]["text"]
    assert f"waits for the user to accept proposal {PENDING_PID}" in text and "Nothing has changed yet" in text
    assert "stays on Paper_p3.pdf" in text and len(OPERATIONS) == before  # no card operation, no source change
    replay = call(token, "move_review_card", **arguments)
    assert replay["structuredContent"]["replayed"] is True and "replayed" in replay["content"][0]["text"]
    detail = call(token, "get_review_card", card_id=PENDING_CARD)
    assert [m["pending_move_id"] for m in detail["structuredContent"]["pending_moves"]] == [arguments["request_id"]]
    assert "waiting for the user to accept it" in detail["content"][0]["text"]
    listed = call(token, "pending_card_moves")
    assert listed["structuredContent"]["count"] == 1 and PENDING_PID in listed["content"][0]["text"]
    cancelled = call(token, "cancel_pending_card_move", pending_move_id=arguments["request_id"])
    assert cancelled["structuredContent"]["state"] == "cancelled" and "stays on Paper_p3.pdf" in cancelled["content"][0]["text"]
    assert call(token, "pending_card_moves")["structuredContent"]["count"] == 0
    everything = call(token, "pending_card_moves", card_id=PENDING_CARD, state="all")["structuredContent"]
    assert [m["state"] for m in everything["pending_moves"]] == ["cancelled"]
    again = call(token, "cancel_pending_card_move", pending_move_id=arguments["request_id"])
    assert "already cancelled" in again["content"][0]["text"]
    missing = rpc(token, "tools/call", {"name": "cancel_pending_card_move",
                                        "arguments": {"pending_move_id": "f6b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert missing["isError"] and missing["structuredContent"]["error_code"] == "not_found"


@pytest.mark.skipif(not PENDING_CLI, reason="thoughts-cli with pending card moves not found")
def test_pending_moves_need_the_server_capability(server, token, pending_note):
    CAPABILITIES.remove("review_card_pending_moves_v1")
    server.thoughts.cached.pop("health", None)
    try:
        refused = rpc(token, "tools/call", {"name": "move_review_card", "arguments": dict(
            card_id=PENDING_CARD, source=IDEAS_PATH, expected_source_sha256=pending_note, start_line=12, end_line=13,
            request_id="f7b6c5b7-1d2a-4c3b-9f8e-112233445566")})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "invalid_selection"
        assert "cannot wait for the proposal" in refused["structuredContent"]["message"] and not PENDING_MOVES
        listed = rpc(token, "tools/call", {"name": "pending_card_moves", "arguments": {}})
        assert listed["isError"] and listed["structuredContent"]["error_code"] == "not_supported"
        detail = call(token, "get_review_card", card_id=PENDING_CARD)["structuredContent"]
        assert "pending_moves" not in detail
    finally:
        CAPABILITIES.append("review_card_pending_moves_v1")
        server.thoughts.cached.pop("health", None)


def test_pending_move_routes_are_allowed_and_nothing_else():
    move_id = "f8b6c5b7-1d2a-4c3b-9f8e-112233445566"
    for method, route in (("POST", "/v1/review-card-pending-moves"), ("GET", "/v1/review-card-pending-moves"),
                          ("GET", f"/v1/review-card-pending-moves/{move_id}"),
                          ("POST", f"/v1/review-card-pending-moves/{move_id}/cancel")):
        thoughts_connector.check_request(method, route, {})
    for method, route in (("DELETE", f"/v1/review-card-pending-moves/{move_id}"),
                          ("POST", f"/v1/review-card-pending-moves/{move_id}/accept"),
                          ("POST", "/v1/review-card-pending-moves/not-a-uuid/cancel")):
        with pytest.raises(thoughts_connector.ThoughtsError):
            thoughts_connector.check_request(method, route, {})


def test_knowledge_profile_is_wired_and_read_only(token):
    before = len(REQUESTS)
    profile = call(token, "knowledge_profile", topic="current results")["structuredContent"]
    assert profile["accepted"] and profile["topic"] == "current results"
    # learner reaches the profile: with it, what all the cards say about the user; without, as few cards match.
    assert "learner" in call(token, "knowledge_profile", topic="current results", learner=True)["structuredContent"]
    assert "learner" not in call(token, "knowledge_profile", topic="current results", learner=False)["structuredContent"]
    assert all(method == "GET" for method, _path, _auth in REQUESTS[before:])


def test_operations_are_only_diary_links_and_marked_cards():
    card = {"entity_type": "review_card", "payload": {"status": "approved", "source_authority": "client-v1",
                                                      "source_change": {"path": "x.md"}}}
    for allowed in (card, {"entity_type": "logical_comment", "payload": {}},
                    {"entity_type": "agent_conversation", "payload": {}}):
        thoughts_connector.check_request("POST", "/v1/operations", {"operations": [allowed]})
    for refused in ({"entity_type": "review_event", "payload": {"card_id": CARD, "rating": "delete"}},
                    {"entity_type": "review_card", "payload": {"status": "approved", "source_authority": "client-v1"}},
                    {"entity_type": "review_card", "payload": {**card["payload"], "status": "deleted"}},
                    {"entity_type": "proposal", "payload": {}}):
        with pytest.raises(thoughts_connector.ThoughtsError):
            thoughts_connector.check_request("POST", "/v1/operations", {"operations": [card, refused]})
def test_merge_is_resolved_without_the_user(token):
    wrapper = "\\agentProposedEdit[created=x]{agent-one}{Why.\n}{Old.\n}{New.\n}"
    server = f"One.\n{wrapper}Two.\nThree, server.\n"
    computer = "One.\nTwo.\nThree, Mac.\n"
    merged = f"One.\n{wrapper}Two.\n<<<<<<< a\nThree, Mac.\n=======\nThree, server.\n>>>>>>> b\n"

    def stored(text):
        OBJECTS[hashlib.sha256(text.encode()).hexdigest()] = text.encode()
        return hashlib.sha256(text.encode()).hexdigest()

    base_sha, server_sha, computer_sha, merged_sha = map(stored, ("One.\nTwo.\nThree.\n", server, computer, merged))
    conflict = "3f0c5a2e-6c1f-4d7a-9e55-1a2b3c4d5e6f"
    stale = "4f0c5a2e-6c1f-4d7a-9e55-1a2b3c4d5e6f"
    CONFLICTS[:] = [{"id": stale, "path": "docs-x/old.tex", "base_sha256": base_sha, "current_sha256": server_sha,
                     "incoming_sha256": computer_sha, "merged_object_sha256": None, "conflict_count": 1, "expected_revision": None},
                    {"id": conflict, "path": "docs-x/note.tex", "base_sha256": base_sha, "current_sha256": server_sha,
                     "incoming_sha256": computer_sha, "merged_object_sha256": merged_sha, "conflict_count": 1, "expected_revision": 7}]
    listed = call(token, "source_conflicts")["structuredContent"]["conflicts"]
    assert [(c["conflict_id"], c["resolvable"]) for c in listed] == [(stale, False), (conflict, True)]
    shown = call(token, "read_source_conflict", conflict_id=conflict)["structuredContent"]
    assert shown["text"] == merged and shown["marker_lines"] == [6]
    assert call(token, "read_source_conflict", conflict_id=conflict, version="computer")["structuredContent"]["text"] == computer

    def refused(code, **arguments):
        answer = rpc(token, "tools/call", {"name": "resolve_source_conflict", "arguments": {"conflict_id": conflict, **arguments}})
        assert answer["isError"] and answer["structuredContent"]["error_code"] == code and not RESOLVED
        return answer["structuredContent"]

    # A merge combines versions: markers, new wording and lost proposals are refused, and nothing is sent.
    refused("invalid_merge", keep="merged", merged_text=merged)
    refused("invalid_merge", keep="merged", merged_text="")
    refused("invalid_merge", keep="merged", merged_text=f"One.\n{wrapper}Two.\nThree, rewritten by the agent.\n")
    assert refused("would_drop_proposals", keep="computer")["proposal_ids"] == ["agent-one"]
    assert refused("would_drop_proposals", keep="merged", merged_text="One.\nTwo.\nThree, Mac.\n")["proposal_ids"] == ["agent-one"]
    assert rpc(token, "tools/call", {"name": "resolve_source_conflict", "arguments": {"conflict_id": stale, "keep": "server"}})["structuredContent"]["error_code"] == "not_resolvable"

    good = f"One.\n{wrapper}Two.\nThree, Mac.\n"
    answer = call(token, "resolve_source_conflict", conflict_id=conflict, keep="merged", merged_text=good)["structuredContent"]
    assert answer["outcome"] == "applied" and answer["dropped_proposals"] == []
    path, body = RESOLVED[-1]
    assert path == f"/v1/conflicts/{conflict}/resolve"
    assert body["expected_revision"] == 7 and body["expected_sha256"] == server_sha
    assert OBJECTS[body["content_sha256"]] == good.encode()
    assert set(body) == {"operation_id", "device_sequence", "expected_revision", "expected_sha256", "content_sha256", "client_timestamp"}
    # Calling again finds it closed instead of writing twice.
    assert call(token, "resolve_source_conflict", conflict_id=conflict, keep="server")["structuredContent"]["state"] == "not_open"
    assert len(RESOLVED) == 1


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "html_pages.py").is_file()),
                    reason="thoughts-cli with HTML pages not found")
def test_an_html_page_is_added_and_then_linked_from_a_note(token):
    page = "docs-rp/tex/support/html/slope-explorer.html"
    html = "<!doctype html><title>Curve</title><input type=range>"
    conversation = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
    source = call(token, "read_source", source="docs-rp/tex/fragments/results.tex")["structuredContent"]
    link = "See \\href{thoughts-html:" + page + "}{the interactive curve}.\n"
    edit = {"start_line": source["lines"] + 1, "end_line": source["lines"] + 1, "replacement": link,
            "explanation": "Link the interactive page."}
    missing = rpc(token, "tools/call", {"name": "create_proposals", "arguments": {
        "source": source["source"], "expected_source_sha256": source["source_sha256"], "proposals": [edit],
        "conversation_id": conversation, "request_id": "5c6c5b7e-1d2a-4c3b-9f8e-112233445566"}})
    assert missing["isError"] and missing["structuredContent"]["error_code"] == "invalid_link"
    assert missing["structuredContent"]["links"][0]["reason"] == "html_page_not_found"
    published = len(PUBLISHED)
    saved = call(token, "add_html_page", folder="docs-rp/tex/fragments/results.tex", name="slope-explorer", html=html,
                 request_id="6c6c5b7e-1d2a-4c3b-9f8e-112233445566")
    body = saved["structuredContent"]
    assert body["path"] == page and body["link"] == "thoughts-html:" + page and len(PUBLISHED) == published
    assert OBJECTS[body["sha256"]] == html.encode()
    assert HTML_CHANGES[-1]["changes"] == [{"path": page, "sha256": body["sha256"], "base_sha256": None}]
    assert "\\href{thoughts-html:" + page + "}" in saved["content"][0]["text"]
    listed = call(token, "html_pages", folder="docs-rp", path=page)["structuredContent"]
    assert [p["path"] for p in listed["pages"]] == [page] and listed["html"] == html
    exists = rpc(token, "tools/call", {"name": "add_html_page", "arguments": {
        "folder": "docs-rp", "name": "slope-explorer", "html": html + "<p>2", "request_id": "7c6c5b7e-1d2a-4c3b-9f8e-112233445566"}})
    assert exists["isError"] and exists["structuredContent"]["error_code"] == "html_page_exists"
    replaced = call(token, "add_html_page", folder="docs-rp", name="slope-explorer", html=html + "<p>2", replace=True,
                    request_id="8c6c5b7e-1d2a-4c3b-9f8e-112233445566")["structuredContent"]
    assert replaced["replaced"] is True and HTML_CHANGES[-1]["changes"][0]["base_sha256"] == body["sha256"]
    created = call(token, "create_proposals", source=source["source"], expected_source_sha256=source["source_sha256"],
                   proposals=[edit], conversation_id=conversation, request_id="9c6c5b7e-1d2a-4c3b-9f8e-112233445566")
    assert created["structuredContent"]["accepted"] is True and len(PUBLISHED) == published + 1
    assert "thoughts-html:" + page in OBJECTS[PUBLISHED[-1]["mutations"][0]["content_sha256"]].decode()


def test_html_pages_are_only_added_or_replaced():
    change = {"path": "docs-x/tex/support/html/a.html", "sha256": "a" * 64, "base_sha256": None}
    thoughts_connector.check_request("POST", "/v1/html-pages", {"folder": "docs-x", "changes": [change]})
    for changes in ([{**change, "sha256": None}], [change, change], []):
        with pytest.raises(thoughts_connector.ThoughtsError, match="never remove"):
            thoughts_connector.check_request("POST", "/v1/html-pages", {"folder": "docs-x", "changes": changes})


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "build_status.py").is_file()),
                    reason="thoughts-cli with build-status not found")
def test_build_status_names_the_failing_line(token):
    log = obj("MJPDF_FAILURE\tideas.pdf\tdocs-ideas/markdown/source/ideas.md\tmarkdown_failed: not well-formed\n")
    OBJECTS[log[0]] = log[1]
    message = "markdown_failed: Source docs-ideas/markdown/source/ideas.md:3 failed: not well-formed (invalid token)"
    RENDER_STATUS["ideas.pdf"] = {
        "status": "attention", "reason": "The newest build failed.", "source_frontier": 9,
        "build": {"id": "0fabcdef-3a4b-4c5d-ae6f-7a8b9c0d1e2f", "status": "failed", "target": "all", "error": message,
                  "result": {"failed_documents": [{"pdf_filename": "ideas.pdf", "message": message}], "warnings": [],
                             "log_sha256": log[0]}},
        "publication": {"published_at": "2026-11-10T12:00:00Z", "build_id": "b0"}}
    try:
        body = call(token, "build_status", document="Ideas")["structuredContent"]
    finally:
        RENDER_STATUS.clear()
    assert body["status"] == "attention" and body["pdf_filename"] == "ideas.pdf", body
    assert body["errors"][0]["source"] == "docs-ideas/markdown/source/ideas.md" and body["errors"][0]["line"] == 3
    assert body["error_lines"][0]["line"] == 3
    assert "does not build at docs-ideas/markdown/source/ideas.md:3" in body["message"]


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "pdf_moves.py").is_file()),
                    reason="thoughts-cli with move-pdf not found")
def test_move_pdf_moves_an_imported_pdf_and_back(token):
    there = call(token, "move_pdf", pdf="hidden.pdf", project="Notes",
                 request_id="d1b6c5b7-1d2a-4c3b-9f8e-112233445566")["structuredContent"]
    assert there["state"] == "pdf_moved" and (there["from_project"], there["to_project"]) == ("Research", "Notes")
    assert LIBRARY_CHANGES[-1]["action"] == "move_document" and LIBRARY_CHANGES[-1]["destination_project_id"] == "p3"
    assert "keep working" in there["message"]
    back = call(token, "move_pdf", pdf="doc-hidden", project="Research",
                request_id="d2b6c5b7-1d2a-4c3b-9f8e-112233445566")["structuredContent"]
    assert back["to_project"] == "Research"
    assert any(d["document_id"] == "doc-hidden" for d in PROJECTS["projects"][0]["unpublished_documents"])
    refused = rpc(token, "tools/call", {"name": "move_pdf", "arguments": {
        "pdf": "hidden.pdf", "project": "Research", "request_id": "d3b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
    assert refused["isError"] and "no_change" in refused["content"][0]["text"]


def test_a_project_of_its_own_folder_and_a_note_moved_into_it(token):
    """create_project gives the Project a docs folder; move_note moves a note with its card, links and page."""
    drafts = {"id": "p9", "name": "Drafts", "revision": 2, "unpublished_documents": [], "documents": [
        {"document_id": "doc-vectors", "name": "Vectors, briefly", "included": True,
         "source_file": "docs-drafts/tex/source/vectors.tex", "source_project": "docs-drafts",
         "sha256": "d" * 64, "page_count": 3}],
        "source_folders": [{"project": "docs-drafts", "folder": "drafts"}],
        "project_configuration": {"id": "p9", "name": "Drafts", "default_folder_id": "f9", "document_names": {},
                                  "source_folders": [{"folder_id": "f9", "path": ".thoughts/projects/c/default"},
                                                     {"folder_id": "fc", "path": "docs-drafts"}],
                                  "documents": [], "rendering": {}}}
    PROJECTS["projects"].append(drafts)
    count = len(PROJECTS["projects"])
    try:
        rid = "e1b6c5b7-1d2a-4c3b-9f8e-112233445566"
        created = call(token, "create_project", name="algebra", request_id=rid)
        body = created["structuredContent"]
        assert body["state"] == "project_created" and body["project_id"] == "new:" + rid
        assert LIBRARY_CHANGES[-1] == {"action": "create", "operation_id": rid, "project_id": "new:" + rid,
                                       "name": "algebra", "base_revision": 0, "source_folder": None}
        [folder] = body["source_folders"]
        assert folder == {"folder": "docs-algebra-e1b6c5", "name": "algebra"}
        assert "own folder docs-algebra-e1b6c5" in created["content"][0]["text"]
        replay = call(token, "create_project", name="algebra", request_id=rid)["structuredContent"]
        assert replay["replayed"] is True and len(PROJECTS["projects"]) == count + 1
        taken = rpc(token, "tools/call", {"name": "create_project", "arguments": {
            "name": "Algebra", "request_id": "e2b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
        assert taken["isError"] and taken["structuredContent"]["error_code"] == "project_exists"

        planned = call(token, "move_note", document="Vectors, briefly", project="algebra", dry_run=True,
                       request_id="e3b6c5b7-1d2a-4c3b-9f8e-112233445566")
        assert planned["structuredContent"]["state"] == "move_planned"
        assert LIBRARY_CHANGES[-1]["action"] == "plan_move_source_document"
        assert "docs-algebra-e1b6c5/tex/support/html/explainer.html" in planned["content"][0]["text"]
        assert drafts["documents"] and drafts["documents"][0]["document_id"] == "doc-vectors"  # nothing moved

        moved = call(token, "move_note", document="doc-vectors", project="algebra",
                     request_id="e4b6c5b7-1d2a-4c3b-9f8e-112233445566")
        result = moved["structuredContent"]
        assert result["state"] == "document_moved", result
        assert result["to"] == "docs-algebra-e1b6c5/tex/source/vectors.tex" and result["review_card_markers"] == 1
        sent = LIBRARY_CHANGES[-1]
        assert sent["action"] == "move_source_document" and sent["plan_digest"] == "digest-docs-algebra-e1b6c5"
        assert sent["carry_preamble"] is True and sent["destination_path"] == "tex/source/vectors.tex"
        text = moved["content"][0]["text"]
        assert "Links rewritten in: docs-drafts/tex/source/matrices.tex" in text
        assert "Interactive pages listed in the new folder" in text and drafts["documents"] == []
        algebra = next(p for p in PROJECTS["projects"] if p["name"] == "algebra")
        assert [d["document_id"] for d in algebra["documents"]] == ["doc-vectors"]

        plain = call(token, "create_project", name="sketches", source_folder=False,
                     request_id="e5b6c5b7-1d2a-4c3b-9f8e-112233445566")["structuredContent"]
        assert plain["source_folders"] == [] and "source_folder" not in LIBRARY_CHANGES[-1]
        added = call(token, "add_source_folder", project="sketches",
                     request_id="e6b6c5b7-1d2a-4c3b-9f8e-112233445566")["structuredContent"]
        assert added["state"] == "source_folder_added" and added["folder"].startswith("docs-sketches-")
        assert LIBRARY_CHANGES[-1]["action"] == "add_source_folder"
        again = rpc(token, "tools/call", {"name": "add_source_folder", "arguments": {
            "project": "sketches", "request_id": "e7b6c5b7-1d2a-4c3b-9f8e-112233445566"}})
        assert again["isError"] and "already has this name" in again["content"][0]["text"]
        assert {change["action"] for change in LIBRARY_CHANGES} <= thoughts_connector.LIBRARY_ACTIONS
    finally:
        PROJECTS["projects"][:] = [p for p in PROJECTS["projects"] if p["id"] in {"p1", "p2", "p3"}]


def test_import_pdf_imports_from_a_url_and_a_second_import_is_a_replay(token):
    url = "https://papers.example.org/pdf/2401.12345"
    imported = call(token, "import_pdf", project="notes", name="A paper", url=url)
    body = imported["structuredContent"]
    assert URL_IMPORTS[-1] == {"project": "notes", "name": "A paper", "url": url}
    assert body["accepted"] and not body["replayed"] and body["project_id"] == "p3"
    assert body["document_id"] and body["filename"].endswith(".pdf") and body["source_url"] == url
    text = imported["content"][0]["text"]
    assert "Imported A paper into Notes" in text and body["document_id"] in text
    assert "\\PdfLink{" + body["filename"].removesuffix(".pdf") + "}{document-start}{A paper}" in text
    again = call(token, "import_pdf", project="p3", name="Another title", url=url)["structuredContent"]
    assert again["replayed"] is True and again["document_id"] == body["document_id"]
    assert "already has this PDF" in call(token, "import_pdf", project="Notes", name="A paper", url=url)["content"][0]["text"]
    assert "import_pdf" in thoughts_connector.INSTRUCTIONS and "move_pdf" in thoughts_connector.INSTRUCTIONS


def test_import_pdf_reports_the_server_refusal(token):
    refused = rpc(token, "tools/call", {"name": "import_pdf", "arguments": {
        "project": "Notes", "name": "Secret", "url": "https://intranet.example.org/secret.pdf"}})
    assert refused["isError"] and refused["structuredContent"]["error_code"] == "address_refused"
    assert "nothing imported" in refused["content"][0]["text"]
    missing = rpc(token, "tools/call", {"name": "import_pdf", "arguments": {
        "project": "Nope", "name": "A paper", "url": "https://papers.example.org/pdf/1"}})
    assert missing["isError"] and missing["structuredContent"]["error_code"] == "project_not_found"


def test_import_pdf_needs_the_server_capability(server, token):
    CAPABILITIES.remove("library_import_pdf_v1")
    server.thoughts.cached.pop("health", None)
    try:
        before = len(URL_IMPORTS)
        refused = rpc(token, "tools/call", {"name": "import_pdf", "arguments": {
            "project": "Notes", "name": "A paper", "url": "https://papers.example.org/pdf/2"}})
        assert refused["isError"] and refused["structuredContent"]["error_code"] == "not_supported"
        assert len(URL_IMPORTS) == before
    finally:
        CAPABILITIES.append("library_import_pdf_v1")
        server.thoughts.cached.pop("health", None)


def test_only_allowed_requests_with_its_own_token(token):
    allowed = {(m, p.pattern) for m, p in thoughts_connector.ALLOWED}
    assert REQUESTS
    for method, path, auth in REQUESTS:
        assert any(method == m and re.match(p, path) for m, p in allowed), (method, path)
        assert auth == f"Bearer {TOKEN}"
    assert {change["action"] for change in LIBRARY_CHANGES} <= thoughts_connector.LIBRARY_ACTIONS


def test_bad_token_pauses_instead_of_hammering(server, token):
    (TMP / "token").write_text("wrong-token")
    try:
        server.thoughts.cached.clear()
        before = len(REQUESTS)
        assert rpc(token, "tools/call", {"name": "review_cards", "arguments": {}})["isError"]
        assert rpc(token, "tools/call", {"name": "review_cards", "arguments": {}})["isError"]
        assert len(REQUESTS) == before + 1  # the second call never reached the server
    finally:
        (TMP / "token").write_text(TOKEN)
        server.thoughts.paused_until = 0


def test_unpublished_document_cards(token):
    """A card of an unpublished document is listed as unpublished, never as active."""
    listed = call(token, "review_cards", state="unpublished")
    [card] = listed["structuredContent"]["cards"]
    assert (card["card_id"], card["status"], card["document"]) == (UNPUBLISHED, "unpublished", "Hidden")
    assert "unpublished document" in listed["content"][0]["text"]
    assert UNPUBLISHED not in [c["card_id"] for c in call(token, "review_cards", state="active")["structuredContent"]["cards"]]


def test_a_drawing_is_named_as_one_and_never_counts_as_open_or_disliked():
    pdf = {"document_id": "d1", "pdf_sha256": "a" * 64, "regions": [{"page": 3, "rect": [0, 0, 1, 1]}]}
    drawing = {"feedback_id": "f1", "polarity": "neutral", "kind": "drawing", "comment": "", "created_at": "2026-11-05T10:00:00Z",
               "anchor": {"quote": {"exact": "Drawing"}, "pdf": pdf}, "drawing": {"strokes": [{"points": [[0.1, 0.2]]}]}}
    summary = thoughts_connector.feedback_summary(drawing, "a" * 64)
    assert summary["kind"] == "drawing"
    assert summary["page"] == 3
    assert "drawing" not in summary
    assert thoughts_connector.feedback_line(summary) == "- Drawing, p. 3 (current PDF), 2026-11-05"
    summary["comment"] = "See this"
    assert thoughts_connector.feedback_line(summary) == "- Drawing, p. 3 (current PDF), 2026-11-05: See this"
    assert thoughts_connector.feedback_counts([drawing]) == {"liked": 0, "disliked": 0}


def test_a_text_box_reads_as_its_text_on_its_page():
    pdf = {"document_id": "d1", "pdf_sha256": "a" * 64, "regions": [{"page": 3, "rect": [0, 0, 1, 1]}]}
    box = {"feedback_id": "f2", "polarity": "neutral", "kind": "text", "comment": "Why not the median?", "created_at": "2026-11-09T10:00:00Z",
           "anchor": {"quote": {"exact": "Text"}, "pdf": pdf}, "text_box": {"rect": [0.1, 0.3, 0.4, 0.32], "font_size": 0.015}}
    summary = thoughts_connector.feedback_summary(box, "b" * 64)
    assert summary["kind"] == "text"
    assert summary["page"] == 3
    assert summary["comment"] == "Why not the median?"
    assert "text_box" not in summary
    assert thoughts_connector.feedback_line(summary) == "- Text box, p. 3 (earlier PDF), 2026-11-09: \u201cWhy not the median?\u201d"
    assert thoughts_connector.feedback_counts([box]) == {"liked": 0, "disliked": 0}


def test_feedback_in_an_html_page_names_the_page_instead_of_a_page_number():
    pdf = {"document_id": "d1", "pdf_sha256": "a" * 64, "regions": []}
    feedback = {"feedback_id": "f1", "polarity": "negative", "comment": "Too long", "view": "html_attachment", "created_at": "2026-11-04T10:00:00Z",
                "anchor": {"quote": {"exact": "The item is easy"}, "pdf": pdf,
                           "html_attachment": {"path": "docs-irt/html/page.html", "sha256": "b" * 64},
                           "page_state": {"hash": "#curves"}}}
    summary = thoughts_connector.feedback_summary(feedback, "a" * 64)
    assert summary["page"] is None
    assert summary["html_attachment"] == {"path": "docs-irt/html/page.html", "page_state": {"hash": "#curves"}}
    line = thoughts_connector.feedback_line(summary)
    assert line == "- Disliked, HTML page docs-irt/html/page.html (current PDF), 2026-11-04: “The item is easy” — Too long"
    # Feedback on the PDF reads as before and names no page.
    on_pdf = dict(feedback, view="pdf", comment="", anchor=dict(feedback["anchor"], pdf=dict(pdf, regions=[{"page": 3, "rect": [0, 0, 1, 1]}])))
    summary = thoughts_connector.feedback_summary(on_pdf, "a" * 64)
    assert "html_attachment" not in summary
    assert thoughts_connector.feedback_line(summary) == "- Disliked, p. 3 (current PDF), 2026-11-04: “The item is easy”"


PASSAGE_CLI = HAVE_CLI and (CLI / "thoughts_cli" / "card_passage_moves.py").is_file()
LINKED_CARD = "77777777-7777-4777-8777-777777777777"
LINKED_FROM, LINKED_TO = "docs-ideas/markdown/source/linked.md", "docs-rp/markdown/source/target.md"
LINKED_KEY = f"review:{LINKED_CARD}:answer:0"
LINKED_PASSAGE = (f"<!-- thoughts:v1:start {LINKED_KEY} -->\nThe second technique avoids the quadratic memory.\n"
                  f"<!-- thoughts:v1:end {LINKED_KEY} -->\n")
LINKED_NOTE = "# Linked\n\n" + LINKED_PASSAGE + "\nAfter.\n"
TARGET_NOTE = "# Target\n\nFirst.\n\nSecond.\n"
MOVE_CONVERSATION = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"


@pytest.fixture
def linked_note(server):
    """Two Markdown notes of different folders and an approved card linked to markers in the first."""
    records = []
    for path, text in ((LINKED_FROM, LINKED_NOTE), (LINKED_TO, TARGET_NOTE)):
        digest, data = obj(text)
        OBJECTS[digest] = data
        records.append({"path": path, "revision": 1, "sha256": digest, "object_sha256": digest, "deleted": False})
    SOURCES.extend(records)
    card = {"entity_type": "review_card", "entity_id": LINKED_CARD, "deleted": False, "revision": 1, "payload": {
        "card_id": LINKED_CARD, "status": "approved", "revision": 1, "created_at": "2026-11-01T00:00:00Z",
        "primary": {"document_id": "doc-ideas", "pdf_stem": "ideas", "question": "What does the second technique avoid?",
                    "source_anchor": {"version": 2, "segments": [{"segment_id": "0", "source_file": LINKED_FROM}]}}}}
    PROJECTIONS["projections"].append(card)
    for key in ("sources", "projections", "health"):
        server.thoughts.cached.pop(key, None)
    try:
        yield records[1]["sha256"]
    finally:
        for record in records:
            SOURCES.remove(record)
        PROJECTIONS["projections"].remove(card)
        for key in ("sources", "projections", "health"):
            server.thoughts.cached.pop(key, None)


def source_text(path: str) -> str:
    return OBJECTS[next(s for s in SOURCES if s["path"] == path)["sha256"]].decode()


@pytest.mark.skipif(not PASSAGE_CLI, reason="thoughts-cli with move-card-passage not found")
def test_move_review_card_moves_a_linked_card_as_one_decision(token, linked_note):
    """A card linked to source markers: start_line == end_line proposes the cut and the paste together."""
    arguments = dict(card_id=LINKED_CARD, source=LINKED_TO, expected_source_sha256=linked_note, start_line=5, end_line=5,
                     request_id="a5b6c5b7-1d2a-4c3b-9f8e-112233445566", explanation="Merge the notes.",
                     conversation_id=MOVE_CONVERSATION)
    before, operations = len(PUBLISHED), len(OPERATIONS)
    moved = call(token, "move_review_card", **arguments)
    body = moved["structuredContent"]
    assert (body["state"], body["card_id"], body["source"], body["origin_source"]) == (
        "move_proposed", LINKED_CARD, LINKED_TO, LINKED_FROM)
    cut, paste = body["proposals"]
    base = cut["proposal_id"].removesuffix(".cut")
    assert re.fullmatch(r"agent-[0-9a-f-]{36}", base) and paste["proposal_id"] == base + ".paste"
    assert (cut["original_text"], cut["replacement"], paste["original_text"], paste["replacement"]) == (
        LINKED_PASSAGE, "", "", LINKED_PASSAGE)
    # One publication carries both sources; no card operation: the card follows its markers once accepted.
    assert len(PUBLISHED) == before + 1 and len(OPERATIONS) == operations
    assert [m["path"] for m in PUBLISHED[-1]["mutations"]] == [LINKED_FROM, LINKED_TO]
    assert "proposal_decisions" not in PUBLISHED[-1]
    assert f"id: {base}.cut\n" in source_text(LINKED_FROM) and LINKED_PASSAGE not in source_text(LINKED_FROM)
    assert source_text(LINKED_TO).endswith(LINKED_PASSAGE + f"<!-- thoughts:agent-proposal:v2:end {base}.paste -->\nSecond.\n")
    text = moved["content"][0]["text"]
    assert f"Proposed moving card {LINKED_CARD} with its marked passage from {LINKED_FROM} to {LINKED_TO}" in text
    assert "deciding either proposal decides both" in text and f"{base}.paste at line 5 of {LINKED_TO}" in text
    replay = call(token, "move_review_card", **arguments)
    assert replay["structuredContent"]["replayed"] is True and len(PUBLISHED) == before + 1
    # A half cannot be revised; withdrawing either half withdraws both, in both sources.
    digest = current_sha256(token, LINKED_TO)
    revised = rpc(token, "tools/call", {"name": "revise_proposal", "arguments": dict(
        source=LINKED_TO, expected_source_sha256=digest, proposal_id=base + ".paste", replacement="Other.\n",
        explanation="Change.", conversation_id=MOVE_CONVERSATION, request_id="a6b6c5b7-1d2a-4c3b-9f8e-112233445566")})
    assert revised["isError"] and revised["structuredContent"]["error_code"] == "move_proposal"
    withdrawn = call(token, "remove_proposal", source=LINKED_TO, expected_source_sha256=digest,
                     proposal_id=base + ".paste", explanation="Not there.", conversation_id=MOVE_CONVERSATION,
                     request_id="a7b6c5b7-1d2a-4c3b-9f8e-112233445566")
    assert withdrawn["structuredContent"]["withdrawn_proposal_ids"] == [base + ".paste", base + ".cut"]
    assert f"the other half ({base}.cut in {LINKED_FROM}" in withdrawn["content"][0]["text"]
    assert (source_text(LINKED_FROM), source_text(LINKED_TO)) == (LINKED_NOTE, TARGET_NOTE)
    assert [(c["proposal_id"], c["action"], c["source_file"]) for c in PUBLISHED[-1]["proposal_decisions"]] == [
        (base + ".paste", "withdraw", LINKED_TO), (base + ".cut", "withdraw", LINKED_FROM)]


@pytest.mark.skipif(not PASSAGE_CLI, reason="thoughts-cli with move-card-passage not found")
def test_a_linked_card_move_needs_its_reason_the_server_and_the_cli(server, token, linked_note, monkeypatch):
    arguments = dict(card_id=LINKED_CARD, source=LINKED_TO, expected_source_sha256=linked_note, start_line=5, end_line=5,
                     request_id="a8b6c5b7-1d2a-4c3b-9f8e-112233445566")
    before = len(PUBLISHED)

    def refused(code, **extra):
        answer = rpc(token, "tools/call", {"name": "move_review_card", "arguments": {**arguments, **extra}})
        assert answer["isError"] and answer["structuredContent"]["error_code"] == code, answer
        assert len(PUBLISHED) == before
        return answer["content"][0]["text"]

    reason = dict(explanation="Merge the notes.", conversation_id=MOVE_CONVERSATION)
    assert "needs explanation and conversation_id" in refused("invalid_request")
    # With a line range the card is told how to move, not sent to the user.
    ranged = refused("linked_card", source=IDEAS_PATH, expected_source_sha256=current_sha256(token, IDEAS_PATH),
                     start_line=3, end_line=4)
    assert "start_line equal to end_line" in ranged and "cutting" not in ranged
    # A line past the end of the destination.
    refused("invalid_range", start_line=9, end_line=9, **reason)
    CAPABILITIES.remove("review_card_move_pairs_v1")
    server.thoughts.cached.pop("health", None)
    try:
        assert "review_card_move_pairs_v1" in refused("server_unsupported", **reason)
    finally:
        CAPABILITIES.append("review_card_move_pairs_v1")
        server.thoughts.cached.pop("health", None)
    from types import SimpleNamespace

    old_cli = SimpleNamespace(client=SimpleNamespace(COMMANDS=("create-review-card", "move-review-card")))
    monkeypatch.setattr(thoughts_connector, "load_thoughts_cli", lambda: old_cli)
    assert "thoughts-cli 1.7.0" in refused("not_supported", **reason)


def test_move_review_card_tells_agents_to_move_linked_cards_themselves(token):
    tools = {tool["name"]: tool for tool in rpc(token, "tools/list")["tools"]}
    description = tools["move_review_card"]["description"]
    assert "start_line equal to end_line" in description and "decides both" in description
    assert "the user moves\n        it in Thoughts by cutting" not in description
    assert "never ask the user to cut and paste it by hand" in " ".join(description.split())
    assert {"explanation", "conversation_id"} <= set(tools["move_review_card"]["inputSchema"]["properties"])
    assert not {"explanation", "conversation_id"} & set(tools["move_review_card"]["inputSchema"].get("required", []))


KIND_EVENT = {"entity_type": "review_event", "entity_id": "k1", "deleted": False, "payload": {
    "schema_version": 3, "event_id": "k1", "card_id": RETIRED, "queue_action": "set_review_kind", "review_kind": "project",
    "reviewed_at": "2026-11-17T10:00:00Z", "origin_id": "phone"}}


@pytest.mark.skipif(not (HAVE_CLI and (CLI / "thoughts_cli" / "review_kinds.py").is_file()),
                    reason="needs thoughts-cli with review_kinds (thoughts-cli 1.10.0)")
def test_review_cards_show_and_filter_the_kind(server, token):
    """thoughts-server protocol/review-card-kinds.md section 8."""
    every = call(token, "review_cards")["structuredContent"]["cards"]
    assert {(c["card_id"], c["review_kind"], c["review_kind_source"]) for c in every} == {
        (CARD, "knowledge", "project"), (RETIRED, "knowledge", "project"), (UNPUBLISHED, "knowledge", "project")}
    assert all("passage_changed" not in c for c in every)
    PROJECTIONS["projections"].append(KIND_EVENT)
    PROJECTS["projects"][0]["review_kind"] = "project"
    try:
        server.thoughts.cached.clear()
        server.thoughts.catalogue = None
        listed = call(token, "review_cards", kind="project")
        cards = {c["card_id"]: c for c in listed["structuredContent"]["cards"]}
        assert set(cards) == {CARD, RETIRED, UNPUBLISHED}
        assert (cards[RETIRED]["review_kind_source"], cards[CARD]["review_kind_source"]) == ("card", "project")
        assert cards[CARD]["kind_projects"] == ["Research"] and cards[CARD]["passage_changed"] is False
        assert ", project card (card" in listed["content"][0]["text"]
        assert call(token, "review_cards", kind="knowledge")["structuredContent"]["total"] == 0
        detail = call(token, "get_review_card", card_id=RETIRED)
        assert detail["structuredContent"]["review_kind"] == "project"
        assert {"action": "set_review_kind", "review_kind": "project", "reviewed_at": "2026-11-17T10:00:00Z"} in \
            detail["structuredContent"]["history"]
        assert "2026-11-17 set_review_kind project" in detail["content"][0]["text"]
        assert rpc(token, "tools/call", {"name": "review_cards", "arguments": {"kind": "draft"}})["isError"]
    finally:
        PROJECTIONS["projections"].remove(KIND_EVENT)
        del PROJECTS["projects"][0]["review_kind"]
        server.thoughts.cached.clear()
        server.thoughts.catalogue = None


def test_card_kind_fields_without_review_kinds(monkeypatch):
    monkeypatch.setattr(thoughts_connector, "review_kinds", lambda: None)
    assert thoughts_connector.card_kind_fields({"card_id": CARD}, [], []) == {}
    assert thoughts_connector.kind_text({}) == ""
    assert thoughts_connector.kind_text({"review_kind": "project", "passage_changed": True}) == \
        ", project card, passage changed since the last review"


def test_create_review_card_passes_the_kind(token):
    tools = {tool["name"]: tool for tool in rpc(token, "tools/list")["tools"]}
    schema = tools["create_review_card"]["inputSchema"]["properties"]["kind"]
    assert {"knowledge", "project"} <= set(json.dumps(schema).replace('"', " ").split())
    assert "kind" not in tools["create_review_card"]["inputSchema"].get("required", [])
    assert "kind" in tools["review_cards"]["inputSchema"]["properties"]
