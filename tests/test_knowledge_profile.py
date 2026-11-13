# SPDX-License-Identifier: GPL-2.0-or-later
"""knowledge_profile against a fake Thoughts server, through the connector's own Thoughts wrapper.

The cases are those of the Mac's autotests/reviewknowledgetest.cpp (thoughts-desktop) for
knowledge-profile, on the same Library: two projects, a TeX and a Markdown source with card
markers, cards, answers, a tag merge and the tag groups, served as /v1/projections,
/v1/library/projects, /v1/sources and /v1/objects records. Plus what is particular to the server:
only GET requests to the routes the connector may use, a mapping passed in, and deletions the
server inferred.
"""
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("mcp")
import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_profile as knowledge  # noqa: E402
import thoughts_connector  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
GRPO = "00000000-0000-4000-8000-0000000000a1"
BASELINE = "00000000-0000-4000-8000-0000000000a2"
TENSOR = "00000000-0000-4000-8000-0000000000a3"
DELETED_RECORD = "00000000-0000-4000-8000-0000000000a4"
DELETED_BY_EVENT = "00000000-0000-4000-8000-0000000000a5"
PPO = "00000000-0000-4000-8000-0000000000a6"
KL = "00000000-0000-4000-8000-0000000000a7"
# The mapping of the tags before the 2026-10-15 reorganization (the Mac test's "legacy" Levels).
LEGACY = {"correct": "solid", "Memorized": "solid", "Kind of memorized": "shaky", "I kind of get it": "shaky", "wrong": "unknown"}


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def card(card_id, pdf_stem, question, source_file="", line=0) -> dict:
    primary = {"pdf_stem": pdf_stem, "pdf_sha256": "a" * 64, "page": 1, "rect": [0.1, 0.2, 0.8, 0.6],
               "page_visual_sha256": "b" * 64, "question": question, "document_id": "doc-" + pdf_stem}
    if source_file:
        primary["source_anchor"] = {"version": 2, "segments": [
            {"segment_id": "0", "source_file": source_file, "line_start": line, "line_end": line}]}
    return {"schema_version": 3, "card_type": "visual_region", "card_id": card_id, "status": "approved", "revision": 1,
            "primary": primary, "initial_due_at": iso(NOW + timedelta(days=5))}


def answer(event_id, card_id, days_ago, interval_days, tags=None, notes=None, action="") -> dict:
    at = NOW - timedelta(days=days_ago)
    event = {"event_id": event_id, "card_id": card_id, "origin_id": "test", "reviewed_at": iso(at),
             "rating": "custom_interval", "scheduler_version": 2, "schema_version": 1,
             "scheduled_due_at": iso(at + timedelta(milliseconds=int(interval_days * 86400000)))}
    if tags is not None:
        event["review_tags"] = tags
    if notes:
        event["review_group_notes"] = notes
    if action:
        event["review_action"] = action
    return event


def queue_action(event_id, card_id, action, hours_ago, undone="") -> dict:
    event = {"schema_version": 3, "event_id": event_id, "card_id": card_id, "queue_action": action,
             "reviewed_at": iso(NOW - timedelta(hours=hours_ago)), "origin_id": "test"}
    if undone:
        event["undone_event_id"] = undone
    return event


def merge(merge_id, to, sources, created_at="2026-10-15T00:00:00Z") -> dict:
    return {"merge_id": merge_id, "from": sources, "to": to, "created_at": created_at, "origin_id": "test"}


def long_parallel_text() -> str:
    return "Tensor parallel layers split each weight matrix across devices. " * 40 + \
        "Every layer ends with an allreduce of the partial outputs."


