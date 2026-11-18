#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The Thoughts connector: the Thoughts Library in ChatGPT/Claude (read) plus notes to the phone.

Runs on the Thoughts server host as the systemd --user unit `thoughts-connector` (port 8095 on
loopback) behind a public HTTPS reverse proxy at CONNECTOR_PUBLIC_URL/connector (sign-in and OAuth:
connector_core.py).
It talks to the Thoughts server on 127.0.0.1:8080 as its own device `connector` (token in
THOUGHTS_CONNECTOR_TOKEN, made with `thoughts-admin create-device connector`), so its notes and
reply feed are its own and it never shares a device sequence with the Mac or the phone.

A device token can do everything, so every request goes through ALLOWED: reading the Library
catalogue, sources, objects, projections and agent notes, creating agent notes, diary entries and
review-card conversation links, and publishing the sources into which thoughts-cli wrote pending
agent proposals (objects, source publications, the render check after them; never a decision), and
adding a new note to a Project (create_document: thoughts-cli publishes the new source, sends the
Library-project actions include_source and rename_document, check_request refusing every other
Library-project action, and publishes the new PDF's build inputs as the Mac does, reading the
folder's latest snapshots and checking objects with HEAD; when the PDF gets no inputs it
unpublishes the note, since a published note without them stops every Library build, which is the
only set_document_published allowed: published false, for a note create_document added), and
resolving source conflicts ("Merge needed") with a version or a combination of versions of the file,
and adding or replacing one interactive HTML page of a docs folder (add_html_page: thoughts-cli
add-html-page uploads it and changes the folder's page list, POST /v1/html-pages; never a removal),
moving an imported PDF to another Project (move_pdf: the Library-project action move_document),
creating a Project, with a docs folder of its own unless asked otherwise, and giving a Project one more
docs folder (create_project, add_source_folder: thoughts-cli create-project and add-source-folder, the
Library-project actions create and add_source_folder), moving a note of a docs folder to another Project
with its review cards, comments, figures, links and interactive pages (move_note: thoughts-cli
move-document, the actions plan_move_source_document and move_source_document, as the Mac's Move to
project sends them),
importing a PDF into a Project from a URL (import_pdf: POST /v1/library/url-imports; the server downloads
it, checks it and makes the Library's Import document change), merging review cards into one when the user
asks (merge_review_cards: thoughts-cli merge-review-cards, POST /v1/review-card-merges; the server deletes the
other cards with their markers and gives the kept card every passage, in one transaction, as Merge cards… on
the Mac does; cards of an imported PDF are merged without source fields and the kept card gets their areas),
moving a card onto a pending proposal's text (move_review_card: thoughts-cli asks the server to wait for the
proposal, POST /v1/review-card-pending-moves; the server moves the card when the user accepts it),
moving a card linked to source markers with its marked passage (move_review_card with start_line equal to
end_line: thoughts-cli move-card-passage writes two linked pending proposals in one source publication, as the
Mac's thoughtsctl move-card-passage does; the user decides them as one),
listing and cancelling such waiting moves (pending_card_moves, cancel_pending_card_move),
and reading why a PDF does not build (build_status: GET /v1/render-status, /v1/builds/<id> and its
source-diff, and the build log object).
Nothing else (no builds, uploaded imports, decisions, receipts or inbox polling).
After a 401 it stops for 10 minutes so a bad token cannot trip the server's per-address lockout for
other local callers.

Proposals are written by thoughts-cli (thoughts-server clients/thoughts-cli, the package thoughts_cli
copied next to this file by deploy.sh, or THOUGHTS_CLI): a headless sync client that reads the source,
writes the \\agentProposedEdit wrappers exactly as the desktop's thoughtsctl does and publishes the new
source as this device, like the Mac's source publisher. Its request records (request_id replays) are
in <state>/thoughts-cli.

Notes are rendered from Markdown (with KaTeX math) to a PNG by agent_notes.py from the Thoughts
plugin (copied next to this file by deploy.sh) with Chromium (THOUGHTS_CHROME). The renderer
measures every link of the note; send_note sends them as tappable rectangles of the picture
(links, when the server has agent_note_links_v1), checking Library PDF links against the Library
catalogue, as the Mac's send_note does against its PDF folder.

Commands: serve (default) | set-passphrase [--stdin] | revoke-all | status
"""
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

import connector_core as core
import knowledge_profile as knowledge

THOUGHTS_URL = os.environ.get("THOUGHTS_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN_FILE = Path(os.environ.get("THOUGHTS_CONNECTOR_TOKEN", Path.home() / "connectors" / "thoughts-token"))
DIARY_ZONE = ZoneInfo(os.environ.get("THOUGHTS_DIARY_ZONE", "Europe/Zurich"))
DIARY_AUTHOR = os.environ.get("THOUGHTS_DIARY_AUTHOR", "user")  # the author name on diary entries
AGENT_NOTES = Path(os.environ.get("THOUGHTS_AGENT_NOTES", Path(__file__).with_name("agent_notes.py")))
# The directory holding the thoughts_cli package (thoughts-server clients/thoughts-cli).
THOUGHTS_CLI = Path(os.environ.get("THOUGHTS_CLI", Path(__file__).resolve().parent))
SERVICE = core.Service(
    name="Thoughts", prefix="/connector", port=int(os.environ.get("THOUGHTS_CONNECTOR_PORT", "8095")),
    state=Path(os.environ.get("THOUGHTS_CONNECTOR_STATE", Path.home() / "connectors" / "state" / "thoughts")),
    what="your Thoughts Library and send notes to your phone")
CACHE_TTL = 30.0
PAUSE_AFTER_401 = 600.0
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# What the user lets the agent answering a reply use, chosen with each reply on the Mac or the phone
# (server agent_note_reply_sources_v1). A reply without it (older apps) allows everything, as before.
REPLY_SOURCES = {
    "none": "Use no Thoughts documents at all: do not search or read the Library, the knowledge profile (review cards) "
            "or the Mental Model, and give no Library links. Answer from the note, the reply and general knowledge.",
    "linked_pdf": "Use only the PDF this note is linked to (its document_id): read and cite that document alone, with page "
                  "links into it. No other Library document, no knowledge profile, no Mental Model. When the note has no "
                  "linked document, use no Thoughts documents.",
    "library": "Use any Library document (search, read, cite with page links) and the knowledge profile, but not the Mental Model.",
    "library_and_mental_model": "Use the Library, the knowledge profile and the Mental Model (mental_model), as the answering rules say.",
}
DEFAULT_REPLY_SOURCES = "library_and_mental_model"


def reply_sources(value) -> tuple[str, str]:
    """The reply's sources choice (the default when absent or unknown) and the rule it sets for the answer."""
    chosen = value if value in REPLY_SOURCES else DEFAULT_REPLY_SOURCES
    return chosen, REPLY_SOURCES[chosen]
ALLOWED = [(method, re.compile(f"^{pattern}$")) for method, pattern in (
    ("GET", r"/v1/library/projects"),
    ("GET", r"/v1/sources"),
    ("GET", r"/v1/objects/[0-9a-f]{64}"),
    ("GET", r"/v1/projections"),
    ("GET", r"/v1/agent-notes"),
    ("GET", f"/v1/agent-notes/{UUID}"),
    ("GET", f"/v1/agent-notes/{UUID}/image"),
    ("GET", r"/v1/agent-notes/replies"),
    ("GET", r"/v1/agent-notes/replies/[A-Za-z0-9_-]{1,128}/image"),
    # A picture the user sent with a reply (the phone's Send with picture, from the glasses camera).
    ("GET", r"/v1/agent-notes/replies/[A-Za-z0-9_-]{1,128}/photo"),
    ("POST", r"/v1/agent-notes"),
    ("GET", r"/v1/device-sequence"),
    # add_diary_entry, link_conversation and thoughts-cli's review cards (check_request).
    ("POST", r"/v1/operations"),
    ("GET", r"/v1/conflicts"),
    ("POST", f"/v1/conflicts/{UUID}/resolve"),
    # thoughts-cli (read_source, create_proposals, revise_proposal, remove_proposal); never a decision about the
    # user's text: the only decision claim it may send is the withdrawal of an agent proposal (check_request).
    ("GET", r"/v1/sources/[^?#]+"),
    ("POST", r"/v1/objects"),
    ("POST", r"/v1/source-publications"),
    ("POST", r"/v1/render-checks"),
    # thoughts-cli create-document: only LIBRARY_ACTIONS (check_request).
    ("POST", r"/v1/library/projects"),
    # thoughts-cli create-document: the new PDF's build inputs (never another PDF's; existing ones are kept).
    ("GET", r"/v1/build-input-snapshots/latest"),
    ("HEAD", r"/v1/objects/[0-9a-f]{64}"),
    ("POST", r"/v1/build-input-snapshots"),
    # thoughts-cli create-review-card and move-review-card: the server's capabilities.
    ("GET", r"/v1/health"),
    # html_pages and add_html_page (thoughts-cli add-html-page): a docs folder's HTML pages; add or replace only
    # (check_request), never a removal.
    ("GET", r"/v1/html-pages"),
    ("POST", r"/v1/html-pages"),
    # build_status (thoughts-cli build-status): a PDF's render status, its build and that build's comparison.
    ("GET", r"/v1/render-status"),
    ("GET", f"/v1/builds/{UUID}"),
    ("GET", f"/v1/builds/{UUID}/source-diff"),
    # import_pdf: the server downloads a PDF from a URL and imports it into a Project (library_import_pdf_v1).
    ("POST", r"/v1/library/url-imports"),
    # merge_review_cards (thoughts-cli merge-review-cards): the server merges cards into one (review_card_merge_v1).
    ("POST", r"/v1/review-card-merges"),
    # move_review_card onto a pending proposal's text: the move waits for the proposal (review_card_pending_moves_v1);
    # pending_card_moves, cancel_pending_card_move and get_review_card read or cancel such moves.
    ("POST", r"/v1/review-card-pending-moves"),
    ("GET", r"/v1/review-card-pending-moves"),
    ("GET", f"/v1/review-card-pending-moves/{UUID}"),
    ("POST", f"/v1/review-card-pending-moves/{UUID}/cancel"),
    # Who answers a reply or page (agent_note_claims_v1), topic owners (agent_note_owners_v1) and the user's answering
    # rules (agent_note_rules_v1): get_note claims for the calling conversation; own_topic, release_topic, note_rules.
    ("GET", f"/v1/agent-notes/handling/{UUID}"),
    ("POST", f"/v1/agent-notes/handling/{UUID}/claim"),
    ("GET", r"/v1/agent-notes/owners"),
    ("POST", r"/v1/agent-notes/owners"),
    ("DELETE", r"/v1/agent-notes/owners/[0-9a-f]{24}"),
    ("DELETE", r"/v1/agent-notes/owners"),
    ("GET", r"/v1/agent-notes/rules"),
    ("GET", r"/v1/mental-model"),
)]
# Who answers (server capabilities); the reply agent's own MCP server sets the environment defaults below.
CLAIMS_CAPABILITY = "agent_note_claims_v1"
OWNERS_CAPABILITY = "agent_note_owners_v1"
RULES_CAPABILITY = "agent_note_rules_v1"
# The user's Mental Model, the Library note mental-model.md (GET /v1/mental-model).
MENTAL_MODEL_CAPABILITY = "mental_model_v1"
# The conversation this connector process answers for when a tool names none (the reply agent's session).
DEFAULT_CONVERSATION = os.environ.get("THOUGHTS_AGENT_CONVERSATION", "").strip()
# Extra origin fields for every note this process sends (the reply agent marks its notes as the responder's).
try:
    NOTE_ORIGIN = json.loads(os.environ.get("THOUGHTS_NOTE_ORIGIN") or "{}")
except ValueError:
    NOTE_ORIGIN = {}
NOTE_ORIGIN = NOTE_ORIGIN if isinstance(NOTE_ORIGIN, dict) else {}
# The reply agent has the rules in its prompt already: tools then name them instead of repeating them.
RULES_IN_PROMPT = os.environ.get("THOUGHTS_RULES_IN_PROMPT") == "1"
# The server capability import_pdf needs, and how long its download and import may take.
IMPORT_CAPABILITY = "library_import_pdf_v1"
IMPORT_SECONDS = 240
# The server capability merge_review_cards needs, and the one for cards of an imported PDF (no source fields; the
# thoughts-cli that sends such a request has card_merges.is_pdf_merge, thoughts-cli 1.8.0).
MERGE_CAPABILITY = "review_card_merge_v1"
MERGE_PDF_CAPABILITY = "review_card_merge_pdf_v1"
# The thoughts-cli command that moves a card linked to source markers with its passage (thoughts-cli 1.7.0), and
# the server capability it needs: the server decides both halves of the move together.
PASSAGE_MOVE_COMMAND = "move-card-passage"
PASSAGE_MOVE_CAPABILITY = "review_card_move_pairs_v1"
# The server capability for card moves that wait for a pending proposal (move_review_card, pending_card_moves).
PENDING_MOVES_CAPABILITY = "review_card_pending_moves_v1"
# Notes may carry links: rectangles of the picture that open a web page or a Library PDF page on the phone
# (thoughts-server protocol/agent-notes.md, "Links"). The limits and forms are the server's.
LINKS_CAPABILITY = "agent_note_links_v1"
NOTE_WIDTH = 480  # CSS px of a rendered note (agent_notes.PAGE_WIDTH)
MAX_NOTE_LINKS = 64
MAX_LINK_HREF = 2048
WEB_LINK = re.compile(r"https?://[^\s/?#]+", re.I)
PDF_LINK = re.compile(r"thoughts-pdf-link:([A-Za-z0-9][A-Za-z0-9_.-]*\.pdf)\?page=[0-9]{1,6}"
                      r"(?:&x=-?[0-9]+(?:\.[0-9]+)?)?(?:&y=-?[0-9]+(?:\.[0-9]+)?)?")
# A Library PDF by filename, as Thoughts links write it (optionally behind ../docs-<folder>/), with a page as printed.
LIBRARY_PDF_HREF = re.compile(r"(?:\.\./)*(?:[^/?#]+/)*([A-Za-z0-9][A-Za-z0-9_.-]*\.pdf)(?:#page=([0-9]{1,6}))?", re.I)
OWN_PAGE_HREF = re.compile(r"#page=([0-9]{1,6})", re.I)
# Operations the connector itself sends: add_diary_entry and link_conversation.
OWN_OPERATIONS = frozenset({"logical_comment", "agent_conversation"})
# create_document's three, move_pdf's move_document (an imported PDF to another Project), create_project's
# create, add_source_folder's own, and move_note's plan and move (a note of a docs folder to another Project).
LIBRARY_ACTIONS = frozenset({"include_source", "rename_document", "set_document_published", "move_document",
                             "create", "add_source_folder", "plan_move_source_document", "move_source_document"})
MARKER = re.compile(r"(?:<!--\s*)?\\?ReviewCardMask(?:Start|End)\{[^}]*\}\{[^}]*\}%?(?:\s*-->)?")
INPUT = re.compile(r"\\(?:input|include|subfile)\{([^}]+)\}")
FOLDER = re.compile(r"docs-[A-Za-z0-9_.-]+")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-but-readable PDFs warn on every read


class ThoughtsError(RuntimeError):
    pass


# The rules thoughts-cli enforces before it writes (thoughts_cli guards.py, pdf_links.py), for the tool descriptions.
PROPOSAL_RULES = """
        Links to Library PDFs: \\PdfLink{pdfKey}{anchor}{text} opens the imported PDF whose Library filename is
        pdfKey.pdf in ANY Project (Markdown: [text](pdfKey.pdf#anchor)); anchor is document-start or a doc-anchor:
        name. Every note, also one in a Project's default folder, has \\PdfLink: never \\providecommand it. An old
        \\CrossPdfLink{docs-<folder>/pdfKey}{anchor}{text} still works after the PDF moved. A PDF no Project has is
        refused (invalid_link, with the closest filename in links[].suggestion).
        Refused before anything is written, with the fix in the answer: proposal_in_argument (lines inside a
        key=value argument such as \\hypersetup{...}, \\geometry{...}, package or TikZ options, or inside a [ ]
        optional argument such as \\twocolumn[...]: select the whole command, suggested_start_line and
        suggested_end_line); invalid_include_path; review_card_latex_unsupported (\\hyperref inside a review card's
        answer: use \\hyperlink{label}{text} and \\hypertarget{label}{}); in Markdown unsupported_character (emoji)
        and markdown_math_lt (a literal < in math: write \\lt). \\includegraphics{../assets/x} is written as
        tex/assets/x (warning include_path_rewritten). To repair a brace the file never closes (or closes twice),
        select from the line that opens the group through where it should close and give the whole corrected text:
        accepted although the selected original does not balance (warning original_braces_balanced). When a PDF does
        not build, call build_status first."""


def with_rules(tool):
    """Append PROPOSAL_RULES to a tool's description (its docstring), below its own text."""
    tool.__doc__ = (tool.__doc__ or "").rstrip() + "\n" + PROPOSAL_RULES
    return tool


def check_request(method: str, route: str, body=None, created: frozenset[str] | set[str] = frozenset()) -> None:
    """Refuse a request ALLOWED does not list, and every Library-project change but LIBRARY_ACTIONS.

    set_document_published only unpublishes a note create_document added (``created``: its document ids)."""
    if not any(method == m and pattern.match(route) for m, pattern in ALLOWED):
        raise ThoughtsError(f"{method} {route} is not allowed for the connector")
    if (method, route) == ("POST", "/v1/operations"):
        operations = body.get("operations") if isinstance(body, dict) else None
        if not isinstance(operations, list) or not all(isinstance(op, dict) and allowed_operation(op) for op in operations):
            raise ThoughtsError("the connector may only add diary entries, link conversations, and create or move review "
                                "cards with their source markers; the user reviews, retires and deletes cards in Thoughts")
        return
    if (method, route) == ("POST", "/v1/source-publications"):
        claims = body.get("proposal_decisions") if isinstance(body, dict) else None
        if claims is not None and (not isinstance(claims, list) or any(
                not isinstance(claim, dict) or claim.get("action") != "withdraw" for claim in claims)):
            raise ThoughtsError("the connector may only withdraw agent proposals; the user accepts or rejects them in Thoughts")
        return
    if (method, route) == ("POST", "/v1/html-pages"):
        changes = body.get("changes") if isinstance(body, dict) else None
        if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict) \
                or not isinstance(changes[0].get("sha256"), str):
            raise ThoughtsError("the connector may only add or replace one HTML page at a time, never remove one")
        return
    if (method, route) != ("POST", "/v1/library/projects"):
        return
    action = body.get("action") if isinstance(body, dict) else None
    if action not in LIBRARY_ACTIONS:
        raise ThoughtsError(f"the Library-project action {action!r} is not allowed for the connector "
                            "(only include_source and rename_document, to add a new note, move_document, to "
                            "move an imported PDF, create and add_source_folder, to make a Project or give it a "
                            "folder, and plan_move_source_document and move_source_document, to move a note)")
    if action == "set_document_published" and (body.get("published") is not False
                                                or body.get("document_id") not in created):
        raise ThoughtsError("set_document_published is not allowed for the connector, except to unpublish a note "
                            "create_document added without build inputs")


def allowed_operation(operation: dict) -> bool:
    """One of the connector's own operations, or a card thoughts-cli creates or moves together with its
    source markers (create-review-card, move-review-card): never a review, retirement or deletion."""
    if operation.get("entity_type") in OWN_OPERATIONS:
        return True
    payload = operation.get("payload")
    return (operation.get("entity_type") == "review_card" and isinstance(payload, dict)
            and payload.get("source_authority") == "client-v1" and isinstance(payload.get("source_change"), dict)
            and payload.get("status") == "approved" and "delete_source" not in payload)


def created_documents(state: Path) -> set[str]:
    """The notes create_document added: the document ids of thoughts-cli's create-document records."""
    try:
        records = json.loads((state / "thoughts-cli" / "requests.json").read_text(encoding="utf-8"))["requests"]
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    return {record["document_id"] for record in records.values() if isinstance(record, dict)
            and record.get("command") == "create-document" and isinstance(record.get("document_id"), str)}


UUID_FIELD = f"^{UUID}$"


class ProposalFigure(BaseModel):
    """An SVG drawing a proposal adds (thoughts-server protocol/agent-proposals.md section 11)."""
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$", description="File name without .svg; the figure becomes agent-figures/<name>.svg next to the note.")]
    svg: Annotated[str, Field(min_length=1, max_length=1048576, description="A complete self-contained SVG document (root <svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=...>); no scripts, event handlers, external links, images by URL or @import; data:image/ URIs are fine.")]


FIGURES_FIELD = ("SVG drawings this proposal adds. Each is published with the proposal as agent-figures/<name>.svg in "
                 "the note's folder, and the replacement must reference it: LaTeX "
                 "\\includegraphics[width=\\linewidth]{agent-figures/<name>} (no extension; Thoughts converts it to PDF), "
                 "Markdown ![description](agent-figures/<name>.svg). It shows in the PDF while pending; Reject removes it.")


class ProposalEdit(BaseModel):
    """One edit of create_proposals (thoughtsctl create-proposals)."""
    model_config = ConfigDict(extra="forbid")
    start_line: Annotated[int, Field(ge=1, le=2147483647, description="One-based first line of the selection, in the version read.")]
    end_line: Annotated[int, Field(ge=1, le=2147483647, description="One-based exclusive end line; equal to start_line inserts before that line.")]
    replacement: Annotated[str, Field(max_length=1000000, description="Proposed source text for the selection, without any proposal wrapper; empty deletes it.")]
    explanation: Annotated[str, Field(min_length=1, max_length=100000, description="Plain-text reason why the change is needed.")]
    figures: Annotated[list[ProposalFigure] | None, Field(default=None, max_length=20, description=FIGURES_FIELD)] = None


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def snippet(text: str, words: list[str], width: int = 240) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    at = max(0, fold(flat).find(words[0]) - width // 3) if words else 0
    return ("…" if at else "") + flat[at:at + width] + ("…" if at + width < len(flat) else "")


def clean(text: str) -> str:
    """Source text without the review-card marker commands (they only delimit card answers)."""
    return MARKER.sub("", text)


class Thoughts:
    """The Thoughts server, through ALLOWED only, with small caches (the objects are immutable)."""

    def __init__(self, http, url: str = THOUGHTS_URL, token_file: Path = TOKEN_FILE, state: Path = SERVICE.state):
        self.http, self.url, self.token_file = http, url, token_file
        self.paused_until = 0.0
        self.catalogue: tuple[str, dict] | None = None
        self.cached: dict[str, tuple[float, object]] = {}
        self.objects: dict[str, bytes] = {}
        self.pdf_dir = state / "pdftext"

    async def request(self, method: str, path: str, *, params=None, body=None, content=None, headers=None, timeout=60,
                      refusals=False):
        """The response; a 4xx/5xx raises ThoughtsError, except with refusals one whose JSON names an error_code."""
        check_request(method, path, body)
        if time.time() < self.paused_until:
            raise ThoughtsError("the Thoughts server refused the connector's token; paused for a few minutes")
        token = self.token_file.read_text().strip()
        response = await self.http.request(method, self.url + path, params=params, json=body, content=content, timeout=timeout,
                                           headers={"Authorization": "Bearer " + token, **(headers or {})})
        if response.status_code == 401:
            self.paused_until = time.time() + PAUSE_AFTER_401
            core.log("thoughts: 401 for the connector's token; pausing requests for 10 minutes")
            raise ThoughtsError("the Thoughts server refused the connector's token")
        if response.status_code >= 400 and response.status_code != 304:
            try:
                answer = response.json()
                detail = answer.get("detail")
            except (ValueError, AttributeError):
                answer, detail = None, response.text[:300]
            if refusals and isinstance(answer, dict) and answer.get("error_code"):
                return response
            raise ThoughtsError(f"Thoughts server: {response.status_code} {detail}")
        return response

    async def ttl(self, key: str, path: str):
        hit = self.cached.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        value = (await self.request("GET", path)).json()
        self.cached[key] = (time.monotonic(), value)
        return value

    async def projects(self) -> list[dict]:
        headers = {"If-None-Match": self.catalogue[0]} if self.catalogue else {}
        response = await self.request("GET", "/v1/library/projects", headers=headers)
        if response.status_code != 304 or not self.catalogue:
            self.catalogue = (response.headers.get("etag", ""), response.json())
        return self.catalogue[1]["projects"]

    async def documents(self) -> dict[str, dict]:
        """document_id -> {document_id, name, project, kind (tex|md|pdf), source_file, base, pdf_sha256, pages, published,
        filename (the Library filename of its PDF, which links name)}.

        A document published in any Project is published; one only some Project lists in
        unpublished_documents is not (protocol/project-document-publication.md)."""
        out = {}
        projects = await self.projects()
        for field, published in (("documents", True), ("unpublished_documents", False)):
            for project in projects:
                for d in project.get(field, []):
                    source = d.get("source_file") or ""
                    kind = "tex" if source.endswith(".tex") else "md" if source.endswith((".md", ".markdown")) else "pdf"
                    out.setdefault(d["document_id"], {
                        "document_id": d["document_id"], "name": d.get("name") or d.get("filename"), "project": project["name"],
                        "kind": kind, "source_file": source or None, "base": d.get("source_project") or "",
                        "pdf_sha256": d.get("sha256"), "pages": d.get("page_count"), "included": published,
                        "published": published, "filename": d.get("filename") or d.get("attachment_filename")})
        return out

    async def sources(self) -> dict[str, dict]:
        listing = await self.ttl("sources", "/v1/sources")
        return {s["path"]: s for s in listing["sources"] if not s["deleted"]}

    async def object(self, sha256: str) -> bytes:
        if sha256 not in self.objects:
            data = (await self.request("GET", f"/v1/objects/{sha256}", timeout=120)).content
            if len(data) > 4 * 1024 * 1024:
                return data  # PDFs: not kept in memory
            if sum(map(len, self.objects.values())) > 128 * 1024 * 1024:
                self.objects.clear()
            self.objects[sha256] = data
        return self.objects[sha256]

    async def fresh_sources(self) -> dict[str, dict]:
        """The source listing now, not from the cache: proposals need the current SHA-256."""
        self.cached.pop("sources", None)
        return await self.sources()

    async def source(self, path: str) -> str | None:
        record = (await self.sources()).get(path)
        if record is None:
            return None
        return (await self.object(record["object_sha256"])).decode("utf-8", "replace")

    async def document_text(self, doc: dict) -> list[str]:
        """The document as lines: its source with \\input files inlined (tex), or its PDF's pages' text."""
        if doc["kind"] == "pdf":
            return [f"--- page {n} ---\n{text}" for n, text in enumerate(await self.pdf_pages(doc["pdf_sha256"]), 1)]
        text = await self.source(doc["source_file"]) or ""
        if doc["kind"] == "tex":
            text = await self.inline_inputs(text, doc, depth=0)
        return clean(text).splitlines()

    async def inline_inputs(self, text: str, doc: dict, depth: int) -> str:
        if depth > 4:
            return text
        sources = await self.sources()
        folder = doc["source_file"].split("/tex/")[0] if "/tex/" in doc["source_file"] else doc["base"]
        out, last = [], 0
        for match in INPUT.finditer(text):
            name = match.group(1).strip()
            candidates = [f"{base}/{name}{ext}" for base in (folder, doc["base"]) if base for ext in ("", ".tex")]
            path = next((c for c in candidates if c in sources), None)
            if path is None:
                continue
            inner = await self.inline_inputs(await self.source(path) or "", doc, depth + 1)
            out.append(text[last:match.start()] + f"% ---- {name} ----\n" + inner)
            last = match.end()
        return "".join(out) + text[last:]

    async def pdf_pages(self, sha256: str) -> list[str]:
        cache = self.pdf_dir / f"{sha256}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        data = await self.object(sha256)

        def extract() -> list[str]:
            from pypdf import PdfReader
            return [(page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(data)).pages]
        pages = await asyncio.to_thread(extract)
        core.write_atomic(cache, json.dumps(pages).encode())
        return pages

    async def review(self) -> tuple[dict, dict]:
        """(card_id -> card payload, card_id -> [event payloads, oldest first]) from the projections."""
        projections = (await self.ttl("projections", "/v1/projections"))["projections"]
        cards = {p["entity_id"]: p["payload"] for p in projections if p["entity_type"] == "review_card" and not p["deleted"]}
        events: dict[str, list] = {}
        for p in projections:
            if p["entity_type"] == "review_event" and not p["deleted"]:
                events.setdefault(p["payload"].get("card_id"), []).append(p["payload"])
        for history in events.values():
            history.sort(key=lambda e: e.get("reviewed_at") or e.get("updated_at") or "")
        return cards, events

    async def document_feedback(self) -> dict[str, list]:
        """document_id -> [document_feedback payloads, oldest first] from the projections (non-deleted only).

        Liked/disliked passages of a document's PDF snapshot (thoughts-server protocol/document-feedback.md)."""
        projections = (await self.ttl("projections", "/v1/projections"))["projections"]
        out: dict[str, list] = {}
        for p in projections:
            payload = p.get("payload")
            if p["entity_type"] != "document_feedback" or p["deleted"] or not isinstance(payload, dict) or payload.get("deleted"):
                continue
            pdf = (payload.get("anchor") or {}).get("pdf") or {}
            if pdf.get("document_id"):
                out.setdefault(pdf["document_id"], []).append(payload)
        for records in out.values():
            records.sort(key=lambda f: (created_order(f.get("created_at")), str(f.get("feedback_id") or "")))
        return out

    async def card_answer(self, card: dict) -> str | None:
        """The text between the card's markers in its sources, or the PDF text it selected."""
        primary = card.get("primary") or {}
        anchor = primary.get("source_anchor") or {}
        parts = []
        for segment in anchor.get("segments") or []:
            text = await self.source(segment.get("source_file", ""))
            if not text:
                continue
            ident = re.escape(card.get("card_id", "")) + r"\}\{" + re.escape(str(segment.get("segment_id", "0")))
            match = re.search(r"ReviewCardMaskStart\{" + ident + r"\}%?(.*?)(?:<!--\s*)?\\?ReviewCardMaskEnd\{" + ident + r"\}",
                              text, re.S)
            if match:
                parts.append(clean(match.group(1)).strip())
        if parts:
            return "\n\n".join(parts)
        if (primary.get("selected_text") or "").strip():
            return primary["selected_text"].strip()
        regions = primary.get("regions") or ([{"page": primary.get("page"), "rect": primary.get("rect")}] if primary.get("rect") else [])
        if primary.get("pdf_sha256") and regions:
            try:
                return await self.pdf_region_text(primary["pdf_sha256"], regions) or None
            except Exception as error:  # noqa: BLE001  (an unreadable PDF only loses the answer)
                core.log(f"card {card.get('card_id')}: no region text ({type(error).__name__}: {error})")
        return None

    async def pdf_region_text(self, sha256: str, regions: list[dict]) -> str:
        """The PDF text inside the card's regions (page numbers from 1, rects as page fractions from the top left)."""
        data = await self.object(sha256)

        def extract() -> str:
            from pypdf import PdfReader
            reader, out = PdfReader(io.BytesIO(data)), []
            for region in regions:
                page = reader.pages[int(region["page"]) - 1]
                box = page.mediabox
                width, height = float(box.width), float(box.height)
                x0, y0, x1, y1 = region["rect"]
                left, right = float(box.left) + x0 * width - 2, float(box.left) + x1 * width + 2
                top, bottom = float(box.bottom) + (1 - y0) * height + 2, float(box.bottom) + (1 - y1) * height - 2
                pieces = []

                def visit(text, cm, tm, font, size):
                    x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
                    y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
                    if text and left <= x <= right and bottom <= y <= top:
                        pieces.append(text)
                page.extract_text(visitor_text=visit)
                out.append("".join(pieces).strip())
            return "\n".join(part for part in out if part)
        return await asyncio.to_thread(extract)


def load_thoughts_cli():
    """The thoughts_cli package (thoughts-server clients/thoughts-cli) from THOUGHTS_CLI."""
    if "thoughts_cli" not in sys.modules:
        if not (THOUGHTS_CLI / "thoughts_cli" / "__init__.py").is_file():
            raise ThoughtsError(f"thoughts-cli is not installed (no thoughts_cli package in {THOUGHTS_CLI})")
        sys.path.insert(0, str(THOUGHTS_CLI))
    import thoughts_cli
    import thoughts_cli.api
    return thoughts_cli


def reply_photo_captures(note: dict) -> list[str]:
    """The glasses captures (photo.capture) of a note's replies, oldest first."""
    captures = []
    for reply in note.get("replies") or []:
        photo = reply.get("photo") if isinstance(reply, dict) else None
        if isinstance(photo, dict) and isinstance(photo.get("capture"), str) and photo["capture"]:
            captures.append(photo["capture"])
    return captures


def load_agent_notes():
    """agent_notes.py of the Thoughts plugin (render + check_image), loaded from AGENT_NOTES."""
    spec = importlib.util.spec_from_file_location("agent_notes", AGENT_NOTES)
    module = importlib.util.module_from_spec(spec)
    # dataclasses (Python 3.14) look the defining module up in sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def note_link_target(href: str, own_pdf: str | None, library: dict[str, str] | None) -> tuple[str | None, str | None]:
    """What the phone opens for a link as the agent wrote it, and a warning when it is dropped or doubtful.

    As the Mac's agent_notes.resolve_link: https/http stay as they are; a Library PDF, written <filename>.pdf#page=N
    (N as printed, 1-based) or #page=N for the note's own document (own_pdf), becomes
    thoughts-pdf-link:<filename>.pdf?page=N-1. The filename is looked up in the Library as the server lists it
    (library: casefolded filename -> filename; None when the catalogue could not be read), and written as the
    Library writes it. A link the server would refuse is dropped, so it never costs the whole note."""
    href = (href or "").strip()
    if WEB_LINK.match(href):
        return (href, None) if len(href) <= MAX_LINK_HREF else (
            None, f"Link {href[:60]}… is longer than {MAX_LINK_HREF} characters; dropped.")
    if href.lower().startswith("thoughts-pdf-link:"):
        match = PDF_LINK.fullmatch(href)
        if not match:
            return None, f"Link {href[:200]} is not thoughts-pdf-link:<file>.pdf?page=<n>; dropped."
        filename, rest = match.group(1), href[match.end(1):]
    else:
        match = OWN_PAGE_HREF.fullmatch(href)
        if match:
            if not own_pdf:
                return None, (f"Link {href} opens a page of the note's own document, but the note has none whose PDF is "
                              "known (give document_id); dropped.")
            filename, page = own_pdf, match.group(1)
        else:
            match = LIBRARY_PDF_HREF.fullmatch(href)
            if not match:
                return None, (f"Link {href[:200]} is neither a web address (https://…) nor a Library PDF "
                              "(<Library filename>.pdf#page=N); dropped.")
            filename, page = match.group(1), match.group(2)
        rest = f"?page={max(0, int(page or 1) - 1)}"
    known = library.get(filename.casefold()) if library is not None else None
    target = f"thoughts-pdf-link:{known or filename}{rest}"
    if not PDF_LINK.fullmatch(target):
        return None, f"Link {href[:200]}: {filename} is not a Library PDF filename; dropped."
    if library is not None and known is None:
        return target, (f"Link {href[:200]}: no Library PDF is named {filename} (list_documents shows each document's "
                        "filename); the phone shows it as unavailable.")
    return target, None


def note_links(measured: list, css_height: int, own_pdf: str | None = None,
               library: dict[str, str] | None = None) -> tuple[list, list]:
    """The note's links in the server's form, made as the Mac's agent_notes.note_links makes them: each measured
    rectangle ((href, [x, y, width, height] in CSS px of the page, linked text), from the renderer) as fractions of
    the picture, with what the phone opens (note_link_target); and warnings."""
    links, warnings, warned = [], [], set()
    width, height = float(NOTE_WIDTH), float(max(1, css_height))
    for href, (x, y, w, h), label in measured:
        target, warning = note_link_target(href, own_pdf, library)
        if warning and href not in warned:
            warnings.append(warning)
            warned.add(href)
        if target is None:
            continue
        left, top = max(0.0, x), max(0.0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        if right - left < 1 or bottom - top < 1:
            continue  # cut off by the page's width or height
        link = {"rect": [round(left / width, 6), round(top / height, 6), round((right - left) / width, 6),
                         round((bottom - top) / height, 6)], "href": target}
        if label:
            link["label"] = label[:200]
        links.append(link)
    if len(links) > MAX_NOTE_LINKS:
        warnings.append(f"The note has {len(links)} link rectangles; only the first {MAX_NOTE_LINKS} are kept.")
        links = links[:MAX_NOTE_LINKS]
    return links, warnings


def review_kinds():
    """thoughts_cli.review_kinds (thoughts-server protocol/review-card-kinds.md), or None with an older thoughts-cli."""
    try:
        load_thoughts_cli()
        import thoughts_cli.review_kinds as kinds
    except (ImportError, ThoughtsError):
        return None
    return kinds


def card_kind_fields(card: dict, history: list, projects: list | None) -> dict:
    """review_kind, review_kind_source, kind_projects and, for a project card, passage_changed."""
    kinds = review_kinds() if projects is not None else None
    if kinds is None:
        return {}
    kind = kinds.card_kind(card, history, projects)
    out = {"review_kind": kind.kind, "review_kind_source": kind.source}
    names = [p.get("name") for p in projects if p.get("id") in kind.project_ids and p.get("name")]
    if names:
        out["kind_projects"] = names
    if kind.is_project:
        out["passage_changed"] = kinds.passage_changed(card, history)
    return out


def kind_text(summary: dict) -> str:
    """", project card" (", changed since last review") for a listing line; nothing for a knowledge card."""
    if summary.get("review_kind") != "project":
        return ""
    return ", project card" + (", passage changed since the last review" if summary.get("passage_changed") else "")


def card_summary(card: dict, history: list, docs: dict, projects: list | None = None) -> dict:
    primary = card.get("primary") or {}
    doc = docs.get(primary.get("document_id") or "")
    # The card's Project document: its PDF's for a card made in an attachment.
    owner = docs.get(primary.get("pdf_document_id") or "") or doc
    answered = [e for e in history if e.get("rating") not in (None, "delete") and not e.get("retracted_event_id")]
    last = answered[-1] if answered else None
    out = {"card_id": card.get("card_id"), "question": primary.get("question"), "document_id": primary.get("document_id"),
           "document": doc["name"] if doc else None, "page": primary.get("page"), "created_at": card.get("created_at"),
           "reviews": len(answered)}
    retired = retired_at(history)
    # A card of an unpublished document is kept but not offered for review.
    out["status"] = "retired" if retired else "unpublished" if owner and owner.get("published") is False else "active"
    if retired:
        out["retired_at"] = retired
    if last:
        out.update(last_reviewed_at=last.get("reviewed_at"), last_rating=last.get("rating"),
                   last_tags=last.get("review_tags") or [])
        notes = {k: v for k, v in (last.get("review_group_notes") or {}).items() if v}
        if notes:
            out["last_notes"] = notes
    out.update(card_kind_fields(card, history, projects))
    return out


def waiting_text(body: dict) -> str:
    """move_review_card's answer for a move that waits for a pending proposal."""
    lines = [f"Card {body['card_id']} waits for the user to accept proposal {body['proposal_id']} in {body['source']}"
             + (" (replayed: already recorded by this request_id)" if body.get("replayed") else "")
             + f". Nothing has changed yet. When the user accepts it, Thoughts moves the card from "
             f"{body['from_document']} into {body['document']} onto these lines, with its history; if the proposal is "
             f"rejected or withdrawn, the card stays on {body['from_document']}. pending_move_id {body['pending_move_id']}."]
    current = body.get("pending_move") or {}
    if current and current.get("state") != "waiting_for_proposal":
        lines.append(f"Now: {current['state']}" + (f" ({current['reason']})" if current.get("reason") else "")
                     + (f": {current['message']}" if current.get("message") else "") + ".")
    return "\n".join(lines)


def move_proposed_text(body: dict) -> str:
    """move_review_card's answer for a card linked to source markers: two linked pending proposals."""
    halves = {half["role"]: half for half in body["proposals"]}
    cut, paste = halves["cut"], halves["paste"]
    lines = [f"Proposed moving card {body['card_id']} with its marked passage from {cut['source']} to {paste['source']}"
             + (" (replayed: already written by this request_id)" if body.get("replayed") else "")
             + ". Nothing has moved yet: the user accepts or rejects the move in Thoughts, and deciding either "
             "proposal decides both.",
             f"- {cut['proposal_id']} at line {cut['line']} of {cut['source']} removes the passage "
             f"(new source_sha256 {body['origin_source_sha256']})",
             f"- {paste['proposal_id']} at line {paste['line']} of {paste['source']} inserts it "
             f"(new source_sha256 {body['source_sha256']})",
             "remove_proposal on either one withdraws both; they cannot be revised. Accepted, the card keeps its ID, "
             "schedule and history and follows its markers when the notes are next built."]
    if body.get("also_moved_card_ids"):
        lines.append("Cards inside the passage that move with it: " + ", ".join(body["also_moved_card_ids"]) + ".")
    lines += [f"Warning: {w['message']}" for w in body.get("warnings") or []]
    if (body.get("render_check") or {}).get("status") == "failed":
        lines.append(f"Warning: the rebuild could not be requested: {body['render_check'].get('message')}")
    return "\n".join(lines)


def pending_move_line(move: dict) -> str:
    """One pending card move, for pending_card_moves and get_review_card."""
    head = (f"- card {move.get('card_id')} into {move.get('document')} (from {move.get('from_document')}), "
            f"proposal {move.get('proposal_id')} in {move.get('source')}: ")
    if move.get("state") == "waiting_for_proposal":
        return head + f"waiting for the user to accept it (pending_move_id {move.get('pending_move_id')})"
    return head + str(move.get("state")) + (f" ({move['reason']})" if move.get("reason") else "") + (
        f", {move['settled_at'][:10]}" if move.get("settled_at") else "")


def deleted(history: list) -> bool:
    return any(e.get("rating") == "delete" for e in history)


def _when(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def created_order(value) -> tuple:
    """Sort key for RFC 3339 timestamps with any offset; unparsable ones last, by their text."""
    when = _when(value)
    return (0, when.timestamp(), "") if when else (1, 0.0, str(value or ""))


def feedback_summary(feedback: dict, current_pdf_sha256: str | None) -> dict:
    """One document_feedback record as the document_feedback tool shows it (protocol/document-feedback.md 6).

    snapshot is "current" when the feedback was given on the document's current PDF, "earlier" otherwise,
    and None when the current PDF is not known (the document is not in the Library catalogue). Feedback made
    in an HTML attachment (section 8) has no page: html_attachment names the page and its state instead."""
    anchor = feedback.get("anchor") or {}
    pdf = anchor.get("pdf") or {}
    regions = pdf.get("regions") or []
    sha = pdf.get("pdf_sha256")
    summary = {"feedback_id": feedback.get("feedback_id"), "polarity": feedback.get("polarity"),
               "quote": (anchor.get("quote") or {}).get("exact"), "comment": feedback.get("comment") or "",
               "page": regions[0].get("page") if regions and isinstance(regions[0], dict) else None,
               "pdf_sha256": sha,
               "snapshot": None if not current_pdf_sha256 else "current" if sha == current_pdf_sha256 else "earlier",
               "created_at": feedback.get("created_at"), "updated_at": feedback.get("updated_at"),
               "origin": feedback.get("origin_id")}
    # A question or another comment (section 9): it stays until resolved_at, whatever the version.
    # A drawing (section 10) is shown as one on its page: its points are of no use to read. A text box
    # (section 11) is its text, the comment, on its page, without its place.
    if feedback.get("kind"):
        summary["kind"] = feedback["kind"]
        summary["resolved_at"] = feedback.get("resolved_at")
    if feedback.get("view") == "html_attachment":
        summary["html_attachment"] = {"path": (anchor.get("html_attachment") or {}).get("path"),
                                      "page_state": anchor.get("page_state") or {}}
    return summary


def feedback_line(record: dict) -> str:
    """One record of the document_feedback tool's text."""
    place = f"HTML page {record['html_attachment']['path']}" if record.get("html_attachment") else f"p. {record['page']}"
    what = {"question": "Question", "other": "Comment", "drawing": "Drawing", "text": "Text box"}.get(record.get("kind")) or ("Liked" if record["polarity"] == "positive" else "Disliked")
    if record.get("resolved_at"):
        what += " (resolved)"
    start = f"- {what}, {place}" + (f" ({record['snapshot']} PDF)" if record["snapshot"] else "") + f", {str(record['created_at'] or '')[:10]}"
    # A drawing has no passage to quote, and a text box is its text.
    if record.get("kind") == "drawing":
        return start + (f": {record['comment']}" if record["comment"] else "")
    if record.get("kind") == "text":
        return start + f": \u201c{record['comment']}\u201d"
    return start + f": \u201c{record['quote']}\u201d" + (f" \u2014 {record['comment']}" if record["comment"] else "")


# The versions of a source conflict, by read_source_conflict's names for them.
CONFLICT_VERSIONS = {"merged": "merged_object_sha256", "server": "current_sha256", "computer": "incoming_sha256", "base": "base_sha256"}
MERGE_MARKER = re.compile(r"^(<<<<<<<|=======|>>>>>>>)( |$)")
PROPOSAL_ID = re.compile(r"\\agentProposedEdit(?:\[[^\]]*\])?\{([^}]*)\}")


def merge_problem(text: str, versions: list[str]) -> str | None:
    """Why text is no merge of versions: a merge only combines them, so that new wording stays a proposal."""
    lines = text.split("\n")
    if not text.strip():
        return "merged_text is empty."
    if any(MERGE_MARKER.match(line) for line in lines):
        return "merged_text still has merge markers."
    known = {line for version in versions for line in version.split("\n")}
    new = [line for line in lines if line.strip() and line not in known]
    if new:
        return ("merged_text has lines that are in no version of the file, so it would change the note outside a proposal. "
                "Keep lines of the versions as they are, then propose new wording with create_proposals. First new line: "
                + new[0][:200])
    return None


def feedback_counts(records: list) -> dict:
    liked = sum(1 for f in records if f.get("polarity") == "positive")
    counts = {"liked": liked, "disliked": sum(1 for f in records if f.get("polarity") == "negative")}
    open_comments = sum(1 for f in records if f.get("kind") in ("question", "other") and not f.get("resolved_at"))
    if open_comments:
        counts["open_comments"] = open_comments
    return counts


def retired_at(history: list) -> str | None:
    """When the card was retired, or None when it is active (thoughts-server protocol/review-card-retire.md:
    the latest retire or restore not undone, by reviewed_at then event_id, decides)."""
    undone = {e.get("undone_event_id") for e in history if e.get("queue_action") == "undo_retire"}
    actions = [(at, e.get("event_id") or "", e) for e in history
               if e.get("queue_action") in ("retire", "restore") and e.get("event_id") not in undone
               and (at := _when(e.get("reviewed_at"))) is not None]
    if not actions:
        return None
    latest = max(actions, key=lambda item: item[:2])[2]
    return latest.get("reviewed_at") if latest.get("queue_action") == "retire" else None


def link_id(project_id: str, agent: str, conversation_id: str) -> str:
    """The agent_conversation record id (thoughts-server protocol/agent-conversations.md 2)."""
    return hashlib.sha256(f"{project_id}\n{agent}:{conversation_id}".encode()).hexdigest()


def stamp_ms(when: datetime) -> str:
    return when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def linked_conversation(stored: dict | None, *, project_id: str, conversation_id: str, card_id: str, title: str,
                        now: datetime) -> dict:
    """The agent_conversation payload after an explicit link of this claude.ai chat to a review card: the record
    created, or the stored one copied (unknown fields kept) and touched as an agent link (spec 3.2; card entry:
    thoughts-card-agent-conversations spec 2). The chat has no URL Thoughts accepts, so conversation_url is ""."""
    at = stamp_ms(now)
    if stored is None:
        record = {"schema_version": 1, "link_id": link_id(project_id, "claude", conversation_id), "project_id": project_id,
                  "agent": "claude", "conversation_id": conversation_id, "conversation_url": "", "title": title,
                  "title_source": "agent", "session_name": "", "linked_by": "agent", "created_at": at,
                  "last_active_at": at, "revision": 0, "removed": False, "removed_at": None, "deleted": False}
    else:
        record = json.loads(json.dumps(stored))
        record.update(title=title, title_source="agent", removed=False, removed_at=None)
        record["last_active_at"] = max(record.get("last_active_at") or at, at)
    previous = record.get("updated_at") or ""
    if previous >= at:  # strictly after the stored write
        at = stamp_ms(datetime.fromisoformat(previous.replace("Z", "+00:00")) + timedelta(milliseconds=1))
    record.update(last_tool="link_conversation", device_name="claude.ai", updated_at=at, origin_id="connector",
                  revision=int(record.get("revision") or 0) + 1)
    cards = [c for c in record.get("review_cards") or [] if isinstance(c, dict)]
    entry = next((c for c in cards if c.get("card_id") == card_id), None)
    if entry is None:
        cards.append({"card_id": card_id, "linked_at": at, "last_active_at": at, "linked_by": "agent", "removed": False})
        # At most 200: drop removed ones first, then the one active longest ago, never the new one.
        while len(cards) > 200:
            cards.remove(min(cards[:-1], key=lambda c: (not c.get("removed"), str(c.get("last_active_at") or ""))))
    else:
        entry.update(removed=False, last_active_at=max(str(entry.get("last_active_at") or at), at))
    record["review_cards"] = cards
    return record


INSTRUCTIONS = (
    "The user's Thoughts Library: their notes and papers (LaTeX or Markdown sources, imported PDFs) in projects, and "
    "the review cards they study (a question plus the marked passage as answer, and how they rated themselves: "
    "Understanding, Recall, Interest tags). list_documents and search_library find documents; read_document reads one. "
    "review_cards and get_review_card show cards and the user's review history; use them to see what the user knows "
    "before explaining. Before explaining a concept or editing a passage on a topic the user may have studied, call "
    "knowledge_profile once for that topic and calibrate: skip or compress what is solid, explain shaky and not-understood "
    "points carefully. With few or no matching cards, its result also says what the user's cards show about them in "
    "general (its learner section and the end of its summary): connect new material to what they know well and find "
    "interesting. create_review_card makes a card from a note's passage (or a whole section, by its heading) and "
    "move_review_card moves a card on an "
    "imported PDF into a note, only when the user asks; onto a pending proposal's proposed text it waits and moves the "
    "card when the user accepts that proposal (pending_card_moves lists such moves). To move a review card's marked "
    "passage (between its \\ReviewCardMaskStart/End or <!-- thoughts:v1:start/end review:... --> marker lines) "
    "elsewhere, e.g. when merging notes, never cut and paste it with two proposals and never ask the user to do it by "
    "hand: call move_review_card with start_line equal to end_line, which proposes removing it here and inserting it "
    "there as one decision for the user. To merge review cards into one card with several passages, "
    "when the user asks for it, call merge_review_cards once: never delete one card and edit another, or move markers "
    "with proposals. send_note puts a picture (Markdown with $math$ and tappable links) on the user's phone, which they can draw on; "
    "get_note shows their handwritten reply. While you run you answer the replies to your own notes: pass your "
    "conversation_id to send_note and get_note (get_note then claims the reply for you, so nobody answers twice, and "
    "hands you the user's answering rules, also in note_rules: follow them whenever you answer a reply or page). The "
    "Thoughts reply agent answers what no running agent covers. When the user makes you responsible for a topic, call "
    "own_topic (and keep ~/.local/bin/thoughts-watch-replies --conversation <your id> running); release_topic ends it. "
    "add_diary_entry writes to their Diary. Library documents are never edited "
    "directly: to suggest a change to a LaTeX or Markdown note, read it with read_source (exact lines and its source_sha256) "
    "and call create_proposals; the user accepts or rejects each proposal in Thoughts. revise_proposal changes a pending one, "
    "and remove_proposal withdraws a pending proposal you made (for example one that breaks the build). "
    "create_document adds a new note to a project, only when the user asks for a new note. "
    "import_pdf adds a paper to a project from its URL (the Thoughts server downloads it, as Import document in the "
    "Library does): when the user asks to add a PDF, import it yourself instead of handing them a file to import; "
    "link it from a note with \\PdfLink{<its filename without .pdf>}{document-start}{text}. move_pdf moves an "
    "imported PDF to another project. create_project makes a project with a docs folder of its own (add_source_folder "
    "gives an existing project one), and move_note moves a note of a docs folder to another project with its review "
    "cards, links and interactive pages, as Move to project on the Mac does; never re-create a note to move it. "
    "An interactive HTML page (sliders, a small simulation) lives in a note's docs-* folder: add it with add_html_page, "
    "then link it from the note in a create_proposals replacement, LaTeX \\href{thoughts-html:<path>}{text} or Markdown "
    "[text](thoughts-html:<path>); html_pages lists a folder's pages. A link to a page the folder lacks is refused. "
    "document_feedback lists the passages the user liked or disliked (with comments) while reading a document's PDF. "
    "When a note's change collides with one on the user's computer, Thoughts says Merge needed and their Sync stops: "
    "resolve it yourself with source_conflicts, read_source_conflict and resolve_source_conflict instead of asking the "
    "user to open Thoughts. "
    "When the user's message contains \"Review card: <id>\" (Thoughts' Copy for agent text), call link_conversation "
    "once with that review_card and a short title, so this conversation shows on that card in Thoughts.")


def create_app(service: core.Service = SERVICE, thoughts_url: str = THOUGHTS_URL, token_file: Path = TOKEN_FILE):
    import httpx
    from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

    ctx = core.build(service, INSTRUCTIONS)
    mcp = ctx.mcp
    thoughts = Thoughts(httpx.AsyncClient(), thoughts_url, token_file, service.state)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True)

    def result(text: str, structured: dict, extra: list | None = None, error: bool = False) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text), *(extra or [])],
                              structuredContent=structured, isError=error)

    async def find(document_id: str) -> dict:
        docs = await thoughts.documents()
        if document_id in docs:
            return docs[document_id]
        matches = [d for d in docs.values() if fold(document_id) == fold(d["name"] or "")]
        if len(matches) == 1:
            return matches[0]
        raise LookupError(f"no document {document_id!r}; use list_documents or search_library for ids")

    @mcp.tool(name="list_documents", title="List Library documents", annotations=readonly)
    async def list_documents(query: str = "", project: str = "") -> CallToolResult:
        """The Library's documents (id, name, project, kind: tex, md or pdf, pages, published, and the Library filename
        of its PDF, which links name), optionally only those whose name or project contains query, or of one project.
        An unpublished document stays in its project but is not built, and its review cards are not offered for review."""
        words = fold(query).split()
        docs = [d for d in (await thoughts.documents()).values()
                if (not project or fold(project) in fold(d["project"])) and all(w in fold(f"{d['name']} {d['project']}") for w in words)]
        docs.sort(key=lambda d: (fold(d["project"]), fold(d["name"] or "")))
        rows = [{k: d[k] for k in ("document_id", "name", "project", "kind", "pages", "published", "filename")} for d in docs]
        text = "\n".join(f"[{d['project']}] {d['name']} ({d['kind']}{'' if d['published'] else ', unpublished'}, "
                         + (f"file {d['filename']}, " if d["filename"] else "") + f"id {d['document_id']})" for d in docs)
        return result(f"{len(docs)} document(s):\n{text}", {"documents": rows})

    @mcp.tool(name="read_document", title="Read a document", annotations=readonly)
    async def read_document(document_id: str, from_line: int = 0, max_words: int = 10000) -> CallToolResult:
        """A document's whole text up to max_words words (default 10000, about 25 pages; at most 20000): the
        LaTeX/Markdown source (included files inlined, card markers removed) or, for an imported PDF, its pages'
        text (one line per page). Longer documents continue from next_line (pass it as from_line)."""
        doc = await find(document_id)
        lines = await thoughts.document_text(doc)
        budget, out, used, next_line = max(200, min(max_words, 20000)), [], 0, None
        for index in range(max(0, from_line), len(lines)):
            words = len(lines[index].split())
            if out and used + words > budget:
                next_line = index
                break
            out.append(lines[index])
            used += words
        text = "\n".join(out)
        try:
            counts = feedback_counts((await thoughts.document_feedback()).get(doc["document_id"], []))
        except Exception as error:  # noqa: BLE001  (the feedback count is extra; reading never fails on it)
            core.log(f"read_document {doc['document_id']}: no feedback count ({type(error).__name__}: {error})")
            counts = None
        note = (f"\nThe user marked {counts['liked']} liked and {counts['disliked']} disliked passage(s) here (document_feedback)."
                if counts and (counts["liked"] or counts["disliked"]) else "")
        return result(f"{doc['name']} [{doc['project']}, {doc['kind']}], lines {from_line}–{from_line + len(out)} of {len(lines)}\n{text}"
                      + (f"\n… continues at line {next_line}" if next_line else "") + note,
                      {"document_id": doc["document_id"], "name": doc["name"], "kind": doc["kind"], "lines": len(lines),
                       "words": used, "text": text, "next_line": next_line, "feedback": counts})

    @mcp.tool(name="document_feedback", title="Liked and disliked passages of a document", annotations=readonly)
    async def document_feedback(document_id: str) -> CallToolResult:
        """The user's comments on passages of a document, on its PDF or one of its HTML pages (document id or exact
        name), oldest first. A comment is a like, a dislike, a question or something else: polarity ("positive" =
        liked, "negative" = disliked, "neutral" = kind says "question" or "other"), then for those two resolved_at
        when the user closed it, the quoted passage, the user's comment, the page (html_attachment instead for an HTML page), and the PDF
        snapshot it was given on (snapshot "current" when that is the document's current PDF, "earlier" when the
        document was rebuilt since, so pages may have moved).
        Feedback on review card answers is shown with the card (get_review_card), not here.
        A question is the user asking you something: answer it (in your reply or note); it is not a request to change
        the document, so make no proposal for it unless the user asks for one. Likes and dislikes say what to keep and
        what to change; a change goes in a proposal."""
        by_document = await thoughts.document_feedback()
        try:
            doc = await find(document_id)
        except LookupError:
            # Feedback is kept after its document left the catalogue; its id still finds it.
            if document_id not in by_document:
                raise
            doc = {"document_id": document_id, "name": None, "pdf_sha256": None}
        records = [feedback_summary(f, doc["pdf_sha256"]) for f in by_document.get(doc["document_id"], [])]
        counts = feedback_counts(by_document.get(doc["document_id"], []))
        lines = [feedback_line(r) for r in records]
        name = doc["name"] or doc["document_id"]
        waiting = f", {counts['open_comments']} open question(s) or comment(s)" if counts.get("open_comments") else ""
        return result(f"{name}: {counts['liked']} liked, {counts['disliked']} disliked passage(s){waiting}" + ("\n" if lines else "")
                      + "\n".join(lines),
                      {"document_id": doc["document_id"], "name": doc["name"], "current_pdf_sha256": doc["pdf_sha256"],
                       **counts, "total": len(records), "feedback": records})

    async def open_conflict(conflict_id: str) -> dict | None:
        records = (await thoughts.request("GET", "/v1/conflicts")).json()
        return next((record for record in records if record["id"] == conflict_id), None)

    async def conflict_text(record: dict, version: str) -> str:
        digest = record[CONFLICT_VERSIONS[version]]
        if not digest:
            raise ThoughtsError(f"this conflict has no {version} version")
        return (await thoughts.object(digest)).decode()

    @mcp.tool(name="source_conflicts", title="Notes waiting for a merge", annotations=readonly)
    async def source_conflicts() -> CallToolResult:
        """The source files waiting for a merge: "Merge needed" in Thoughts, where a file changed on the user's
        computer and on the server in overlapping places. The user's Sync stops at such a file until it is resolved.
        The newest are last; an old one may belong to a device that is no longer in use. resolvable is false for a
        conflict too old to resolve here, which stays for the user in Thoughts."""
        records = (await thoughts.request("GET", "/v1/conflicts")).json()
        rows = [{"conflict_id": r["id"], "path": r["path"], "conflict_count": r.get("conflict_count"),
                 "computer_changed_at": r.get("incoming_modified_at"), "server_changed_at": r.get("current_modified_at"),
                 "resolvable": r.get("expected_revision") is not None} for r in records]
        lines = [f"- {r['path']} ({r['conflict_id']}), {r['conflict_count'] or '?'} overlap(s)"
                 + ("" if r["resolvable"] else ", not resolvable here") for r in rows]
        return result(f"{len(rows)} source file(s) waiting for a merge" + ("\n" if lines else "") + "\n".join(lines),
                      {"conflicts": rows})

    @mcp.tool(name="read_source_conflict", title="Read a merge", annotations=readonly)
    async def read_source_conflict(
        conflict_id: Annotated[str, Field(pattern=UUID_FIELD, description="conflict_id from source_conflicts.")],
        version: Annotated[Literal["merged", "server", "computer", "base"], Field(description="merged: both sides with <<<<<<< markers where they overlap; server and computer: each side whole; base: the version both started from.")] = "merged",
    ) -> CallToolResult:
        """One version of a file waiting for a merge. Compare computer with base to see what the user changed there
        (accepting a proposal removes its \\agentProposedEdit wrapper and leaves the new text), and server with base
        for what changed here."""
        record = await open_conflict(conflict_id)
        if record is None:
            return result("This conflict is not open any more.", {"state": "not_open"}, error=True)
        text = await conflict_text(record, version)
        lines = text.split("\n")
        markers = [number for number, line in enumerate(lines, 1) if line.startswith("<<<<<<<")]
        return result(text, {"conflict_id": conflict_id, "path": record["path"], "version": version, "lines": len(lines),
                             "marker_lines": markers, "text": text})

    @mcp.tool(name="resolve_source_conflict", title="Resolve a merge",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False, idempotentHint=True))
    async def resolve_source_conflict(
        conflict_id: Annotated[str, Field(pattern=UUID_FIELD, description="conflict_id from source_conflicts.")],
        keep: Annotated[Literal["server", "computer", "merged"], Field(description="server or computer keeps that side whole; merged keeps merged_text.")],
        merged_text: Annotated[str, Field(max_length=4000000, description="With keep=merged: the whole file, without merge markers, using only lines of the versions.")] = "",
        allow_dropping_proposals: Annotated[bool, Field(description="Allow a result without pending proposals that are on the server. Only when the user decided that.")] = False,
    ) -> CallToolResult:
        """Resolve a file waiting for a merge, so that the user does not have to and their Sync goes on. Keep what both
        sides did: when only one side has real changes keep that side, otherwise combine them with keep=merged. A merge
        only combines versions: new wording goes through create_proposals afterwards, and the proposals pending on the
        server stay unless allow_dropping_proposals is true. The file may have moved on since the conflict; the server
        then merges this result into it. The user's computer takes the result at its next Sync and keeps its own version
        in a backup. Tell the user what you kept. Calling again after a success reports that the conflict is closed."""
        record = await open_conflict(conflict_id)
        if record is None:
            return result("This conflict is not open any more: it was already resolved.", {"state": "not_open"})
        if record.get("expected_revision") is None:
            return result("This conflict is too old to resolve here; it stays for the user in Thoughts.",
                          {"accepted": False, "error_code": "not_resolvable"}, error=True)
        server = await conflict_text(record, "server")
        if keep == "merged":
            versions = [server, await conflict_text(record, "computer"), await conflict_text(record, "base")]
            if record.get("merged_object_sha256"):
                versions.append(await conflict_text(record, "merged"))
            problem = merge_problem(merged_text, versions)
            if problem:
                return result(f"Nothing was written: {problem}", {"accepted": False, "error_code": "invalid_merge"}, error=True)
            text = merged_text
        else:
            text = server if keep == "server" else await conflict_text(record, "computer")
        dropped = sorted(set(PROPOSAL_ID.findall(server)) - set(PROPOSAL_ID.findall(text)))
        if dropped and not allow_dropping_proposals:
            return result(f"Nothing was written: this result would remove {len(dropped)} pending proposal(s) that are on the "
                          "server. Keep their wrappers in merged_text, or ask the user.",
                          {"accepted": False, "error_code": "would_drop_proposals", "proposal_ids": dropped}, error=True)
        content = text.encode()
        digest = hashlib.sha256(content).hexdigest()
        await thoughts.request("POST", "/v1/objects", content=content,
                               headers={"Content-Type": "text/x-tex; charset=utf-8", "X-Content-SHA256": digest})
        sequence = (await thoughts.request("GET", "/v1/device-sequence")).json()["next_device_sequence"]
        stamp = datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        reply = (await thoughts.request("POST", f"/v1/conflicts/{conflict_id}/resolve", body={
            "operation_id": str(uuid.uuid4()), "device_sequence": sequence, "expected_revision": record["expected_revision"],
            "expected_sha256": record["current_sha256"], "content_sha256": digest, "client_timestamp": stamp})).json()
        thoughts.cached.pop("sources", None)
        core.log(f"source conflict {conflict_id}: kept {keep}, outcome {reply.get('outcome')}")
        data = {"path": record["path"], "keep": keep, "outcome": reply.get("outcome"), "revision": reply.get("revision"),
                "sha256": reply.get("sha256"), "dropped_proposals": dropped}
        if reply.get("outcome") == "conflict":
            return result(f"{record['path']} changed again and this result overlaps it: conflict {reply.get('conflict_id')} is now "
                          "open. Read it and resolve that one.", {**data, "conflict_id": reply.get("conflict_id")}, error=True)
        if reply.get("outcome") not in ("applied", "merged"):
            return result(f"Thoughts did not resolve it: {reply}", data, error=True)
        return result(f"Resolved {record['path']} (kept {keep}, revision {reply.get('revision')}). The user's computer takes it "
                      "at its next Sync.", data)

    @mcp.tool(name="search_library", title="Search the Library", annotations=readonly)
    async def search_library(query: str, limit: int = 20) -> CallToolResult:
        """Documents whose name or text contains every word of query (accents and case ignored), with the first
        matching line. Imported PDFs are searched once their text has been read (read_document) at least once."""
        words = fold(query).split()
        if not words:
            return await list_documents()
        hits = []
        for doc in (await thoughts.documents()).values():
            if all(w in fold(f"{doc['name']} {doc['project']}") for w in words):
                hits.append({"document_id": doc["document_id"], "name": doc["name"], "project": doc["project"], "match": "name"})
                continue
            if doc["kind"] == "pdf" and not (thoughts.pdf_dir / f"{doc['pdf_sha256']}.json").exists():
                continue
            for index, line in enumerate(await thoughts.document_text(doc)):
                if all(w in fold(line) for w in words):
                    hits.append({"document_id": doc["document_id"], "name": doc["name"], "project": doc["project"],
                                 "match": "text", "line": index, "passage": snippet(line, words)})
                    break
        hits.sort(key=lambda h: (h["match"] != "name", fold(h["project"]), fold(h["name"] or "")))
        shown = hits[:max(1, min(limit, 100))]
        text = "\n".join(f"[{h['project']}] {h['name']} (id {h['document_id']})" + (f" line {h['line']}: {h['passage']}" if "line" in h else "")
                         for h in shown)
        return result(f"{len(hits)} document(s):\n{text}", {"total": len(hits), "results": shown})

    @mcp.tool(name="review_cards", title="Review cards", annotations=readonly)
    async def review_cards(query: str = "", document_id: str = "", state: str = "", limit: int = 30,
                           kind: str = "") -> CallToolResult:
        """The user's review cards, newest first: question, document, page, how often and how they last rated
        themselves (tags such as 'I get it' or 'Forgot'), and status: "active", "retired" with retired_at (the
        user took the card out of review but kept its history; it can be restored), or "unpublished" (its document
        is unpublished in its Project, so the card is kept but not offered for review until the document is
        published again). query filters by words in the question or document name; document_id by document; state
        by status ("active", "retired" or "unpublished"; default all).
        Each card has review_kind: "knowledge" (material the user studies) or "project" (about work they are writing or
        building, reviewed at least weekly; passage_changed says its passage changed since they last answered it), and
        review_kind_source: "card" (chosen for the card), "project" (its Library Project's kind) or "default". kind
        filters by review_kind ("knowledge" or "project"; default both).
        Deleted cards are left out. get_review_card has the answer and full history."""
        if state not in ("", "active", "retired", "unpublished"):
            raise ValueError('state must be "active", "retired", "unpublished" or empty')
        if kind not in ("", "knowledge", "project"):
            raise ValueError('kind must be "knowledge", "project" or empty')
        cards, events = await thoughts.review()
        docs = await thoughts.documents()
        projects = await thoughts.projects()
        words = fold(query).split()
        rows = []
        for card_id, card in cards.items():
            history = events.get(card_id, [])
            if deleted(history):
                continue
            summary = card_summary(card, history, docs, projects)
            if document_id and summary["document_id"] != document_id:
                continue
            if state and summary["status"] != state:
                continue
            if kind and summary.get("review_kind", "knowledge") != kind:
                continue
            if words and not all(w in fold(f"{summary['question'] or ''} {summary['document'] or ''}") for w in words):
                continue
            rows.append(summary)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        shown = rows[:max(1, min(limit, 200))]
        text = "\n".join(f"- {r['question']} [{r['document']}, p. {r['page']}] {r['reviews']} review(s)"
                         + (f", last {r['last_reviewed_at'][:10]}: {', '.join(r['last_tags']) or r['last_rating']}" if r.get("last_reviewed_at") else "")
                         + (f", retired {r['retired_at'][:10]}" if r.get("retired_at") else "")
                         + (", unpublished document" if r["status"] == "unpublished" else "")
                         + kind_text(r)
                         + f" (card {r['card_id']})" for r in shown)
        return result(f"{len(rows)} card(s):\n{text}", {"total": len(rows), "cards": shown})

    async def card_pending_moves(card_id: str) -> list | None:
        """The card's moves waiting for (or settled by) a proposal, newest first; None on a server without them."""
        try:
            if PENDING_MOVES_CAPABILITY not in ((await thoughts.ttl("health", "/v1/health")).get("capabilities") or []):
                return None
            response = await thoughts.request("GET", "/v1/review-card-pending-moves",
                                              params={"card_id": card_id, "state": "all", "limit": 20})
            return response.json().get("pending_moves") or []
        except (ThoughtsError, ValueError):
            return None

    @mcp.tool(name="get_review_card", title="Review card with answer", annotations=readonly)
    async def get_review_card(card_id: str) -> CallToolResult:
        """One review card: question, answer (the marked passage of its source, or the PDF text it selected),
        status ("active", or "retired" with retired_at), review_kind ("knowledge" or "project", with
        review_kind_source and, for a project card, passage_changed) and every review with its tags and notes;
        retiring, restoring and undoing a retirement appear in the history as action retire, restore and undo_retire,
        and choosing the card's kind as action set_review_kind with review_kind.
        pending_moves lists the card's moves onto a pending proposal's text (move_review_card), waiting or settled."""
        cards, events = await thoughts.review()
        if card_id not in cards:
            raise LookupError(f"no review card {card_id!r}")
        history = events.get(card_id, [])
        summary = card_summary(cards[card_id], history, await thoughts.documents(), await thoughts.projects())
        answer = await thoughts.card_answer(cards[card_id])
        summary.update(answer=answer, deleted=deleted(history), history=[
            {("action" if k == "queue_action" else k): e.get(k)
             for k in ("reviewed_at", "rating", "queue_action", "review_kind", "review_tags", "review_group_notes")
             if e.get(k)}
            for e in history])
        moves = await card_pending_moves(card_id)
        if moves is not None:
            summary["pending_moves"] = moves
        lines = [f"Q: {summary['question']}", f"Document: {summary['document']}, page {summary['page']}"
                 + (f" (retired {summary['retired_at'][:10]})" if summary.get("retired_at") else "")
                 + kind_text(summary),
                 "A: " + (answer or "(a region of the PDF without text)")]
        lines += [pending_move_line(move) for move in moves or [] if move.get("state") == "waiting_for_proposal"]
        lines += [f"{h.get('reviewed_at', '')[:10]} {h.get('rating') or h.get('action')}"
                  + (f" {h['review_kind']}" if h.get("review_kind") else "") + f": {', '.join(h.get('review_tags', []))}"
                  + (f" — {h['review_group_notes']}" if h.get("review_group_notes") else "") for h in summary["history"]]
        return result("\n".join(lines), summary)

    @mcp.tool(name="knowledge_profile", title="Profile the user's knowledge of a topic", annotations=readonly)
    async def knowledge_profile(
        topic: Annotated[str, Field(min_length=1, max_length=2000, description="The concept or question, in a few words, e.g. \"GRPO advantage\".")],
        project: Annotated[str | None, Field(max_length=2000, description="Only cards of this Library project (its name, case-insensitive).")] = None,
        document: Annotated[str | None, Field(max_length=2000, description="Only cards of this Library document: its PDF filename, name or document ID.")] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=200, description="At most this many matching cards are classified.")] = 40,
        learner: Annotated[bool | None, Field(description="Include what all of the user's cards say about them in general (learner). Default: only when fewer than 3 cards match.")] = None,
    ) -> CallToolResult:
        """What the user knows about a topic, from their review cards (read-only).

        Classifies each matching card as solid, shaky, unknown (not understood) or unreviewed, from the
        tags of its answers (the latest weighs most), lapses, interval length and how overdue it is.
        Retired cards (taken out of review, history kept) count with their history, are never overdue, and
        their reason ends with "retired"; deleted cards are left out.
        A card the user came up with (an answer tagged "I came up with it") is solid whatever its schedule and
        has own_idea true, reason "own idea (...)": the idea is theirs, so build on it and never explain it back.
        Returns counts, the cards per class (question, document and a one-line reason such as
        "last 3 answers: Correct, Memorized, Correct; interval 25 d"), a short plain-language summary,
        and the tag-to-level mapping used (the default mapping).
        A card's signals give the tags of its last answer from the user's other tag groups, such as Interest
        ("Very interesting") and Effort ("Studied"); the summary ends with how typical each one is.
        When fewer than 3 cards match (or with learner), learner describes all of the user's cards: counts per
        class, the documents studied most with their mix and typical interest, the groups' typical tags,
        questions they know well and struggle with, and their latest liked and disliked answer passages.
        Call it once per topic before explaining or editing, then calibrate; never change cards because of it.
        """
        profile = await knowledge.knowledge_profile(thoughts, topic=topic, project=project, document=document, limit=limit,
                                                    learner=learner, kinds=review_kinds())
        return result(json.dumps(profile, ensure_ascii=False), profile, error=profile.get("accepted") is False)

    @mcp.tool(name="link_conversation", title="Show this conversation on a review card",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True))
    async def link_conversation(
        review_card: Annotated[str, Field(pattern=UUID_FIELD, description="The review card id (the \"Review card:\" line of the user's Copy for agent text).")],
        conversation_id: Annotated[str, Field(pattern=UUID_FIELD, description="Lowercase UUID of this conversation, the same one create_proposals takes: make one random UUID the first time in this conversation and reuse it for every later call.")],
        title: Annotated[str, Field(max_length=200, description="Short description of what this conversation does for the card; empty uses the card's question.")] = "",
    ) -> CallToolResult:
        """List this conversation on a review card in Thoughts: the card shows its newest conversation above its
        buttons and all of them in its menu, on the Mac and the phone after their next Sync. Call it once when the
        user's message contains "Review card: <id>"; calling it again with the same conversation_id updates the entry.
        The entry has no link back to this chat (Thoughts only opens Claude Code and Codex conversations)."""
        cards, _ = await thoughts.review()
        card = cards.get(review_card)
        if card is None:
            raise LookupError(f"no review card {review_card!r}; use review_cards for ids")
        primary = card.get("primary") or {}
        document_id = primary.get("pdf_document_id") or primary.get("document_id")
        project = next((p for p in await thoughts.projects()
                        if any(d.get("document_id") == document_id for f in ("documents", "unpublished_documents")
                               for d in p.get(f, []))), None)
        if project is None:
            raise LookupError(f"the document of review card {review_card} is in no Library Project")
        title = " ".join(title.split())[:200] or " ".join(f"Card: {primary.get('question') or review_card}".split())[:200]
        lid = link_id(project["id"], "claude", conversation_id)
        projections = (await thoughts.request("GET", "/v1/projections")).json()["projections"]
        stored = next((p["payload"] for p in projections
                       if p["entity_type"] == "agent_conversation" and p["entity_id"] == lid and not p["deleted"]), None)
        record = linked_conversation(stored, project_id=project["id"], conversation_id=conversation_id,
                                     card_id=review_card, title=title, now=datetime.now(ZoneInfo("UTC")))
        sequence = (await thoughts.request("GET", "/v1/device-sequence")).json()["next_device_sequence"]
        operation = {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "agent_conversation",
                     "entity_id": lid, "collection": "agent-conversations", "base_revision": record["revision"] - 1,
                     "client_timestamp": record["updated_at"], "payload": record}
        response = (await thoughts.request("POST", "/v1/operations", body={"operations": [operation]})).json()
        outcome = next(iter(response.get("results") or []), {}).get("outcome")
        if outcome not in ("applied", "duplicate"):
            raise ThoughtsError(f"Thoughts did not store the link (outcome {outcome}); try again")
        core.log(f"conversation linked to card {review_card}: {title!r}")
        return result(f"This conversation now shows on the card \"{primary.get('question')}\" ({project['name']}) as "
                      f"\"{title}\"; the Mac and phone show it after their next Sync.",
                      {"link_id": lid, "project_id": project["id"], "project_name": project["name"],
                       "review_card_id": review_card, "conversation_id": conversation_id, "title": title,
                       "conversation_url": ""})

    notes_ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @mcp.tool(name="list_notes", title="Notes sent to the phone", annotations=notes_ro)
    async def list_notes(document_id: str = "", review_card_id: str = "", limit: int = 20) -> CallToolResult:
        """Notes sent to the user's phone (by any agent), newest first, with their reply counts."""
        params = {"limit": max(1, min(limit, 100))}
        if document_id:
            params["document_id"] = document_id
        if review_card_id:
            params["review_card_id"] = review_card_id
        notes = (await thoughts.request("GET", "/v1/agent-notes", params=params)).json()["notes"]
        rows = [{"note_id": n["note_id"], "title": n["title"], "created_at": n["created_at"], "sender": n["sender_device_id"],
                 "opened": bool(n.get("opened_at")), "replies": len(n.get("replies") or []),
                 # Glasses captures the user sent with a reply: that reply's agent answers them, not a watcher.
                 "photo_captures": reply_photo_captures(n)} for n in notes]
        text = "\n".join(f"- {r['created_at'][:16]} {r['title']} ({r['replies']} repl{'y' if r['replies'] == 1 else 'ies'}, id {r['note_id']})"
                         + (f" with photo {', '.join(r['photo_captures'])}" if r["photo_captures"] else "") for r in rows)
        return result(f"{len(rows)} note(s):\n{text}", {"notes": rows})

    @mcp.tool(name="get_note", title="A note and the user's replies", annotations=notes_ro)
    async def get_note(note_id: str, wait_for_reply_s: int = 0, include_note_image: bool = False,
                       conversation_id: str = "") -> CallToolResult:
        """One note with the user's replies as images (what they drew or wrote on the phone, and a photo from their
        glasses when they used Send with picture), or a page the user sent. wait_for_reply_s (at most 45) waits for a
        first reply to a note this connector sent; while it waits the phone tells the user an agent is listening.
        With conversation_id (your conversation's id), each reply (or the page) is claimed for you when you cover it
        (your note, or a topic you own): "yours" means answer it, following the user's answering rules (given here);
        "taken" means another agent answers it; "not yours" means leave it to the agent that covers it."""
        if not re.fullmatch(UUID, note_id):
            raise LookupError("note_id must be a note's UUID")
        note = (await thoughts.request("GET", f"/v1/agent-notes/{note_id}")).json()
        note = note.get("note", note)
        if wait_for_reply_s > 0 and not note.get("replies"):
            await thoughts.request("GET", "/v1/agent-notes/replies",
                                   params={"note_id": note_id, "wait": min(45, wait_for_reply_s), "listen": "true"}, timeout=60)
            note = (await thoughts.request("GET", f"/v1/agent-notes/{note_id}")).json()
            note = note.get("note", note)
        images = []
        if include_note_image:
            data = (await thoughts.request("GET", f"/v1/agent-notes/{note_id}/image")).content
            images.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=note.get("media_type", "image/png")))
        replies = []
        for reply in note.get("replies") or []:
            photo = reply.get("photo") if isinstance(reply.get("photo"), dict) else None
            sources, sources_rule = reply_sources(reply.get("sources"))
            replies.append({**{k: reply.get(k) for k in ("reply_id", "created_at", "message", "stroke_count")},
                            "photo_capture": photo.get("capture") if photo else None, "has_photo": photo is not None,
                            # What the user lets the answer use (server agent_note_reply_sources_v1).
                            "sources": sources, "sources_rule": sources_rule})
            if reply.get("image_url"):
                data = (await thoughts.request("GET", f"/v1/agent-notes/replies/{reply['reply_id']}/image")).content
                images.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=reply.get("media_type") or "image/png"))
            if photo:
                data = (await thoughts.request("GET", f"/v1/agent-notes/replies/{reply['reply_id']}/photo")).content
                images.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=photo.get("media_type") or "image/jpeg"))
        conversation = (conversation_id or DEFAULT_CONVERSATION).strip()
        page = isinstance(note.get("origin"), dict) and note["origin"].get("kind") == "phone_page"
        handling = await claim_items([note_id] if page else [r["reply_id"] for r in replies], conversation)
        for reply in replies:
            reply["handling"] = handling.get(reply["reply_id"])
        summary = {"note_id": note_id, "title": note.get("title"), "message": note.get("message"),
                   "opened_at": note.get("opened_at"), "replies": replies}
        if page:
            summary["page_handling"] = handling.get(note_id)
        text = (f"{note.get('title')}: {len(replies)} repl{'y' if len(replies) == 1 else 'ies'}"
                + "".join(f"\n- {r['created_at'][:16]}" + (f": {r['message']}" if r.get("message") else "")
                          + (f" [with a photo from the glasses: {r['photo_capture'] or 'attached'}; act on the words with it]"
                             if r["has_photo"] else "")
                          + (f" [Sources the user allows: {r['sources_rule']}]" if r["sources"] != DEFAULT_REPLY_SOURCES else "")
                          + (f" ({handling_words(r['handling'])})" if r.get("handling") else "") for r in replies)
                + (f"\nThis page: {handling_words(handling[note_id])}." if page and handling.get(note_id) else ""))
        if any((value or {}).get("state") == "yours" for value in handling.values()):
            text += "\n\n" + await rules_block()
        return result(text, summary, images)

    async def claim_items(item_ids: list[str], conversation: str) -> dict:
        """Claims each reply or page for the conversation when it covers it (agent_note_claims_v1); item -> state."""
        if not item_ids or not conversation:
            return {}
        try:
            if CLAIMS_CAPABILITY not in ((await thoughts.ttl("health", "/v1/health")).get("capabilities") or []):
                return {}
        except ThoughtsError:
            return {}
        out = {}
        for item_id in item_ids:
            try:
                response = await thoughts.request("POST", f"/v1/agent-notes/handling/{item_id}/claim", refusals=True,
                                                  body={"conversation_id": conversation, "only_if_covering": True})
                answer = response.json()
            except (ThoughtsError, ValueError) as error:
                out[item_id] = {"state": "unknown", "detail": str(error)}
                continue
            claim = answer.get("claim") if isinstance(answer.get("claim"), dict) else None
            state = ("yours" if answer.get("claimed") else
                     "taken" if answer.get("error_code") == "agent_note_claimed" else "not_yours")
            out[item_id] = {"state": state, "claim": claim}
        return out

    def handling_words(value: dict) -> str:
        claim = value.get("claim") or {}
        if value.get("state") == "yours":
            return "yours to answer: answer it following the user's answering rules"
        if value.get("state") == "taken":
            who = "the Thoughts reply agent" if claim.get("role") == "responder" else (
                f"another agent ({claim.get('device_id')}" + (f", conversation {claim['conversation_id']}"
                                                              if claim.get("conversation_id") else "") + ")")
            return f"{who} answers it; do not answer it again"
        if value.get("state") == "not_yours":
            return "not yours: the agent that covers it answers it; answer only if the user asks you"
        return "who answers it is unknown"

    async def rules_block() -> str:
        if RULES_IN_PROMPT:
            return "Follow the user's answering rules (in your instructions)."
        rules = await answering_rules()
        if not rules.get("text"):
            return f"The user's answering rules are not available ({rules.get('missing')})."
        return "The user's answering rules (follow them when you answer):\n" + rules["text"]

    async def answering_rules() -> dict:
        try:
            if RULES_CAPABILITY not in ((await thoughts.ttl("health", "/v1/health")).get("capabilities") or []):
                return {"text": None, "missing": "this Thoughts server does not serve answering rules yet"}
            response = await thoughts.request("GET", "/v1/agent-notes/rules", refusals=True)
        except ThoughtsError as error:
            return {"text": None, "missing": str(error)}
        answer = response.json()
        if response.status_code >= 400 or not isinstance(answer.get("text"), str):
            return {"text": None, "missing": "the Library has no note named agent-note-rules.md yet"}
        return {key: answer.get(key) for key in ("text", "sha256", "source_path", "revision", "updated_at")}

    async def mental_model_page() -> dict:
        try:
            if MENTAL_MODEL_CAPABILITY not in ((await thoughts.ttl("health", "/v1/health")).get("capabilities") or []):
                return {"text": None, "missing": "this Thoughts server does not serve the Mental Model yet"}
            response = await thoughts.request("GET", "/v1/mental-model", refusals=True)
        except ThoughtsError as error:
            return {"text": None, "missing": str(error)}
        answer = response.json()
        if response.status_code >= 400 or not isinstance(answer.get("text"), str):
            return {"text": None, "missing": "the Library has no note named mental-model.md yet"}
        return {key: answer.get(key) for key in ("text", "sha256", "source_path", "revision", "updated_at")}

    @mcp.tool(name="mental_model", title="The user's Mental Model", annotations=notes_ro)
    async def mental_model() -> CallToolResult:
        """The user's Mental Model: what they know well, their gaps, what loses them in an explanation, their own
        words and notation, how they draw, and how to explain a new topic to them. It is written from their review
        cards and feedback by Review > Mental Model > Refresh on the Mac and kept as the Library note mental-model.md,
        the one copy every agent reads. Read it before explaining something, answering a technical question or
        writing a note for the user, then follow it. Card citations link the card (thoughts-card:<card id>)."""
        page = await mental_model_page()
        return result(page.get("text") or f"Not available: {page.get('missing')}", page)

    @mcp.tool(name="note_rules", title="The user's answering rules", annotations=notes_ro)
    async def note_rules() -> CallToolResult:
        """The user's rules for answering their replies, pages and glasses captures, the same for every agent on every
        computer: the Library note agent-note-rules.md as the user accepted it. Follow them whenever you answer a reply
        or a page. The user changes them by editing that note."""
        rules = await answering_rules()
        return result(rules.get("text") or f"Not available: {rules.get('missing')}", rules)

    @mcp.tool(name="own_topic", title="Become responsible for a topic's notes",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True))
    async def own_topic(
        conversation_id: Annotated[str, Field(min_length=1, max_length=128, description="Your conversation's id (the same one you pass to send_note and get_note, and to thoughts-watch-replies --conversation).")],
        label: Annotated[str, Field(min_length=1, max_length=200, description="What the topic is, as the user named it, e.g. Linear algebra.")],
        project: Annotated[str, Field(max_length=512, description="A Library Project whose documents are the topic (its name or id); its current documents are taken.")] = "",
        document_ids: list[str] | None = None,
        review_card_ids: list[str] | None = None,
        title_tags: Annotated[list[str] | None, Field(description="A note is in the topic when its title contains one of these (any case), e.g. [Linear algebra].")] = None,
        captures: Annotated[bool, Field(description="You also answer the glasses captures (the reply agent then leaves them to the owners).")] = False,
    ) -> CallToolResult:
        """Only when the user makes you responsible for the agent notes of a topic: replies to notes in the topic
        (whoever sent them) and pages the user sends on it come to you, and the Thoughts reply agent steps aside while
        you listen (it answers after 10 minutes what is left unanswered). It lasts while you keep
        ~/.local/bin/thoughts-watch-replies --conversation <conversation_id> running; release_topic ends it."""
        try:
            capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
        except ThoughtsError as error:
            return result(f"Not done: {error}", {"error_code": "unavailable"}, error=True)
        if OWNERS_CAPABILITY not in capabilities:
            return result("This Thoughts server does not support topic owners yet.", {"error_code": "unsupported"},
                          error=True)
        documents = list(document_ids or [])
        if project:
            docs = await thoughts.documents()
            matched = [d for d in docs.values() if fold(d["project"]) == fold(project)]
            if not matched:
                return result(f"No Project {project!r}; list_documents shows the Projects.", {"error_code": "not_found"},
                              error=True)
            documents += [d["document_id"] for d in matched if d["document_id"] not in documents]
        body = {"conversation_id": conversation_id, "label": label, "document_ids": documents,
                "review_card_ids": list(review_card_ids or []), "title_tags": list(title_tags or []), "captures": captures}
        try:
            owner = (await thoughts.request("POST", "/v1/agent-notes/owners", body=body)).json()
        except ThoughtsError as error:
            return result(f"Not done: {error}", {"error_code": "refused"}, error=True)
        core.log(f"topic owner {owner.get('owner_id')}: {label!r} for conversation {conversation_id}")
        return result(f"You own the topic {label!r} (owner_id {owner.get('owner_id')}): {len(documents)} document(s), "
                      f"{len(body['review_card_ids'])} card(s), tags {body['title_tags']}"
                      + (", and the glasses captures" if captures else "")
                      + f". Keep ~/.local/bin/thoughts-watch-replies --conversation {conversation_id} running as a "
                      "background monitor: the ownership lasts while it listens.", owner)

    @mcp.tool(name="release_topic", title="Stop being responsible for a topic",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True))
    async def release_topic(owner_id: str = "", conversation_id: str = "") -> CallToolResult:
        """When the user takes a topic back (or hands it to another agent): ends one ownership (owner_id), or every topic
        your conversation owns (conversation_id). The Thoughts reply agent then answers those replies and pages again."""
        try:
            if re.fullmatch(r"[0-9a-f]{24}", owner_id or ""):
                await thoughts.request("DELETE", f"/v1/agent-notes/owners/{owner_id}")
            elif conversation_id:
                await thoughts.request("DELETE", "/v1/agent-notes/owners", params={"conversation_id": conversation_id})
            else:
                return result("Name the topic (owner_id) or your conversation (conversation_id).",
                              {"error_code": "invalid"}, error=True)
        except ThoughtsError as error:
            return result(f"Not done: {error}", {"error_code": "refused"}, error=True)
        return result("Released.", {"owner_id": owner_id or None, "conversation_id": conversation_id or None})

    @mcp.tool(name="add_diary_entry", title="Add a diary entry",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=False))
    async def add_diary_entry(text: str) -> CallToolResult:
        """Add an entry to the user's Thoughts Diary (the Diary tab on the phone and desktop), timestamped now.
        Use this when the user asks to note something in their diary or journal. Write the entry in the user's
        own first-person words, close to what they said."""
        now = datetime.now(DIARY_ZONE)
        stamp = now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        entry_id = f"thoughts-{uuid.uuid4()}"
        sequence = (await thoughts.request("GET", "/v1/device-sequence")).json()["next_device_sequence"]
        payload = {
            "schema_version": 1, "comment_id": entry_id, "thread_root_id": entry_id, "parent_id": None,
            "revision": 1, "created_at": stamp, "updated_at": stamp, "content_updated_at": stamp,
            "origin_id": "connector", "author": DIARY_AUTHOR, "contents": text.strip(), "subtype": "Note",
            "priority": "normal", "deleted": False, "project": None, "origin": None,
            "scope": {"state": "standalone"},
            "entry_kind": "diary", "diary_at": now.isoformat(timespec="seconds"), "diary_zone_id": DIARY_ZONE.key,
        }
        operation = {"operation_id": str(uuid.uuid4()), "device_sequence": sequence,
                     "entity_type": "logical_comment", "entity_id": entry_id, "collection": "comments",
                     "base_revision": 0, "client_timestamp": stamp, "payload": payload}
        await thoughts.request("POST", "/v1/operations", body={"operations": [operation]})
        core.log(f"diary entry added: {entry_id}")
        return result(f"Added to the Diary ({now.strftime('%d.%m.%Y %H:%M')}). It appears on the phone and desktop after their next Sync.",
                      {"entry_id": entry_id, "diary_at": payload["diary_at"]})

    async def rendered_links(rendered, document_id: str, warnings: list) -> list:
        """The links of a rendered note in the server's form, or [] (with a warning) when the server cannot take them.

        Library PDF links are checked against the Library the server lists; #page=N links name the PDF of the note's
        document (document_id)."""
        measured = list(getattr(rendered, "links", None) or [])  # a renderer from before links measures none
        if not measured:
            return []
        library, own_pdf = None, None
        if any(not WEB_LINK.match((href or "").strip()) for href, _rect, _label in measured):
            try:
                docs = await thoughts.documents()
            except ThoughtsError as error:
                warnings.append(f"Links to Library PDFs were not checked: the Library catalogue is unavailable ({error}).")
            else:
                library = {d["filename"].casefold(): d["filename"] for d in docs.values() if d.get("filename")}
                own_pdf = (docs.get(document_id) or {}).get("filename") if document_id else None
        links, link_warnings = note_links(measured, getattr(rendered, "css_height", 0) or 0, own_pdf, library)
        warnings.extend(link_warnings)
        if not links:
            return []
        try:
            capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
        except ThoughtsError as error:
            core.log(f"send_note: no server capabilities ({error})")
            capabilities = []
        if LINKS_CAPABILITY not in capabilities:
            warnings.append(f"The Thoughts server does not support links yet ({LINKS_CAPABILITY}); the note was sent without them.")
            return []
        return links

    @mcp.tool(name="send_note", title="Send a note to the phone",
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=False))
    async def send_note(title: str, markdown: str, message: str = "", document_id: str = "", review_card_id: str = "",
                        conversation_id: str = "") -> CallToolResult:
        """Put a note on the user's phone: markdown (headings, lists, tables, $inline$ and $$display$$ math) rendered
        as a phone-width picture the user can draw on. Optionally tie it to a document or review card. Pass your
        conversation_id, so the note is yours: you answer its replies while you run (get_note with your
        conversation_id claims them; keep ~/.local/bin/thoughts-watch-replies --conversation <id> running). When the
        note answers a reply or a page, follow the user's answering rules (note_rules).
        Links become tappable areas of the picture: [text](https://…) opens a web page, [p. 53](<Library filename>.pdf#page=53)
        opens that page (as printed) of a Library PDF (list_documents shows each document's filename), and [p. 4](#page=4)
        a page of the note's own document (document_id). Other links are dropped with a warning."""
        notes = load_agent_notes()
        rendered = await asyncio.to_thread(notes.render, markdown=markdown)
        warnings = list(rendered.warnings)
        links = await rendered_links(rendered, document_id, warnings)
        body = {"note_id": str(uuid.uuid4()), "title": title[:200],
                "image": {"media_type": "image/png", "data_base64": base64.b64encode(rendered.png).decode()},
                "origin": {"agent": "connector", **NOTE_ORIGIN}}
        conversation = (conversation_id or DEFAULT_CONVERSATION).strip()
        if conversation:
            body["origin"]["conversation_id"] = conversation[:128]
        if message:
            body["message"] = message[:4000]
        if document_id:
            body["document_id"] = document_id
        if review_card_id:
            body["review_card_id"] = review_card_id
        if links:
            body["links"] = links
        sent = (await thoughts.request("POST", "/v1/agent-notes", body=body, timeout=60)).json()
        note = sent.get("note") if isinstance(sent.get("note"), dict) else {}
        if links and "links" not in note:
            warnings.append(f"The Thoughts server did not keep the links ({LINKS_CAPABILITY}); the note has none.")
        kept = [{"href": link.get("href"), "label": link.get("label", "")} for link in note.get("links") or []]
        live = bool((sent.get("delivery") or {}).get("live"))
        # Who hears the user's reply: this conversation (its watcher, or get_note waiting), a topic owner, or the reply
        # agent; servers before agent_note_listening_v1 cannot tell.
        listening = sent.get("reply_listening") or {}
        heard = listening.get("state") or "unknown"
        rows = [row for row in listening.get("listeners") or [] if isinstance(row, dict)]
        responder = any(row.get("responder") for row in rows)
        own = any(row.get("role", "sender") == "sender" and not row.get("responder") for row in rows)
        core.log(f"note sent: {title!r} ({'phone listening' if live else 'phone not listening'}, replies {heard}"
                 + (f", {len(kept)} link(s)" if kept else "") + ")")
        text = (f"Sent '{title}' (note {body['note_id']})"
                + (f" with {len(kept)} tappable link{'' if len(kept) == 1 else 's'}" if kept else "") + ". "
                + ("The phone is open and shows it now." if live else "The phone will show it when Thoughts is opened.")
                + ("" if own or heard == "unknown" else
                   " Nothing of yours listens for the user's reply yet: keep ~/.local/bin/thoughts-watch-replies "
                   "--conversation <your id> running as a background monitor (pass that id to send_note), or wait with "
                   "get_note (wait_for_reply_s)" + ("; meanwhile the Thoughts reply agent covers it." if responder else
                                                    ", or the phone tells the user nobody listens."))
                + (" Warnings: " + "; ".join(warnings) if warnings else ""))
        return result(text, {"note_id": body["note_id"], "phone_listening": live, "reply_listening": heard,
                             "reply_agent_listening": responder,
                             "links": kept, "warnings": warnings})

    async def source_path(source: str) -> str:
        """A source path as GET /v1/sources lists it, or the main source of a document id or name."""
        sources = await thoughts.fresh_sources()
        if source in sources:
            return source
        try:
            doc = await find(source)
        except LookupError:
            raise LookupError(f"no source {source!r}; give a path such as docs-x/tex/source/intro.tex or a document id") from None
        except ThoughtsError as error:
            # The catalogue can fail on its own (a document that does not verify); paths still work.
            raise LookupError(f"the Library catalogue is unavailable ({error}); give the source path instead, "
                              "e.g. docs-x/tex/source/intro.tex") from None
        if not doc["source_file"]:
            raise LookupError(f"{doc['name']} is an imported PDF; it has no source to propose changes to")
        return doc["source_file"]

    @mcp.tool(name="read_source", title="Read a source with line numbers", annotations=readonly)
    async def read_source(source: str, from_line: int = 1, max_lines: int = 500) -> CallToolResult:
        """The exact text of one LaTeX or Markdown source file with one-based line numbers, its path and its
        source_sha256: what create_proposals and revise_proposal need. source is a source path (docs-x/tex/...)
        or a document id or name (its main source). Unlike read_document nothing is inlined or removed; files it
        includes are listed in includes, read them by path. Pending proposals appear as \\agentProposedEdit
        wrappers (Markdown: thoughts:agent-proposal blocks). Long files continue from next_line."""
        path = await source_path(source)
        read = await asyncio.to_thread(proposal_client().read_source, path)
        if not read.get("accepted"):
            raise LookupError(f"cannot read {path}: {read.get('message')}")
        text = read["text"]
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        first = max(1, from_line)
        last = min(len(lines), first - 1 + max(1, min(max_lines, 2000)))
        shown = "\n".join(f"{number:>5}| {lines[number - 1]}" for number in range(first, last + 1))
        known = await thoughts.sources()
        folder = path.split("/tex/")[0] if "/tex/" in path else path.rsplit("/", 1)[0]
        includes = []
        for match in INPUT.finditer(text):
            name = match.group(1).strip()
            found = next((c for c in (f"{folder}/{name}", f"{folder}/{name}.tex") if c in known), None)
            if found and found not in includes:
                includes.append(found)
        next_line = last + 1 if last < len(lines) else None
        digest = read["source_sha256"]
        return result(f"{path} (source_sha256 {digest}), lines {first}–{last} of {len(lines)}\n{shown}"
                      + (f"\n… continues at line {next_line}" if next_line else "")
                      + (f"\nIncludes: {', '.join(includes)}" if includes else ""),
                      {"source": path, "source_sha256": digest, "lines": len(lines), "from_line": first,
                       "text": "\n".join(lines[first - 1:last]), "next_line": next_line, "includes": includes})

    def proposal_client():
        """thoughts-cli as this device, through ALLOWED, with its request records in the connector's state."""
        cli = load_thoughts_cli()

        class ConnectorApi(cli.api.ServerApi):
            def request(self, method, path, **kwargs):
                payload = kwargs.get("payload")
                unpublish = isinstance(payload, dict) and payload.get("action") == "set_document_published"
                check_request(method, path.split("?", 1)[0], payload,
                              created_documents(service.state) if unpublish else frozenset())
                if time.time() < thoughts.paused_until:
                    raise ThoughtsError("the Thoughts server refused the connector's token; paused for a few minutes")
                try:
                    return super().request(method, path, **kwargs)
                except cli.api.ApiError as error:
                    if error.status == 401:
                        thoughts.paused_until = time.time() + PAUSE_AFTER_401
                        core.log("thoughts: 401 for the connector's token; pausing requests for 10 minutes")
                    raise

        token = token_file.read_text().strip()
        return cli.ThoughtsClient(thoughts_url, token, state_dir=service.state / "thoughts-cli",
                                  cache_dir=service.state / "thoughts-cli-cache",
                                  api=ConnectorApi(thoughts_url, token))

    async def author(payload: dict) -> CallToolResult:
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return result(f"Nothing was written: {error}", {"accepted": False, "error_code": "not_allowed",
                                                             "message": str(error)}, error=True)
        if not body.get("accepted"):
            hint = (f" The current source_sha256 is {body['current_source_sha256']}; read the source again."
                    if body.get("current_source_sha256") else "")
            if body.get("retryable"):  # its message says to retry with the same request_id
                return result(f"Not confirmed ({body.get('error_code')}), it may or may not have been written: "
                              f"{body.get('message')}", body, error=True)
            return result(f"Nothing was written ({body.get('error_code')}): {body.get('message')}{hint}", body, error=True)
        thoughts.cached.pop("sources", None)
        if body.get("state") == "proposal_removed":
            outcome = next((d.get("outcome") for d in body.get("proposal_decisions") or []), None)
            core.log(f"proposals: withdrew {body['proposal_id']} in {body['source']} ({outcome})")
            pair = body.get("withdrawn_proposal_ids") or []
            return result(
                f"Withdrew proposal {body['proposal_id']} at line {body['line']} of {body['source']}"
                + (" (replayed: already done by this request_id)" if body.get("replayed") else "")
                + f"; its original text is restored, new source_sha256 {body['source_sha256']}. "
                + (f"It was half of a card move, so the other half ({next((p for p in pair if p != body['proposal_id']), '')}"
                   + (f" in {body['other_source']}, new source_sha256 {body['other_source_sha256']}"
                      if body.get("other_source") else " in the same source")
                   + ") was withdrawn with it and the card stays where it was. " if pair else "")
                + ("Thoughts records it as withdrawn by the agent." if outcome in ("matched", "matched_late")
                   else f"The server's decision outcome: {outcome}." if outcome else ""), body)
        proposals = body.get("proposals") or [body]
        lines = [f"{'Revised' if body.get('state') == 'proposal_revised' else 'Created'} {len(proposals)} pending "
                 f"proposal(s) in {body['source']}" + (" (replayed: already written by this request_id)" if body.get("replayed") else "")
                 + f"; new source_sha256 {body['source_sha256']}. They are not accepted; the user decides in Thoughts."]
        lines += [f"- {p['proposal_id']} at line {p['line']}" for p in proposals]
        lines += [f"- figure {f['path']} (referenced as {f['reference']}) for {f['proposal_id']}"
                  for f in body.get("figures") or []]
        lines += [f"Warning: {w['message']}" + (f" (proposal {w['proposal_index']})" if "proposal_index" in w else "")
                  for w in body.get("warnings") or []]
        core.log(f"proposals: {body.get('state')} {len(proposals)} in {body['source']}")
        return result("\n".join(lines), body)

    proposal_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True)

    @mcp.tool(name="create_proposals", title="Propose edits to a note", annotations=proposal_tool)
    @with_rules
    async def create_proposals(
        source: Annotated[str, Field(min_length=1, description="Source path from read_source (e.g. docs-x/tex/source/intro.tex).")],
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source; a changed source is refused (source_conflict), nothing is rebased.")],
        proposals: Annotated[list[ProposalEdit], Field(min_length=1, max_length=500, description="Edits in any order, all in lines of the version you read: do not shift line numbers for earlier edits. Ranges may touch but not overlap; at most one insertion per line boundary.")],
        conversation_id: Annotated[str, Field(pattern=UUID_FIELD, description="Lowercase UUID of this conversation, recorded with each proposal: your Claude Code or Codex session ID if you have one; otherwise make one random UUID at the first proposal of this conversation and reuse it for every later one.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it is then answered without writing twice.")],
    ) -> CallToolResult:
        """Propose additions, replacements or deletions in one of the user's LaTeX or Markdown notes, as pending
        proposals the user accepts or rejects in Thoughts (never applied by this tool). Use it whenever you would
        change note text: notes are never edited directly. First call read_source for the exact lines and
        source_sha256. Each proposal selects whole lines start_line..end_line (one-based, end exclusive) and gives the
        replacement text: start_line == end_line inserts before that line (lines + 1 appends), an empty replacement
        deletes. The explanation says why the change is needed. Write every sentence of new prose on its own line and
        keep LaTeX braces balanced; never write \\agentProposedEdit or proposal markers yourself. Thoughts captures the
        original text, makes the proposal IDs and timestamps, refuses overlaps with pending proposals (change those with
        revise_proposal) and writes all proposals at once or none. The phone and Mac receive them with their next Sync.
        A proposal can also add pictures: give it figures (complete SVG documents) and reference each one from the
        replacement; prefer a clean SVG drawing over LaTeX picture code for diagrams."""
        return await author({"command": "create-proposals", "source": source, "expected_source_sha256": expected_source_sha256,
                             "proposals": [p.model_dump(exclude_none=True) for p in proposals],
                             "conversation_id": conversation_id, "request_id": request_id})

    @mcp.tool(name="revise_proposal", title="Revise a pending proposal", annotations=proposal_tool)
    @with_rules
    async def revise_proposal(
        source: Annotated[str, Field(min_length=1, description="Source path from read_source.")],
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source.")],
        proposal_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$", description="ID of the pending proposal (the first argument of its wrapper, or from create_proposals).")],
        replacement: Annotated[str, Field(max_length=1000000, description="The complete new proposed text; empty proposes deletion.")],
        explanation: Annotated[str, Field(min_length=1, max_length=100000, description="Why the change is needed.")],
        conversation_id: Annotated[str, Field(pattern=UUID_FIELD, description="This conversation's UUID, as for create_proposals.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call, as for create_proposals.")],
        figures: Annotated[list[ProposalFigure] | None, Field(default=None, max_length=20, description=FIGURES_FIELD + " On a revision a figure of the same name that this proposal added is replaced.")] = None,
    ) -> CallToolResult:
        """Change the proposed text and explanation of a pending proposal; its original text, ID, timestamp and
        conversation stay as created. Never accepts or rejects it. figures adds or replaces this proposal's SVG drawings.
        The two halves of a card move (IDs ending in .cut and .paste, from move_review_card) cannot be revised
        (move_proposal): withdraw the move with remove_proposal and move the card again."""
        payload = {"command": "revise-proposal", "source": source, "expected_source_sha256": expected_source_sha256,
                   "proposal_id": proposal_id, "replacement": replacement, "explanation": explanation,
                   "conversation_id": conversation_id, "request_id": request_id}
        if figures:
            payload["figures"] = [f.model_dump() for f in figures]
        return await author(payload)

    withdraw_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False, idempotentHint=True)

    @mcp.tool(name="remove_proposal", title="Withdraw a pending proposal", annotations=withdraw_tool)
    async def remove_proposal(
        source: Annotated[str, Field(min_length=1, description="Source path from read_source.")],
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source; a changed source is refused (source_conflict).")],
        proposal_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$", description="ID of the pending proposal to withdraw (the first argument of its wrapper, or from create_proposals).")],
        explanation: Annotated[str, Field(min_length=1, max_length=100000, description="Why the proposal is withdrawn.")],
        conversation_id: Annotated[str, Field(pattern=UUID_FIELD, description="This conversation's UUID, as for create_proposals.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call, as for create_proposals.")],
    ) -> CallToolResult:
        """Withdraw a pending agent proposal: its wrapper is removed and its captured original text restored, exactly
        as Reject would leave the note. Use it to take back a proposal you made, for example one that breaks the build;
        other proposals are kept. It is not a decision about the user's text: the user still accepts or rejects
        proposals in Thoughts, and Thoughts records this one as withdrawn by the agent. Withdrawing either half of a
        card move (an ID ending in .cut or .paste, from move_review_card) withdraws both halves, also when the other
        is in another source, and the card stays where it was. Retry an uncertain call only with identical arguments
        and the same request_id."""
        return await author({"command": "remove-proposal", "source": source, "expected_source_sha256": expected_source_sha256,
                             "proposal_id": proposal_id, "explanation": explanation,
                             "conversation_id": conversation_id, "request_id": request_id})

    @mcp.tool(name="create_document", title="Create a new note in a Project", annotations=proposal_tool)
    @with_rules
    async def create_document(
        project: Annotated[str, Field(min_length=1, max_length=512, description="The Library Project: its exact name (the project list_documents shows) or its id.")],
        name: Annotated[str, Field(min_length=1, max_length=200, description="Title of the new document as the Library shows it (no line breaks).")],
        filename: Annotated[str, Field(min_length=4, max_length=900, description="Path of the new file inside the Project's default folder, ending in .md or .markdown (Markdown) or .tex (LaTeX), e.g. attention.md or notes/attention.md. Relative; no segment starting with '.', no '..'. A path that exists is refused.")],
        content: Annotated[str, Field(min_length=1, max_length=4194304, description="The whole note, at most 4 MiB of UTF-8: Markdown (headings, lists, $math$), or a complete standalone LaTeX document (\\documentclass, \\begin{document} ... \\end{document}). Every sentence of prose on its own line.")],
        conversation_id: Annotated[str, Field(pattern=UUID_FIELD, description="Lowercase UUID of this conversation, as for create_proposals: your Claude Code or Codex session ID if you have one; otherwise the one random UUID you use for every call of this conversation.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it then finishes the same note and never creates a second one.")],
    ) -> CallToolResult:
        """Create a NEW LaTeX or Markdown note in one of the user's Library Projects. Use it only when the user asks
        for a new note. It only creates new documents and never overwrites: a filename that already exists is
        refused (source_exists), and existing notes are changed only with create_proposals. Write every sentence of
        prose on its own line. The note joins the Project under name, builds with the next render, and shows on the
        phone and Mac after their next Sync; read it back with read_source like any note. Every note gets \\PdfLink
        from the build (no \\providecommand needed). The content gets the checks of create_proposals (links, figure
        paths, Markdown emoji and < in math)."""
        return await create_note({"command": "create-document", "project": project, "name": name, "filename": filename,
                                  "content": content, "conversation_id": conversation_id, "request_id": request_id})

    async def create_note(payload: dict) -> CallToolResult:
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return result(f"Not done: {error}. A retry with identical arguments and the same request_id never creates "
                          "a second note.", {"accepted": False, "error_code": "not_allowed", "message": str(error)},
                          error=True)
        if not body.get("accepted"):
            if body.get("retryable"):  # its message says to retry with the same request_id
                return result(f"Not confirmed ({body.get('error_code')}), the note may or may not have been created: "
                              f"{body.get('message')}", body, error=True)
            return result(f"The note was not created ({body.get('error_code')}): {body.get('message')}", body, error=True)
        # The new source and the Project's new document: read them fresh next time.
        thoughts.cached.pop("sources", None)
        thoughts.catalogue = None
        builds = ("Thoughts builds its PDF now" if body.get("published", True)
                  else "it is unpublished for now, because its PDF has no build inputs (see the warning)")
        lines = [f"Created document \"{body['name']}\" in Project \"{body['project']}\" ({body['source']}, document id "
                 f"{body['document_id']})" + (" (replayed: already created by this request_id)" if body.get("replayed") else "")
                 + f"; {builds}, and the phone and Mac show it after their next Sync."]
        lines += [f"Warning: {w['message']}" for w in body.get("warnings") or []]
        core.log(f"document created: {body['source']} in {body['project']!r}")
        return result("\n".join(lines), body)

    @mcp.tool(name="build_status", title="Why a document's PDF does not build", annotations=readonly)
    async def build_status(
        document: Annotated[str, Field(min_length=1, max_length=1024, description="The document: its id, its name, its PDF filename or its source path (as list_documents or read_source show them).")],
    ) -> CallToolResult:
        """Why a document's PDF does not build, in one call: its render status, the error of its newest build with the
        source file and line the server located, the LaTeX error lines of the build log, the links the build left
        inactive, the files changed since the last PDF that built, and likely_causes: the pending proposals of the
        failing sources ranked (the one holding the error line, the one holding what the error names, one that breaks
        a rule such as a proposal inside \\hypersetup or a ../ figure path), each with its reasons and the fix
        (revise_proposal, or remove_proposal and a proposal over the whole command). One broken note can stop every PDF
        of its Project, so ask about the PDF that fails. Only reads."""
        try:
            doc = await find(document)
            wanted = doc.get("document_id") or document
        except LookupError:
            wanted = document
        try:
            body = await asyncio.to_thread(proposal_client().run, {"command": "build-status", "document": wanted})
        except ThoughtsError as error:
            return result(f"Not available: {error}", {"accepted": False, "error_code": "not_allowed",
                                                      "message": str(error)}, error=True)
        if not body.get("accepted"):
            return result(f"No build status ({body.get('error_code')}): {body.get('message')}", body, error=True)
        lines = [body["message"]]
        for error in body.get("errors") or []:
            where = f"{error['source']}:{error['line']}: " if error.get("source") and error.get("line") else ""
            lines.append(f"- error: {where}{error.get('detail') or error.get('message')}")
        for shown in body.get("error_lines") or []:
            lines.append(f"- line {shown['line']} of {shown['source']}: {shown['text']}")
        for cause in body.get("likely_causes") or []:
            lines.append(f"- proposal {cause['proposal_id']} (lines {cause['start_line']}-{cause['end_line']}, "
                         f"{cause['created_at']}, score {cause['score']}): {'; '.join(cause['reasons'])}"
                         + (f". Fix: {cause['fix']}" if cause.get("fix") else ""))
        for warning in body.get("link_warnings") or []:
            lines.append(f"- inactive link: {warning}")
        return result("\n".join(lines), body)

    move_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True)

    @mcp.tool(name="move_pdf", title="Move an imported PDF to another Project", annotations=move_tool)
    async def move_pdf(
        pdf: Annotated[str, Field(min_length=1, max_length=1024, description="The imported PDF: its document id, its Library filename (with or without .pdf) or its name.")],
        project: Annotated[str, Field(min_length=1, max_length=512, description="The destination Project: its exact name or its id.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it never moves twice.")],
    ) -> CallToolResult:
        """Move an imported PDF (a paper) to another Library Project, as Thoughts' Move to Project does: it lands in
        that Project's default folder and its review cards move with it; moving it back undoes it. Only when the user
        asks to move a PDF. Links to it need no edit: \\PdfLink and \\CrossPdfLink find a Library PDF by its filename
        wherever it is; the answer lists the notes that name it. The phone and Mac see the move after their next
        Sync."""
        try:
            body = await asyncio.to_thread(proposal_client().run, {"command": "move-pdf", "pdf": pdf,
                                                                   "project": project, "request_id": request_id})
        except ThoughtsError as error:
            return result(f"Nothing moved: {error}", {"accepted": False, "error_code": "not_allowed",
                                                      "message": str(error)}, error=True)
        if not body.get("accepted"):
            state = "it may or may not have moved" if body.get("retryable") else "nothing moved"
            return result(f"Not moved ({body.get('error_code')}; {state}): {body.get('message')}", body, error=True)
        thoughts.catalogue = None
        thoughts.cached.pop("sources", None)
        lines = [body["message"] + (" (replayed: already moved by this request_id)" if body.get("replayed") else "")]
        if body.get("linking_sources"):
            lines.append("Notes that name it: " + ", ".join(body["linking_sources"]))
        core.log(f"pdf moved: {body.get('filename')} {body.get('from_project')!r} -> {body.get('to_project')!r}")
        return result("\n".join(lines), body)

    async def project_folders(payload: dict, what: str) -> tuple[dict | None, CallToolResult | None]:
        """Run a thoughts-cli project-folder command; (answer, None) when accepted, else (None, the refusal)."""
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return None, result(f"Nothing changed: {error}", {"accepted": False, "error_code": "not_allowed",
                                                              "message": str(error)}, error=True)
        if not body.get("accepted"):
            state = f"it may or may not have {what}" if body.get("retryable") else "nothing changed"
            return None, result(f"Not done ({body.get('error_code')}; {state}): {body.get('message')}", body,
                                error=True)
        if body.get("state") != "move_planned":
            thoughts.catalogue = None
            thoughts.cached.pop("sources", None)
        return body, None

    def folder_lines(body: dict) -> list[str]:
        folders = body.get("source_folders") or []
        return ["Its docs folders: " + ", ".join(f"{f['folder']} ({f['name']})" for f in folders)] if folders else []

    @mcp.tool(name="create_project", title="Create a Library Project", annotations=move_tool)
    async def create_project(
        name: Annotated[str, Field(min_length=1, max_length=200, description="The new Project's name (no line breaks); no other Project may have it (case-insensitive).")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. It names the Project (new:<request_id>), so a retry with the same request_id finds it and never creates a second one.")],
        source_folder: Annotated[bool, Field(description="Give the Project a docs folder of its own (docs-<name>-<hex>), as Import folder on the Mac does: its notes can then have interactive HTML pages and agent figures, and notes of other Projects can move into it. False: only the hidden default folder.")] = True,
        folder_name: Annotated[str | None, Field(default=None, min_length=1, max_length=200, description="The folder's label in the Library; default: the Project's name.")] = None,
    ) -> CallToolResult:
        """Create a new Library Project, when the user asks for one. By default it gets a docs folder of its own, so
        notes can be moved into it with move_note and its notes can have interactive pages. A name another Project
        has is refused (project_exists). The phone and Mac show it after their next Sync. To add notes, use
        create_document (a new note) or move_note (an existing note of another Project)."""
        payload = {"command": "create-project", "name": name, "request_id": request_id, "source_folder": source_folder}
        if folder_name is not None:
            payload["folder_name"] = folder_name
        body, refused = await project_folders(payload, "been created")
        if refused is not None:
            return refused
        core.log(f"project created: {body.get('project')!r} {body.get('project_id')}")
        return result("\n".join([body["message"] + (" (replayed: already created by this request_id)"
                                                    if body.get("replayed") else "")] + folder_lines(body)), body)

    @mcp.tool(name="add_source_folder", title="Give a Project a docs folder of its own", annotations=move_tool)
    async def add_source_folder(
        project: Annotated[str, Field(min_length=1, max_length=512, description="The Project: its exact name or its id.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it never adds two folders.")],
        name: Annotated[str | None, Field(default=None, min_length=1, max_length=200, description="The folder's label in the Library; default: the Project's name. Two folders of a Project cannot share a label.")] = None,
    ) -> CallToolResult:
        """Give an existing Project one more docs folder of its own (docs-<label>-<hex>, empty but for its
        .ThoughtIgnore), as Import folder on the Mac does: for a Project that has only its default folder, so notes
        can move into it and have interactive pages. Only when the user asks for it, or before moving notes into
        such a Project (move_note also creates the folder by itself when the Project has none). The phone and Mac
        show it after their next Sync."""
        payload = {"command": "add-source-folder", "project": project, "request_id": request_id}
        if name is not None:
            payload["name"] = name
        body, refused = await project_folders(payload, "been added")
        if refused is not None:
            return refused
        core.log(f"source folder added: {body.get('folder')} to {body.get('project')!r}")
        return result("\n".join([body["message"] + (" (replayed: already added by this request_id)"
                                                    if body.get("replayed") else "")] + folder_lines(body)), body)

    @mcp.tool(name="move_note", title="Move a note to another Project", annotations=move_tool)
    async def move_note(
        document: Annotated[str, Field(min_length=1, max_length=1024, description="The note (LaTeX or Markdown) to move: its document id, its source path (as read_source takes it) or its exact name.")],
        project: Annotated[str, Field(min_length=1, max_length=512, description="The destination Project: its exact name or its id.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it never moves twice.")],
        dry_run: Annotated[bool, Field(description="Only show what the move would do (files, links, pages, warnings); nothing moves.")] = False,
        destination_path: Annotated[str | None, Field(default=None, min_length=1, max_length=900, description="Its path inside the destination's docs folder; default: the same as now (e.g. tex/source/x.tex). Give one when the answer says the path is taken.")] = None,
    ) -> CallToolResult:
        """Move a note of a docs folder to another Library Project, as Thoughts' Move to project does, when the user
        asks to move or regroup notes. The note keeps its identity and PDF; its review cards and comments follow; the
        files only it uses move and shared ones are copied; links to and from it are rewritten
        (\\SourceLink <-> \\CrossSourceLink) in every note; its interactive pages (thoughts-html: links) are listed
        in the new folder too; the style and bibliography it needs come along. A Project with only its default
        folder gets a docs folder of its own with the move. Refused, with nothing moved: notes of a Project's default
        folder (.thoughts/projects/...), a taken path (give destination_path), and moves that need files only the Mac
        holds (needs_mac: the user moves it on the Mac). Use dry_run first when unsure. The phone and Mac see the
        move after their next Sync."""
        payload = {"command": "move-document", "document": document, "project": project, "request_id": request_id,
                   "dry_run": dry_run}
        if destination_path is not None:
            payload["destination_path"] = destination_path
        body, refused = await project_folders(payload, "moved")
        if refused is not None:
            return refused
        lines = [body["message"] + (" (replayed: already moved by this request_id)" if body.get("replayed") else "")]
        lines.append(f"{body['from']} -> {body['to']}" + (" (a new docs folder)" if body.get("creates_folder") else ""))
        for item in body.get("files") or []:
            lines.append(f"{item['action']}: {item['source']} -> {item['destination']}")
        if body.get("edited_sources"):
            lines.append("Links rewritten in: " + ", ".join(body["edited_sources"]))
        if body.get("html_pages"):
            lines.append("Interactive pages listed in the new folder: " + ", ".join(body["html_pages"]))
        lines += [f"Warning: {w}" for w in body.get("warnings") or []]
        if body.get("state") == "document_moved":
            core.log(f"note moved: {body.get('from')} -> {body.get('to')}")
        return result("\n".join(lines), body)

    import_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True, idempotentHint=True)

    @mcp.tool(name="import_pdf", title="Import PDF into a Thoughts project", annotations=import_tool)
    async def import_pdf(
        project: Annotated[str, Field(min_length=1, max_length=2000, description="Project name (case-insensitive) or id; list_documents shows each document's project.")],
        name: Annotated[str, Field(min_length=1, max_length=150, description="Document name shown in the Library, e.g. the paper title.")],
        url: Annotated[str, Field(min_length=1, max_length=4096, description="An https URL that returns the PDF (for arXiv the /pdf/ link, not /abs/); it is kept as the document's source link.")],
    ) -> CallToolResult:
        """Import a PDF (a paper) into a Library project from a URL, exactly as Import document in the Library does.

        The Thoughts server downloads it itself (https on a public host, at most 100 MiB) and adds it to the
        project; the Mac and the phone get it with their next Sync. Use it when the user asks to add a paper or PDF
        to Thoughts, instead of giving them a file to import by hand. Importing a PDF the project already has
        changes nothing and reports replayed: true, so a retry is safe. The result names the document (document_id,
        filename) for links (\\PdfLink{<filename without .pdf>}{document-start}{text}) and review cards."""
        def refused(code: str, message: str, state: str = "nothing imported") -> CallToolResult:
            return result(f"Not imported ({code}; {state}): {message}",
                          {"accepted": False, "error_code": code, "message": message}, error=True)

        try:
            capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
        except ThoughtsError as error:
            return refused("not_available", str(error))
        if IMPORT_CAPABILITY not in capabilities:
            return refused("not_supported", "this Thoughts server cannot import from a URL yet (it lacks "
                           f"{IMPORT_CAPABILITY}); ask the user to import the PDF in Thoughts' Library.")
        try:
            response = await thoughts.request("POST", "/v1/library/url-imports", timeout=IMPORT_SECONDS, refusals=True,
                                              body={"project": project, "name": name, "url": url})
        except ThoughtsError as error:
            return refused("not_allowed", str(error))
        except Exception as error:  # noqa: BLE001  (a timeout or a dropped connection: the import may have happened)
            return refused("unknown", f"{type(error).__name__}: {error}. Call import_pdf again with the same "
                           "arguments: a PDF the project already has is reported as replayed.",
                           "it may or may not have been imported")
        body = response.json()
        if response.status_code >= 400 or not body.get("accepted"):
            code = body.get("error_code") or "refused"
            return result(f"Not imported ({code}; nothing imported): {body.get('detail') or body.get('message')}",
                          {**body, "accepted": False, "error_code": code}, error=True)
        thoughts.catalogue = None
        thoughts.cached.pop("sources", None)
        key = body["filename"].removesuffix(".pdf")
        if body.get("replayed"):
            text = (f"{body['project']} already has this PDF: {body.get('name')} ({body['filename']}, document_id "
                    f"{body['document_id']}); nothing changed (replayed).")
        else:
            text = (f"Imported {body.get('name')} into {body['project']} as {body['filename']} (document_id "
                    f"{body['document_id']}, {body.get('page_count')} page(s)). The Mac and the phone get it with their "
                    "next Sync.")
        text += f" Link it with \\PdfLink{{{key}}}{{document-start}}{{{body.get('name')}}}."
        core.log(f"pdf imported: {body['filename']} into {body['project']!r} from {url[:200]}"
                 + (" (replayed)" if body.get("replayed") else ""))
        return result(text, body)

    async def page_folder(value: str) -> str:
        """A docs-* folder from a folder name, a path inside it, or a document id or name (its source's folder)."""
        head = value.strip().strip("/").split("/", 1)[0]
        if FOLDER.fullmatch(head):
            return head
        doc = await find(value)
        head = (doc.get("source_file") or "").split("/", 1)[0]
        if not FOLDER.fullmatch(head):
            raise LookupError(f"{doc['name']} is not in a docs-* folder, and only docs-* folders have HTML pages")
        return head

    @mcp.tool(name="html_pages", title="A folder's interactive HTML pages", annotations=readonly)
    async def html_pages(
        folder: Annotated[str, Field(min_length=1, max_length=900, description="The docs-* source folder (e.g. docs-notes-0a1b2c), a path in it, or a document id or name.")],
        path: Annotated[str, Field(default="", max_length=900, description="Optional: one page's path; its HTML is returned too.")] = "",
    ) -> CallToolResult:
        """The interactive HTML pages of a docs folder (self-contained .html files at <folder>/tex/support/html/,
        opened from a note's thoughts-html: link in a sandboxed viewer with no network access), each with the link a
        note of that folder uses. With path, also that page's HTML (to read it before replacing it with
        add_html_page). A page not yet in the folder's list exists because the Mac's build inputs carry it."""
        name = await page_folder(folder)
        listing = (await thoughts.request("GET", "/v1/html-pages", params={"folder": name})).json()
        rows = [{**page, "listed": listed, "link": "thoughts-html:" + page["path"]}
                for listed, key in ((True, "pages"), (False, "unlisted_pages")) for page in listing.get(key, [])]
        rows.sort(key=lambda page: page["path"])
        structured = {"folder": name, "pages": rows}
        lines = [f"{len(rows)} HTML page(s) in {name}:"] + [
            f"- {page['path']} ({page['size_bytes']} bytes{'' if page['listed'] else ', from the Mac build inputs'}): "
            f"link thoughts-html:{page['path']}" for page in rows]
        if path:
            page = next((row for row in rows if row["path"] == path), None)
            if page is None:
                raise LookupError(f"{name} has no HTML page {path!r}")
            html = (await thoughts.object(page["sha256"])).decode("utf-8", "replace")
            if len(html) > 2_000_000:
                lines.append(f"{path} is {len(html)} characters, too long to show here.")
            else:
                structured["html"] = html
                lines += [f"--- {path} ---", html]
        return result("\n".join(lines), structured)

    page_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True)

    @mcp.tool(name="add_html_page", title="Add an interactive HTML page to a folder", annotations=page_tool)
    async def add_html_page(
        folder: Annotated[str, Field(min_length=1, max_length=900, description="The docs-* folder of the note that will link the page (e.g. docs-notes-0a1b2c), a path in it such as the note's source path, or the note's document id or name.")],
        name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,122}$", description="File name of the page, letters, digits, '.', '_' and '-' (.html is added if missing), e.g. slope-explorer.")],
        html: Annotated[str, Field(min_length=1, max_length=20971520, description="The complete self-contained page (at most 20 MiB of UTF-8): scripts, styles, fonts, images and data embedded inline. The viewer allows no network access, forms, frames or storage.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments.")],
        replace: Annotated[bool, Field(default=False, description="Replace a page of that name. Read it first with html_pages; without replace an existing page is refused (html_page_exists).")] = False,
    ) -> CallToolResult:
        """Add (or, with replace, replace) one interactive HTML page in a docs folder, as
        <folder>/tex/support/html/<name>.html. Use it when the user wants an interactive figure, explorer or small
        simulation next to a note. It does not change any note: then call create_proposals on a note of the same folder
        and put the link in the replacement, LaTeX \\href{thoughts-html:<path>}{text}, Markdown
        [text](thoughts-html:<path>), for example next to the figure it explains. The Mac installs the page into the
        folder with its next Sync, and every PDF of the folder carries it once rebuilt; the user opens it from the link.
        A page another device changed meanwhile is never overwritten (html_page_changed: read it again)."""
        try:
            target = await page_folder(folder)
            body = await asyncio.to_thread(proposal_client().run, {
                "command": "add-html-page", "folder": target, "name": name, "html": html, "replace": replace,
                "request_id": request_id})
        except (ThoughtsError, LookupError) as error:
            return result(f"Nothing was saved: {error}", {"accepted": False, "error_code": "not_allowed",
                                                           "message": str(error)}, error=True)
        if not body.get("accepted"):
            return result(f"Nothing was saved ({body.get('error_code')}): {body.get('message')}", body, error=True)
        lines = [f"{'Replaced' if body.get('replaced') else 'Saved'} HTML page {body['path']} ({body['size_bytes']} bytes)"
                 + ("" if body.get("changed") else " (it already had this content)")
                 + f". Link it from a note of {body['folder']} with create_proposals: LaTeX {body['latex']}, Markdown "
                 f"{body['markdown']}. The Mac and phone receive it with their next Sync; a note's PDF carries it once rebuilt."]
        lines += [f"Warning: {w['message']}" for w in body.get("warnings") or []]
        core.log(f"html page: {'replaced' if body.get('replaced') else 'saved'} {body['path']}")
        return result("\n".join(lines), body)

    card_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True)
    LineBoundary = Annotated[int, Field(strict=True, ge=1, le=2147483647)]

    @mcp.tool(name="create_review_card", title="Create a review card from a note's passage", annotations=card_tool)
    async def create_review_card(
        source: Annotated[str, Field(min_length=1, description="Source path from read_source: the main .tex or .md source of a Library document.")],
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source; a changed source is refused (source_conflict).")],
        question: Annotated[str, Field(min_length=1, max_length=500, description="The card's question; the selected passage (or section) is its answer.")],
        due_at: Annotated[str, Field(description="First review time as ISO 8601 with a timezone, for example 2026-11-05T09:00:00+01:00; now or later.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call; it becomes the card's ID. Retry an uncertain call only with identical arguments and the same request_id; it never makes a second card.")],
        start_line: Annotated[LineBoundary | None, Field(description="One-based first complete source line of the answer. Omit it for a section card.")] = None,
        end_line: Annotated[LineBoundary | None, Field(description="Exclusive end line of the continuous answer range. Omit it for a section card.")] = None,
        section_line: Annotated[LineBoundary | None, Field(description="Section card: the one-based line of the section's heading (\\section, \\subsection or \\subsubsection, starred or not; in Markdown # to ######). Instead of start_line/end_line and section_heading.")] = None,
        section_heading: Annotated[str | None, Field(min_length=1, max_length=500, description="Section card: the heading's text as read_source shows it, for example Results and discussion. Instead of section_line when only one heading reads so.")] = None,
        kind: Annotated[Literal["knowledge", "project"] | None, Field(description="The card's kind: \"knowledge\" (material the user studies, long spaced repetition) or \"project\" (about work they are writing or building: reviewed at least weekly and again when its passage changes). Leave it out to use the kind of the note's Library Project (usually what the user wants).")] = None,
    ) -> CallToolResult:
        """Create a review card whose answer is a passage of one of the user's notes, as the Mac's Create card does.
        Use it only when the user asks for a card. Read the note with read_source first and select complete lines
        (start_line, end_line): whole paragraphs or blocks with text, in LaTeX inside the document body and outside
        figures, tables, math and unclosed groups, in Markdown whole top-level blocks (a list entirely); not inside a
        pending proposal or another card's passage. Thoughts marks the passage in the source with the card's markers
        (they do not change the PDF) and saves the approved card in the same step; the card's PDF areas are placed
        when the note is next built, and the phone and Mac show it after their next Sync. The card is due at due_at.

        Section card: when the user asks for a card of a whole section or subsection, name its heading with
        section_line (or section_heading) instead of start_line/end_line, as the Mac's Add Review Card on a heading
        does. The card then hides the section's body, from below the heading (after its \\label or anchor lines) to
        just before the next heading of the same or a higher level (in LaTeX also \\appendix, the bibliography or
        \\end{document}), across pages, with its figures; the heading stays visible as the cue. Cards already inside
        the section stay as they are and are hidden with it (the result's section.wrapped_cards counts them); a card
        whose passage runs past the section's end is refused (invalid_selection). \\paragraph headings run into their
        text and are refused, as is a heading whose line goes on with text. The result's section gives the title,
        kind and the body's lines. A thoughts-cli without section cards answers invalid_request."""
        payload = {"command": "create-review-card", "source": source, "expected_source_sha256": expected_source_sha256,
                   "question": question, "due_at": due_at, "request_id": request_id}
        if kind:
            payload["kind"] = kind
        for name, value in (("start_line", start_line), ("end_line", end_line), ("section_line", section_line),
                            ("section_heading", section_heading)):
            if value is not None:
                payload[name] = value
        if (start_line is None) != (end_line is None) or (start_line is None) == (section_line is None and section_heading is None) \
                or (section_line is not None and section_heading is not None):
            message = ("Give either start_line and end_line (a passage) or one of section_line and section_heading "
                       "(a section card).")
            return result(f"Nothing was written (invalid_request): {message}",
                          {"accepted": False, "error_code": "invalid_request", "message": message}, error=True)
        return await card_command(payload)

    @mcp.tool(name="move_review_card", title="Move a review card", annotations=card_tool)
    async def move_review_card(
        card_id: Annotated[str, Field(pattern=UUID_FIELD, description="Card to move (card_id from review_cards or get_review_card).")],
        source: Annotated[str, Field(min_length=1, description="Source path from read_source: the .tex or .md source to move it into (for a card without markers, the note's main source).")],
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source; a changed source is refused (source_conflict).")],
        start_line: Annotated[LineBoundary, Field(description="Card without source markers: one-based first complete source line of its new answer. Card linked to markers: the line before which its marked passage is inserted (equal to end_line; lines + 1 appends).")],
        end_line: Annotated[LineBoundary, Field(description="Card without source markers: exclusive end line of the continuous answer range. Card linked to markers: equal to start_line.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id.")],
        explanation: Annotated[str | None, Field(min_length=1, max_length=100000, description="Card linked to markers only: why the passage moves, shown with both proposals. Do not write the card's ID in it.")] = None,
        conversation_id: Annotated[str | None, Field(pattern=UUID_FIELD, description="Card linked to markers only: this conversation's UUID, as for create_proposals, recorded in both proposals.")] = None,
    ) -> CallToolResult:
        """Move a review card to another place of the Library; it keeps its ID, question, review history and schedule.

        Card linked to source markers (its passage stands between \\ReviewCardMaskStart/End lines, in Markdown
        between <!-- thoughts:v1:start/end review:... --> lines): give start_line equal to end_line, the line of
        `source` before which the passage goes, with explanation and conversation_id. Its marked passage, markers
        included, then moves there: `source` may be the card's own source, another source of the same folder, or a
        source of another folder or Project, of the same format (LaTeX to LaTeX, Markdown to Markdown). Read the
        destination with read_source first. Nothing moves yet: Thoughts writes two linked pending proposals, one
        removing the passage where it is (agent-<uuid>.cut) and one inserting it there (agent-<uuid>.paste), and the
        user accepts or rejects them as one in Thoughts (deciding either decides both). Never cut and paste a card's
        passage with create_proposals, and never ask the user to cut and paste it by hand. remove_proposal on either
        half withdraws both; they cannot be revised. Choose a line between complete paragraphs or blocks of the
        document body: the call fails with invalid_destination if the markers would not read back there after
        acceptance, and with invalid_link if a link in the passage would not resolve from its new place (propose a
        fix of those links first). A passage that intersects a pending proposal cannot move (proposal_overlap), nor
        one that holds only some of another card's markers (split_card); other cards wholly inside the passage move
        with it (also_moved_card_ids). Other errors: pdf_only_card (the card has no markers: select its new answer
        with start_line < end_line), unsupported_card, format_mismatch, invalid_range, no_change, source_conflict,
        generated_source, server_unsupported or not_supported (an older Thoughts server or thoughts-cli: only then
        does the user move the passage by hand).

        Card without source markers yet (for example a card on an imported paper's PDF): give start_line < end_line,
        the complete lines of the note's main source that become its answer, marked like create_review_card (same
        selection rules), at once, as the Mac's move-review-card does. Use it only when the user asks to move a card.
        A card linked to markers is refused with a line range (linked_card): call again with start_line equal to
        end_line as above.

        The new home of a card without markers may be text that is still a pending agent proposal: select the lines
        of its proposed text (in read_source, the last argument of \\agentProposedEdit, or the text between a
        Markdown proposal's header and its end comment; the wrapper's own "}{" or closing "}" on those lines is fine,
        its explanation and original text are not). Then nothing changes yet: the result is state
        waiting_for_proposal with the proposal_id and a pending_move_id, and Thoughts moves the card by itself when
        the user accepts that proposal, exactly as it would move it onto the accepted text. Do not ask the user to
        accept first or to call again. If the user rejects the proposal or it is withdrawn, or revised so that those
        lines are gone, or the card is deleted or moved another way first, the move is dropped and the card stays
        where it was. pending_card_moves shows such moves (also in get_review_card) and cancel_pending_card_move
        cancels one. An older Thoughts server refuses lines inside a pending proposal (invalid_selection)."""
        payload = {"command": "move-review-card", "card_id": card_id, "source": source,
                   "expected_source_sha256": expected_source_sha256, "start_line": start_line,
                   "end_line": end_line, "request_id": request_id}
        if start_line == end_line:
            def refused(code: str, message: str) -> CallToolResult:
                return result(f"Nothing was written ({code}): {message}",
                              {"accepted": False, "error_code": code, "message": message}, error=True)

            if explanation is None or conversation_id is None:
                return refused("invalid_request", "Moving a card linked to source markers (start_line equal to "
                               "end_line) needs explanation and conversation_id.")
            try:
                capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
                commands = load_thoughts_cli().client.COMMANDS
            except ThoughtsError as error:
                return refused("not_available", str(error))
            if PASSAGE_MOVE_CAPABILITY not in capabilities:
                return refused("server_unsupported", "this Thoughts server cannot decide the two halves of a card move "
                               f"together yet (it lacks {PASSAGE_MOVE_CAPABILITY}); until it can, the user moves the "
                               "card by cutting its marked passage and pasting it into the other source.")
            if PASSAGE_MOVE_COMMAND not in commands:
                return refused("not_supported", "the thoughts-cli installed next to the connector cannot move a card "
                               f"linked to source markers yet (it needs {PASSAGE_MOVE_COMMAND}, thoughts-cli 1.7.0); "
                               "until it can, the user moves the card by cutting its marked passage and pasting it "
                               "into the other source.")
            payload.update(explanation=explanation, conversation_id=conversation_id)
        return await card_command(payload)

    merge_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False, idempotentHint=True)
    CardId = Annotated[str, Field(pattern=UUID_FIELD)]

    @mcp.tool(name="merge_review_cards", title="Merge review cards into one", annotations=merge_tool)
    async def merge_review_cards(
        card_ids: Annotated[list[CardId], Field(min_length=2, max_length=20, description="The cards to merge (card_id from review_cards or get_review_card): two or more cards of one document whose passages are marked in `source`, or two or more cards of one imported PDF.")],
        request_id: Annotated[str, Field(pattern=UUID_FIELD, description="Fresh random UUID for this call. Retry an uncertain call only with identical arguments and the same request_id; it never merges twice.")],
        source: Annotated[str | None, Field(min_length=1, description="Source path from read_source: the file that holds every card's markers. Leave it out for cards of an imported PDF.")] = None,
        expected_source_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", description="source_sha256 from read_source; a changed source is refused (source_conflict). Leave it out for cards of an imported PDF.")] = None,
        keep_card_id: Annotated[CardId | None, Field(description="The card that stays, with its ID, schedule and review history; one of card_ids. Default: the one with the most reviews.")] = None,
        question: Annotated[str | None, Field(min_length=1, max_length=500, description="The merged card's question. Default: the cards' questions joined in reading order.")] = None,
    ) -> CallToolResult:
        """Merge two or more review cards into one, as Merge cards… in the Thoughts app does.

        The kept card keeps its ID, schedule and review history, takes `question`, and gets every card's marked
        passage as its own answer area, in reading order; text between the passages (figures, other paragraphs)
        stays out of the card. The other cards are deleted with their markers and history. It is one change, made at
        once by the Thoughts server: no proposals and nothing for the user to accept piece by piece; never merge cards
        with a deletion and an edit, or with proposals that move markers. Use it only when the user asked for these
        cards to be merged. Read the source with read_source first. The phone and Mac get the merge with their next
        Sync; the kept card's PDF areas are placed when the note is next built.
        Cards of an imported PDF (no source; the answer is highlighted in the PDF) can be merged too: leave out source
        and expected_source_sha256, and their areas become the kept card's areas, in reading order; the PDF is unchanged.
        Returns kind (source or pdf), kept_card_id, deleted_card_ids, card_order (reading order), question, passages
        (areas, for a PDF) and the source's new source_sha256 (null for a PDF). Errors: merge_refused with the reason
        (cards of several documents or source files, cards of a note with cards of a PDF, cards on different PDF
        versions, a card without answer areas yet), card_unavailable, source_conflict (read the source again),
        invalid_source, invalid_request, request_conflict, not_supported (an older Thoughts server)."""
        def refused(code: str, message: str) -> CallToolResult:
            return result(f"Not merged ({code}; nothing changed): {message}",
                          {"accepted": False, "error_code": code, "message": message}, error=True)

        if (source is None) != (expected_source_sha256 is None):
            return refused("invalid_request", "give both source and expected_source_sha256 for cards marked in a "
                           "note, or leave both out for cards of an imported PDF.")
        pdf = source is None  # cards of an imported PDF: no source fields
        try:
            capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
        except ThoughtsError as error:
            return refused("not_available", str(error))
        if MERGE_CAPABILITY not in capabilities:
            return refused("not_supported", f"this Thoughts server cannot merge review cards yet (it lacks "
                           f"{MERGE_CAPABILITY}); the user can merge them with Merge cards… in Thoughts on the Mac.")
        if pdf and MERGE_PDF_CAPABILITY not in capabilities:
            return refused("not_supported", f"this Thoughts server cannot merge cards of a PDF yet (it lacks "
                           f"{MERGE_PDF_CAPABILITY}); the user can merge them with Merge cards… in Thoughts on the Mac.")
        try:
            cli = load_thoughts_cli()
            commands = cli.client.COMMANDS
        except ThoughtsError as error:
            return refused("not_available", str(error))
        if "merge-review-cards" not in commands:
            return refused("not_supported", "the thoughts-cli installed next to the connector cannot merge review cards "
                           "yet (it needs merge-review-cards); the user can merge them with Merge cards… in Thoughts on the Mac.")
        if pdf and not hasattr(getattr(cli, "card_merges", None), "is_pdf_merge"):
            return refused("not_supported", "the thoughts-cli installed next to the connector cannot merge cards of a "
                           "PDF yet (it needs thoughts-cli 1.8.0); the user can merge them with Merge cards… in Thoughts "
                           "on the Mac.")
        payload = {"command": "merge-review-cards", "card_ids": card_ids, "request_id": request_id}
        if not pdf:
            payload.update(source=source, expected_source_sha256=expected_source_sha256)
        if keep_card_id is not None:
            payload["keep_card_id"] = keep_card_id
        if question is not None:
            payload["question"] = question
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return refused("not_allowed", str(error))
        if not body.get("accepted"):
            hint = (f" The current source_sha256 is {body['current_source_sha256']}; read the source again."
                    if body.get("current_source_sha256") else "")
            if body.get("retryable"):  # its message says to retry with the same request_id
                return result(f"Not confirmed ({body.get('error_code')}), the cards may or may not have been merged: "
                              f"{body.get('message')}", body, error=True)
            return result(f"Not merged ({body.get('error_code')}; nothing changed): {body.get('message')}{hint}",
                          body, error=True)
        # The merged source and cards: read them fresh next time.
        thoughts.cached.pop("sources", None)
        thoughts.cached.pop("projections", None)
        if body.get("kind") == "pdf":
            core.log(f"review cards merged into {body['kept_card_id']} on {body['document']}: "
                     f"{len(body['card_order'])} cards" + (" (replayed)" if body.get("replayed") else ""))
            return result(
                f"Merged {len(body['card_order'])} cards of {body['document']} into card {body['kept_card_id']}"
                + (" (replayed: already merged by this request_id)" if body.get("replayed") else "")
                + f": question \u201c{body['question']}\u201d, {body['passages']} area(s) on the PDF. Deleted: "
                f"{', '.join(body['deleted_card_ids'])}. The kept card keeps its schedule and review history; nothing "
                "in the PDF changed, and the phone and Mac show the merge after their next Sync.", body)
        lines = [f"Merged {len(body['card_order'])} cards of {body['document']} into card {body['kept_card_id']}"
                 + (" (replayed: already merged by this request_id)" if body.get("replayed") else "")
                 + f": question \u201c{body['question']}\u201d, {body['passages']} passage(s) marked in {body['source']} "
                 f"(new source_sha256 {body['source_sha256']}). Deleted with their markers: "
                 f"{', '.join(body['deleted_card_ids'])}. The kept card keeps its schedule and review history; its PDF "
                 "areas are placed when the note is next built, and the phone and Mac show the merge after their next Sync."]
        if (body.get("render_check") or {}).get("status") == "failed":
            lines.append(f"Warning: the rebuild could not be requested: {body['render_check'].get('message')}")
        core.log(f"review cards merged into {body['kept_card_id']} in {body['source']}: {len(body['card_order'])} cards"
                 + (" (replayed)" if body.get("replayed") else ""))
        return result("\n".join(lines), body)

    async def card_command(payload: dict) -> CallToolResult:
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return result(f"Nothing was written: {error}", {"accepted": False, "error_code": "not_allowed",
                                                             "message": str(error)}, error=True)
        if not body.get("accepted"):
            hint = (f" The current source_sha256 is {body['current_source_sha256']}; read the source again."
                    if body.get("current_source_sha256") else "")
            if body.get("retryable"):  # its message says to retry with the same request_id
                return result(f"Not confirmed ({body.get('error_code')}), the card may or may not have been written: "
                              f"{body.get('message')}", body, error=True)
            return result(f"Nothing was written ({body.get('error_code')}): {body.get('message')}{hint}", body, error=True)
        if body.get("state") == "waiting_for_proposal":
            core.log(f"review card: {body['card_id']} waits for proposal {body['proposal_id']} in {body['source']}"
                     + (" (replayed)" if body.get("replayed") else ""))
            return result(waiting_text(body), body)
        if body.get("state") == "move_proposed":
            thoughts.cached.pop("sources", None)
            core.log(f"review card: move of {body['card_id']} proposed, {body['origin_source']} -> {body['source']}"
                     + (" (replayed)" if body.get("replayed") else ""))
            return result(move_proposed_text(body), body)
        # The marked source and the new or moved card: read them fresh next time.
        thoughts.cached.pop("sources", None)
        thoughts.cached.pop("projections", None)
        moved = body["state"] == "card_moved"
        lines = [(f"Moved card {body['card_id']} from {body['from_document']} into {body['document']}" if moved
                  else f"Created card {body['card_id']} in {body['document']}, due {body['due_at']}")
                 + (" (replayed: already written by this request_id)" if body.get("replayed") else "")
                 + f"; its passage is marked in {body['source']} (new source_sha256 {body['source_sha256']}). "
                 + "Its PDF areas are placed when the note is next built; the phone and Mac show it after their next Sync."
                 + ({"project": " It is a project card", "knowledge": " It is a knowledge card"}[body["review_kind"]]
                    + (" (its Project's kind)." if body.get("review_kind_source") == "project" else ".")
                    if body.get("review_kind") in ("knowledge", "project") and not moved else "")]
        if isinstance(body.get("section"), dict):
            section = body["section"]
            lines.append(f"It hides the {section.get('kind', 'section')} “{section.get('title')}” (lines "
                         f"{section.get('start_line')}–{int(section.get('end_line', 1)) - 1} before the markers); the "
                         "heading stays visible"
                         + (f", and the {section['wrapped_cards']} card(s) inside it stay as they are"
                            if section.get("wrapped_cards") else "") + ".")
        if (body.get("render_check") or {}).get("status") == "failed":
            lines.append(f"Warning: the rebuild could not be requested: {body['render_check'].get('message')}")
        core.log(f"review card: {body['state']} {body['card_id']} in {body['source']}")
        return result("\n".join(lines), body)

    async def pending_moves_supported() -> str | None:
        """Why pending card moves cannot be used here, or None."""
        try:
            capabilities = (await thoughts.ttl("health", "/v1/health")).get("capabilities") or []
        except ThoughtsError as error:
            return str(error)
        if PENDING_MOVES_CAPABILITY not in capabilities:
            return f"this Thoughts server cannot keep card moves that wait for a proposal yet ({PENDING_MOVES_CAPABILITY})"
        try:
            commands = load_thoughts_cli().client.COMMANDS
        except ThoughtsError as error:
            return str(error)
        if "pending-card-moves" not in commands:
            return "the thoughts-cli installed next to the connector cannot list pending card moves yet"
        return None

    @mcp.tool(name="pending_card_moves", title="Card moves waiting for a proposal", annotations=readonly)
    async def pending_card_moves(
        card_id: Annotated[str | None, Field(pattern=UUID_FIELD, description="Only this card's moves.")] = None,
        state: Annotated[Literal["waiting_for_proposal", "moved", "dropped", "cancelled", "all"], Field(
            description="waiting_for_proposal (default): moves still waiting; or those made, dropped, cancelled, or all.")]
            = "waiting_for_proposal",
    ) -> CallToolResult:
        """Review card moves that wait for the user to accept a pending agent proposal (move_review_card onto a
        proposal's text), newest first: card, proposal, the note and the target lines' text, and once settled why
        (moved; dropped because the proposal was rejected, withdrawn or revised, or the card was deleted, moved or
        linked another way first; cancelled). Reads only."""
        if (problem := await pending_moves_supported()) is not None:
            return result(f"Not available (not_supported): {problem}.", {"accepted": False, "error_code": "not_supported",
                                                                          "message": problem}, error=True)
        payload = {"command": "pending-card-moves", "state": state}
        if card_id:
            payload["card_id"] = card_id
        try:
            body = await asyncio.to_thread(proposal_client().run, payload)
        except ThoughtsError as error:
            return result(f"Not available: {error}", {"accepted": False, "error_code": "not_allowed",
                                                       "message": str(error)}, error=True)
        if not body.get("accepted"):
            return result(f"Not available ({body.get('error_code')}): {body.get('message')}", body, error=True)
        moves = body.get("pending_moves") or []
        lines = [f"{len(moves)} pending card move(s)" + (f" ({state})" if state != "waiting_for_proposal" else "") + ":"]
        lines += [pending_move_line(move) for move in moves]
        return result("\n".join(lines), body)

    cancel_tool = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True)

    @mcp.tool(name="cancel_pending_card_move", title="Cancel a card move waiting for a proposal", annotations=cancel_tool)
    async def cancel_pending_card_move(
        pending_move_id: Annotated[str, Field(pattern=UUID_FIELD, description="pending_move_id from move_review_card or pending_card_moves.")],
    ) -> CallToolResult:
        """Cancel a review card move that waits for a pending proposal; the card stays where it is and the proposal
        is not touched. A move already made cannot be cancelled (already_moved): move the card again instead."""
        if (problem := await pending_moves_supported()) is not None:
            return result(f"Nothing was cancelled (not_supported): {problem}.",
                          {"accepted": False, "error_code": "not_supported", "message": problem}, error=True)
        try:
            body = await asyncio.to_thread(proposal_client().run, {"command": "cancel-pending-card-move",
                                                                   "pending_move_id": pending_move_id})
        except ThoughtsError as error:
            return result(f"Nothing was cancelled: {error}", {"accepted": False, "error_code": "not_allowed",
                                                               "message": str(error)}, error=True)
        if not body.get("accepted"):
            return result(f"Nothing was cancelled ({body.get('error_code')}): {body.get('message')}", body, error=True)
        core.log(f"pending card move {pending_move_id}: cancelled" + (" (already settled)" if body.get("already_settled")
                                                                       else ""))
        text = (f"The move was already {body['state']}; nothing changed." if body.get("already_settled") else
                f"Cancelled: card {body['card_id']} stays on {body['from_document']}; proposal {body['proposal_id']} is "
                "untouched.")
        return result(text, body)

    asgi = core.app(ctx)
    asgi.thoughts, asgi.provider = thoughts, ctx.provider
    return asgi


if __name__ == "__main__":
    core.main(SERVICE, create_app, sys.argv)