class Library:
    """The Mac test's fixture as the Thoughts server serves it."""

    def __init__(self):
        self.cards = {}
        self.events = {}
        self.merges = {}
        self.groups = None
        self.extra = []  # Other projection records, as they are.
        self.projects = []
        for index, (name, stems) in enumerate((("RL", ["rl_grpo", "rl_ppo"]), ("Systems", ["sys_parallel"])), 1):
            self.projects.append({"id": "source:docs-" + name.lower(), "name": name, "revision": 1, "documents": [
                {"document_id": f"00000000-0000-4000-a{index:03d}-{number:012d}", "filename": stem + ".pdf",
                 "name": "GRPO notes" if stem == "rl_grpo" else stem, "included": True, "published": True}
                for number, stem in enumerate(stems, 1)], "unpublished_documents": []})
        self.sources = {
            "rl/grpo.tex": f"\\section{{GRPO}}\n\\ReviewCardMaskStart{{{GRPO}}}{{0}}%\nThe advantage normalizes each reward "
                           f"by the group mean and standard deviation.\n\\ReviewCardMaskEnd{{{GRPO}}}{{0}}%\n",
            "sys/parallel.md": f"# Parallelism\n<!-- thoughts:v1:start review:{TENSOR}:answer:0 -->\n{long_parallel_text()}\n"
                               f"<!-- thoughts:v1:end review:{TENSOR}:answer:0 -->\n",
        }
        self.add_card(card(GRPO, "rl_grpo", "How does GRPO compute the group-relative advantage?", "rl/grpo.tex", 3))
        baseline = card(BASELINE, "rl_grpo", "Why is a baseline needed?")
        baseline["note"] = "Compare with the GRPO advantage"
        baseline["initial_due_at"] = iso(NOW - timedelta(days=3))
        baseline["primary"]["answer_anchor"] = {"normalized_text": "a baseline reduces the variance of the gradient"}
        self.add_card(baseline)
        self.add_card(card(TENSOR, "sys_parallel", "What does tensor parallelism split?", "sys/parallel.md", 3))
        self.add_card({**card(DELETED_RECORD, "rl_grpo", "GRPO card deleted in its record"), "deleted": True})
        self.add_card(card(DELETED_BY_EVENT, "rl_grpo", "GRPO card deleted by an event"))
        self.add_card(card(PPO, "rl_ppo", "What does the PPO clipping ratio bound?"))
        self.add_card(card(KL, "rl_ppo", "Why add a KL penalty to the reward?"))
        # GRPO: half-known, then known under a tag merged into Memorized.
        self.add_event(answer("e1", GRPO, 30, 5, ["Kind of memorized", "interesting"],
                              {"g-understanding": "almost", "note": "mixed up the baseline"}))
        self.add_event(answer("e2", GRPO, 3, 25, ["Got it"]))
        # Tensor parallelism: right long ago (legacy, no tags), now wrong.
        self.add_event(answer("e3", TENSOR, 40, 7, action="correct"))
        self.add_event(answer("e4", TENSOR, 1, 0.9, ["wrong"]))
        # PPO: answered without a tag, with a long interval.
        self.add_event(answer("e5", PPO, 5, 40, []))
        # KL: half understood.
        self.add_event(answer("e6", KL, 4, 7, ["I kind of get it"]))
        self.add_event({"event_id": "e7", "card_id": DELETED_BY_EVENT, "origin_id": "test",
                        "reviewed_at": iso(NOW - timedelta(days=1)), "rating": "delete"})
        self.merges["m1"] = merge("m1", "Memorized", ["Got it"], "2026-10-01T00:00:00Z")
        self.groups = {"layout_id": "groups", "schema_version": 1, "updated_at": "2026-10-01T00:00:00.000Z", "origin_id": "test",
                       "groups": [{"group_id": "g-understanding", "name": "Understanding",
                                   "tags": ["correct", "wrong", "Memorized", "Kind of memorized", "I kind of get it", "Totally new tag"]},
                                  {"group_id": "g-interest", "name": "Interest", "tags": ["interesting"]}]}

    def add_card(self, record):
        self.cards[record["card_id"]] = record

    def add_event(self, event):
        self.events[event["event_id"]] = event

    def projections(self) -> list[dict]:
        def record(kind, entity_id, collection, payload):
            return {"entity_type": kind, "entity_id": entity_id, "collection": collection, "revision": 1, "deleted": False,
                    "payload": payload}
        out = [record("review_card", k, "cards", v) for k, v in self.cards.items()]
        out += [record("review_event", k, "events", v) for k, v in self.events.items()]
        out += [record("review_tag_merge", k, "review-tags", v) for k, v in self.merges.items()]
        if self.groups:
            out.append(record("review_tag_groups", "groups", "review-tags", self.groups))
        return out + self.extra


class Server:
    """A fake Thoughts server for the connector's Thoughts wrapper; it records every request."""

    def __init__(self, library: Library, tmp_path: Path):
        self.library, self.requests = library, []
        self.token = tmp_path / "token"
        self.token.write_text("t" * 43 + "\n")
        self.state = tmp_path / "state"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.headers.get("Authorization") != "Bearer " + "t" * 43:
            return httpx.Response(401, json={"detail": "Invalid device token"})
        objects = {hashlib.sha256(text.encode()).hexdigest(): text.encode() for text in self.library.sources.values()}
        path = request.url.path
        if path == "/v1/projections":
            return httpx.Response(200, json={"snapshot_sequence": 7, "projections": self.library.projections()})
        if path == "/v1/library/projects":
            return httpx.Response(200, json={"schema_version": 1, "projects": self.library.projects}, headers={"etag": '"1"'})
        if path == "/v1/sources":
            return httpx.Response(200, json={"sources": [
                {"path": p, "revision": 1, "sha256": hashlib.sha256(t.encode()).hexdigest(),
                 "object_sha256": hashlib.sha256(t.encode()).hexdigest(), "deleted": False} for p, t in self.library.sources.items()]})
        if path.startswith("/v1/objects/") and path.rsplit("/", 1)[1] in objects:
            return httpx.Response(200, content=objects[path.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"detail": "not found"})

    def profile(self, **request) -> dict:
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
                thoughts = thoughts_connector.Thoughts(http, "http://thoughts.test", self.token, self.state)
                return await knowledge.knowledge_profile(thoughts, now=request.pop("now", NOW), **request)
        result = asyncio.run(run())
        json.dumps(result)  # Plain JSON.
        return result


@pytest.fixture
def library():
    return Library()


@pytest.fixture
def server(library, tmp_path):
    return Server(library, tmp_path)


def reason_of(result: dict, level: str, card_id: str) -> str:
    return next((c["reason"] for c in result["cards"][level] if c["card_id"] == card_id), "<missing>")


def ids(result: dict) -> set[str]:
    return {c["card_id"] for cards in result["cards"].values() for c in cards}


def test_profile_classifies_by_tags_recency_and_interval(server):
    # The project name matches every RL card.
    result = server.profile(topic="RL", levels=LEGACY)
    assert result["accepted"], result
    assert result["counts"] == {"solid": 2, "shaky": 1, "unknown": 0, "unreviewed": 1}
    assert result["matched"] == 4 and result["topic"] == "RL"
    # Memorized now, Kind of memorized before: the latest answer weighs most.
    assert reason_of(result, "solid", GRPO) == "last 2 answers: Memorized, Kind of memorized; interval 25 d"
    # No understanding tag, but a long interval.
    assert reason_of(result, "solid", PPO) == "1 answer, none with an understanding tag; interval 40 d"
    assert reason_of(result, "shaky", KL) == "last answer: I kind of get it; interval 7 d"
    assert reason_of(result, "unreviewed", BASELINE) == "never answered; overdue 3 d"
    summary = result["summary"].split("\n")
    assert summary[0] == '"RL": 4 matching review cards: 2 solid, 1 shaky, 0 not understood, 1 not yet reviewed.'
    assert 3 <= len(summary) <= 5
    assert summary[2].startswith("Shaky: Why add a KL penalty")
    assert result["mapping"]["source"] == "given mapping"
    assert result["mapping"]["levels"]["Correct"] == "solid" and result["mapping"]["levels"]["Wrong"] == "unknown"
    assert result["mapping"]["unmapped_understanding_tags"] == ["Totally new tag"]
    # Each card: its question, document (the Library name of its PDF), project and reason.
    grpo = next(c for c in result["cards"]["solid"] if c["card_id"] == GRPO)
    assert grpo == {"card_id": GRPO, "question": "How does GRPO compute the group-relative advantage?",
                    "document": "GRPO notes", "project": "RL", "reason": grpo["reason"]}

    # Wrong now after Correct long ago: not understood.
    tensor = server.profile(topic="tensor parallelism", levels=LEGACY)
    assert tensor["counts"]["unknown"] == 1
    assert tensor["cards"]["unknown"][0]["reason"].startswith("last 2 answers: Wrong, Correct")

    # The user's own levels replace the defaults; the scope narrows the cards.
    custom = server.profile(topic="RL", document="rl_ppo", levels={"I kind of get it": "unknown", "memorized": "ignore"})
    assert custom["counts"] == {"solid": 1, "shaky": 0, "unknown": 1, "unreviewed": 0}
    assert custom["document"] == "rl_ppo" and "project" not in custom

    nothing = server.profile(topic="diffusion", levels=LEGACY)
    assert nothing["accepted"] and nothing["matched"] == 0
    assert nothing["counts"] == {"solid": 0, "shaky": 0, "unknown": 0, "unreviewed": 0}
    assert nothing["summary"] == 'No review cards match "diffusion": there is no record of what the user knows about it.'


def test_default_levels_read_the_slider_steps(server, library):
    # The reorganization merged the old tags into the steps of the Understanding and Recall sliders.
    for merge_id, to, sources in (("r1", "Recalled", ["correct"]), ("r2", "Forgot", ["wrong"]), ("r3", "Know by heart", ["Memorized"]),
                                  ("r4", "Partly", ["I kind of get it"]), ("r5", "Partly recalled", ["Kind of memorized"])):
        library.merges[merge_id] = merge(merge_id, to, sources)
    result = server.profile(topic="RL")
    assert result["accepted"], result
    assert result["counts"] == {"solid": 2, "shaky": 1, "unknown": 0, "unreviewed": 1}
    levels = result["mapping"]["levels"]
    assert result["mapping"]["source"] == "default mapping"
    assert levels["Know by heart"] == "solid" and levels["Forgot"] == "unknown" and "Correct" not in levels
    assert levels == dict(knowledge.DEFAULT_LEVELS)
    # Forgot after Recalled long ago: not understood; the old answers read under the new names.
    tensor = server.profile(topic="tensor parallelism")
    assert tensor["counts"]["unknown"] == 1
    assert tensor["cards"]["unknown"][0]["reason"].startswith("last 2 answers: Forgot, Recalled")


def test_deleted_cards_are_left_out(server):
    # readingTheIndexWritesNothing: "GRPO" finds the GRPO card (solid by its interval: no tag of the
    # default mapping) and the baseline card's note, never the two deleted GRPO cards.
    result = server.profile(topic="GRPO")
    assert result["counts"]["solid"] == 1 and result["counts"]["unreviewed"] == 1
    assert ids(result) == {GRPO, BASELINE}
    assert result["index"] == {"indexed_at": None, "tag_groups_available": True, "cards": 5}
    assert server.profile(topic="deleted")["matched"] == 0


def test_retired_cards_count_with_their_history_and_are_never_overdue(server, library):
    # The overdue new baseline card and the shaky KL card are retired; KL is restored again;
    # the retire of the due tensor card is undone.
    for event in (queue_action("r1", BASELINE, "retire", 24), queue_action("r2", KL, "retire", 5),
                  queue_action("r3", KL, "restore", 4), queue_action("r4", TENSOR, "retire", 3),
                  queue_action("r5", TENSOR, "undo_retire", 2, undone="r4")):
        library.add_event(event)
    result = server.profile(topic="baseline")
    assert result["counts"]["unreviewed"] == 1
    assert result["cards"]["unreviewed"][0]["reason"] == "never answered; retired"
    # Restored and undone: not retired.
    assert reason_of(server.profile(topic="KL penalty", levels=LEGACY), "shaky", KL) == "last answer: I kind of get it; interval 7 d"
    assert reason_of(server.profile(topic="tensor parallelism", levels=LEGACY), "unknown", TENSOR) == "last 2 answers: Wrong, Correct"


def test_the_retirement_rule_is_the_connectors(library):
    # retired_cards (the Mac's ReviewRetire::fold) and thoughts_connector.retired_at agree.
    histories = {
        "a": [queue_action("x1", "a", "retire", 5), queue_action("x2", "a", "undo_retire", 4, undone="x1")],
        "b": [queue_action("x3", "b", "retire", 9), queue_action("x4", "b", "restore", 7), queue_action("x5", "b", "retire", 6)],
        "c": [queue_action("x6", "c", "retire", 3), queue_action("x7", "c", "restore", 2)],
        "d": [queue_action("x8", "d", "retire", 3), queue_action("x9", "d", "restore", 2),
              queue_action("x10", "d", "undo_retire", 1, undone="x9")],
        "e": [answer("x11", "e", 2, 3, ["Recalled"])],
    }
    retired = knowledge.retired_cards([event for history in histories.values() for event in history])
    assert retired == {card_id for card_id, history in histories.items() if thoughts_connector.retired_at(history)} == {"b", "d"}


def test_bad_requests(server):
    def code(**request):
        return server.profile(**request).get("error_code")
    assert code(topic="") == "invalid_request"
    assert code(topic="   ") == "invalid_request"
    assert server.profile(topic="")["message"] == 'topic must name what to profile, for example "GRPO advantage".'
    assert code(topic="x" * 2001) == "invalid_request"
    for limit in (0, 201, 1.5, True, "3"):
        assert code(topic="RL", limit=limit) == "invalid_request", limit
    assert code(topic="RL", project=3) == "invalid_request"
    # Invalid requests are refused before the server is asked anything.
    assert server.requests == []
    unknown = server.profile(topic="RL", project="Nope")
    assert (unknown["accepted"], unknown["error_code"], unknown["projects"]) == (False, "unknown_project", ["RL", "Systems"])
    assert code(topic="RL", document="nope.pdf") == "unknown_document"
    # A known document whose cards do not match the topic is no error.
    other = server.profile(topic="RL", document="sys_parallel")
    assert other["accepted"] and other["matched"] == 0 and other["document"] == "sys_parallel"
    assert code(topic="RL", levels={"Correct": "great"}) == "invalid_levels"
    assert "solid, shaky, unknown, ignore" in server.profile(topic="RL", levels={"Correct": "great"})["message"]
    assert code(topic="RL", levels={"schema_version": 2, "levels": {}}) == "invalid_levels"


def test_scope_and_the_levels_file_format(server):
    # By project name (case-insensitive) or id; a knowledge-tag-levels.json document works as the levels.
    by_name = server.profile(topic="RL", project="rl", levels={"schema_version": 1, "levels": LEGACY})
    by_id = server.profile(topic="RL", project="source:docs-rl", levels=LEGACY)
    assert by_name["counts"] == by_id["counts"] == {"solid": 2, "shaky": 1, "unknown": 0, "unreviewed": 1}
    assert by_name["project"] == "rl"
    assert server.profile(topic="tensor", project="RL")["matched"] == 0
    # The document by its Library name, PDF filename or the card's document_id.
    for document in ("GRPO notes", "rl_grpo.pdf", "doc-rl_grpo"):
        assert ids(server.profile(topic="GRPO", document=document)) == {GRPO, BASELINE}, document


def test_weak_matches_and_the_limit(server):
    # "advantage" is in GRPO's question and answer, and in the baseline card's note: both match.
    assert ids(server.profile(topic="advantage", levels=LEGACY)) == {GRPO, BASELINE}
    limited = server.profile(topic="advantage", limit=1, levels=LEGACY)
    assert limited["matched"] == 2 and sum(limited["counts"].values()) == 1
    assert ids(limited) == {GRPO}
    assert limited["summary"].startswith('"advantage": 1 matching review card: 1 solid, ')
    # With "group" too, the note's single word is a weak match (under a fifth of the best score): left out.
    weak = server.profile(topic="group advantage", levels=LEGACY)
    assert ids(weak) == {GRPO} and weak["matched"] == 1
    # Plural query words find singular text, and stop words are dropped.
    assert ids(server.profile(topic="what are advantages", levels=LEGACY)) == {GRPO, BASELINE}
    # A word only in a long answer far down the source still finds its card.
    assert ids(server.profile(topic="allreduce")) == {TENSOR}


def test_lapses_overdue_and_never_asked_again(server, library):
    # A card relearned twice (Again after an interval), long overdue; and one never to be asked again.
    lapsed = "00000000-0000-4000-8000-0000000000b1"
    library.add_card(card(lapsed, "rl_ppo", "Why does the PPO value loss need clipping?"))
    for event_id, days_ago, rating in (("l1", 90, "easy"), ("l2", 80, "again"), ("l3", 79, "good"), ("l4", 70, "again"),
                                       ("l5", 69, "good")):
        library.add_event({"event_id": event_id, "card_id": lapsed, "reviewed_at": iso(NOW - timedelta(days=days_ago)),
                           "rating": rating, "scheduler_version": 2, "review_tags": ["Recalled"], "origin_id": "test"})
    retained = "00000000-0000-4000-8000-0000000000b2"
    library.add_card(card(retained, "rl_ppo", "Who proposed PPO clipping?"))
    library.add_event({"event_id": "n1", "card_id": retained, "reviewed_at": iso(NOW - timedelta(days=2)), "rating": "retained",
                       "review_tags": ["I get it"], "origin_id": "test"})
    result = server.profile(topic="PPO clipping")
    # Recalled every time (1.0), 2 lapses (-0.2), relearned to a 1-day interval and 68 days overdue (-0.15): 0.65.
    assert reason_of(result, "shaky", lapsed) == "last 3 answers: Recalled, Recalled, Recalled; interval 1 d; 2 lapses; overdue 68 d"
    assert reason_of(result, "solid", retained) == "last answer: I get it; never asked again"


def test_skips_retractions_and_inferred_deletions(server, library):
    skip = {"schema_version": 3, "event_id": "00000000-0000-4000-8000-0000000000c1", "card_id": BASELINE, "queue_action": "skip",
            "skip_id": "00000000-0000-4000-8000-0000000000c2", "reviewed_at": iso(NOW - timedelta(hours=1)),
            "skip_until": iso(NOW + timedelta(days=1)), "origin_id": "test"}
    library.add_event(skip)
    # A skipped card is not due, so not overdue.
    assert reason_of(server.profile(topic="baseline needed"), "unreviewed", BASELINE) == "never answered"
    # An undone answer no longer counts: KL's only answer is retracted.
    library.add_event({"event_id": "u1", "card_id": KL, "reviewed_at": iso(NOW - timedelta(days=3)), "retracted_event_id": "e6",
                       "origin_id": "test"})
    assert reason_of(server.profile(topic="KL penalty"), "unreviewed", KL) == "never answered"
    # The server inferred a deletion when the markers left the source: the card stays, as on the Mac.
    library.add_event({"event_id": "d1", "card_id": TENSOR, "reviewed_at": iso(NOW - timedelta(hours=2)), "rating": "delete",
                       "origin_id": "thoughts-source-review", "reason": "source_markers_removed"})
    assert TENSOR in ids(server.profile(topic="tensor parallelism"))
    library.add_event({"event_id": "d2", "card_id": TENSOR, "reviewed_at": iso(NOW - timedelta(hours=1)), "rating": "delete",
                       "origin_id": "mac-okular"})
    assert server.profile(topic="tensor parallelism")["matched"] == 0


def test_cards_the_mac_leaves_out_and_unknown_documents(server, library):
    # Candidates, rejected cards, invalid records and a record whose ID is not its projection's.
    for number, change in enumerate(({"status": "candidate"}, {"status": "rejected"}, {"schema_version": 2}), 1):
        library.add_card({**card(f"00000000-0000-4000-8000-0000000000d{number}", "rl_ppo", f"PPO entropy bonus {number}?"), **change})
    library.cards["00000000-0000-4000-8000-0000000000d9"] = card("00000000-0000-4000-8000-0000000000d8", "rl_ppo", "PPO entropy?")
    # A card whose PDF is in no Project's documents: named by its PDF, no project.
    library.add_card(card("00000000-0000-4000-8000-0000000000e1", "loose_notes", "What is the PPO entropy bonus for?"))
    result = server.profile(topic="PPO entropy")
    assert result["matched"] == 1
    assert result["cards"]["unreviewed"] == [{"card_id": "00000000-0000-4000-8000-0000000000e1",
                                              "question": "What is the PPO entropy bonus for?", "document": "loose_notes",
                                              "reason": "never answered"}]


def test_cards_of_an_unpublished_document_are_never_overdue(server, library):
    # protocol/project-document-publication.md: kept, with their history, but offered for nothing.
    library.projects[0]["unpublished_documents"].append(
        {"document_id": "doc-hidden", "filename": "hidden.pdf", "name": "Hidden", "included": False, "published": False})
    hidden = card("00000000-0000-4000-8000-0000000000f1", "hidden", "When is the PPO trust region hidden?")
    hidden["initial_due_at"] = iso(NOW - timedelta(days=4))
    hidden["primary"]["document_id"] = "doc-hidden"
    library.add_card(hidden)
    # Its document by filename when the card names none.
    by_name = card("00000000-0000-4000-8000-0000000000f2", "hidden", "Why is the PPO trust region hidden twice?")
    by_name["initial_due_at"] = iso(NOW - timedelta(days=4))
    del by_name["primary"]["document_id"]
    library.add_card(by_name)
    # A card made in an attachment counts under its PDF's document.
    attached = card("00000000-0000-4000-8000-0000000000f3", "rl_ppo", "Is the hidden PPO attachment trust region due?")
    attached["initial_due_at"] = iso(NOW - timedelta(days=4))
    attached["primary"]["pdf_document_id"] = "doc-hidden"
    library.add_card(attached)
    result = server.profile(topic="hidden trust region")
    assert {c["card_id"]: c["reason"] for c in result["cards"]["unreviewed"]} == {
        hidden["card_id"]: "never answered", by_name["card_id"]: "never answered", attached["card_id"]: "never answered"}
    # Published again: due, and overdue.
    library.projects[0]["documents"].append(library.projects[0]["unpublished_documents"].pop())
    result = server.profile(topic="hidden trust region")
    assert {c["reason"] for c in result["cards"]["unreviewed"]} == {"never answered; overdue 4 d"}


def test_the_unpublished_rule_is_the_connectors(library, tmp_path):
    library.projects[1]["unpublished_documents"] = [{"document_id": "only-here", "filename": "o.pdf", "name": "O"},
                                                     {"document_id": library.projects[0]["documents"][0]["document_id"],
                                                      "filename": "rl_grpo.pdf", "name": "GRPO notes"}]
    server = Server(library, tmp_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
            thoughts = thoughts_connector.Thoughts(http, "http://thoughts.test", server.token, server.state)
            documents = await thoughts.documents()
            return {d for d, doc in documents.items() if not doc["published"]}, knowledge.unpublished_documents(await thoughts.projects())
    published_false, unpublished = asyncio.run(run())
    assert published_false == unpublished == {"only-here"}


def test_reads_only_and_only_allowed_routes(server):
    result = server.profile(topic="GRPO advantage", project="RL")
    assert result["accepted"]
    assert {method for method, _ in server.requests} == {"GET"}
    for method, path in server.requests:
        thoughts_connector.check_request(method, path)  # raises for a route the connector may not use
    assert {path for _, path in server.requests} >= {"/v1/projections", "/v1/library/projects", "/v1/sources"}
    # The JSON of the Mac: keys in order, as a QJsonObject writes them.
    assert list(result) == sorted(result)
    assert list(result) == ["accepted", "cards", "counts", "index", "mapping", "matched", "project", "schema_version",
                            "summary", "topic"]
    assert list(result["cards"]) == ["shaky", "solid", "unknown", "unreviewed"]


def test_words_and_text_helpers():
    assert knowledge.query_tokens("What is the GRPO advantage?") == ["grpo", "advantage"]
    assert knowledge.query_tokens("the of") == ["the", "of"]
    assert knowledge.query_tokens("a 3 b") == ["3"]
    assert knowledge.words_of("rl_grpo Straße ÉTÉ") == ["rl", "grpo", "straße", "été"]
    assert knowledge.word_matches("advantages", "advantage") and knowledge.word_matches("advantage", "advantages")
    assert not knowledge.word_matches("adv", "advantages") and not knowledge.word_matches("ad", "a")
    assert knowledge.short_question("x" * 91) == "x" * 88 + "…" and knowledge.short_question("  a \n b ") == "a b"
    assert knowledge.days_text(25) == "25 d" and knowledge.days_text(0) == "0 d" and knowledge.days_text(0.5) == "12 h"
    passages = knowledge.source_passages(
        f"a\n\\ReviewCardMaskStart{{{GRPO}}}{{1}}%\n\n  second  \n\\ReviewCardMaskEnd{{{GRPO}}}{{1}}%\n"
        f"x \\ReviewCardMaskStart{{{GRPO}}}{{0}}first\\ReviewCardMaskEnd{{{GRPO}}}{{0}} y\n", GRPO)
    assert passages == ["  second", "first"]
    mapping = knowledge.merge_mapping([merge("b", "C", ["B"], "2"), merge("a", "B", ["A"], "1"), merge("c", "", ["X"], "0")])
    assert mapping == {"A": "C", "B": "C"}
