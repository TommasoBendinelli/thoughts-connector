# SPDX-License-Identifier: GPL-2.0-or-later
"""knowledge_profile: what the user knows about a topic, from their review cards (read-only).

A port of the Mac's `thoughtsctl knowledge-profile` (thoughts-desktop shell/reviewknowledge.cpp,
ReviewKnowledge::profile and findCards, with the card fold of shell/reviewstore.cpp: answers,
tags, tag merges, tag groups, the schedule and retirement) to the connector. Same request,
same classification, same JSON.

The Mac reads its review index (review-query.sqlite, the server's projections as of its last
Sync), the Library catalogue and the local sources. Here the same facts come from the Thoughts
server through the connector's `Thoughts` wrapper, with GET requests only: review() (card records
and their events), the review_tag_merge and review_tag_groups records of the same /v1/projections
snapshot (ttl, the call review() makes), projects() (the catalogue: PDF filenames, names,
projects) and source() (the text between a card's markers). Nothing is written.

This module never imports thoughts_connector: the connector runs as `python thoughts_connector.py`,
so a module-level import back would load it a second time (or fail as a circular import). The
retirement rule is therefore the Mac's own (ReviewRetire::fold), which agrees with
thoughts_connector.retired_at for every card that is not deleted.
"""
import logging
import math
import os
import posixpath
import re
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# The user's local time zone: overdue days are counted in local days, as on the Mac. The same
# setting as the connector's diary zone.
ZONE = ZoneInfo(os.environ.get("THOUGHTS_DIARY_ZONE", "Europe/Zurich"))

DEFAULT_LIMIT = 40
MAXIMUM_LIMIT = 200
MAXIMUM_TEXT_LENGTH = 2000
LEVEL_NAMES = ("solid", "shaky", "unknown", "ignore")
CLASSES = ("solid", "shaky", "unknown", "unreviewed")
# ReviewKnowledge::defaultLevels(): the user's slider groups (2026-10-15). Understanding and Recall
# say how well the answer is known; Interest, Study, Material and Source say nothing about it.
DEFAULT_LEVELS = (
    ("I get it", "solid"), ("Deeply", "solid"), ("Recalled", "solid"), ("Know by heart", "solid"),
    ("Partly", "shaky"), ("Partly recalled", "shaky"), ("Don't get it", "unknown"), ("Forgot", "unknown"),
)
STOP_WORDS = frozenset("a an and are as at be by do does for from how in is it its of on or that the this to what "
                       "when which why with".split())

# reviewstore.cpp
CARD_SCHEMA_VERSION = 3
MAXIMUM_QUESTION_LENGTH = 500
MAX_INTERVAL_DAYS = 365
CURRENT_SCHEDULER_VERSION = 2
LEARN_AHEAD_LIMIT = timedelta(minutes=20)
FIRST_LEARNING_STEP_MINUTES = 1
SECOND_LEARNING_STEP_MINUTES = 10
FIRST_LEARNING_HARD_MINUTES = 6
RELEARNING_STEP_MINUTES = 10
RELEARNING_HARD_MINUTES = 15
GRADUATING_INTERVAL_DAYS = 3
EASY_INTERVAL_DAYS = 7
RELEARNING_INTERVAL_DAYS = 1
RATINGS = frozenset({"hard", "good", "easy", "again", "delete", "retained", "ten_minutes", "next_day", "next_week",
                     "custom_interval"})
CORRECT, WRONG, SKIP = "correct", "wrong", "skip"
TAGS_COLLECTION = "review-tags"
GROUPS_ID = "groups"
OTHER_GROUP = "Other"
OTHER_NOTE_KEY = "other"
ANSWER_NOTE_KEY = "note"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- JSON values as Qt reads them

def _str(obj, key: str) -> str:
    """QJsonObject::value(key).toString(): the string, else ""."""
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, str) else ""


def _obj(obj, key: str) -> dict:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


def _list(obj, key: str) -> list:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, list) else []


def _int(value) -> int:
    """QJsonValue::toInt(): a whole number, else 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _number(value) -> float:
    """QJsonValue::toDouble(): a number, else 0."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _qround(value: float) -> int:
    """qRound / std::round for the non-negative values here: halves away from zero."""
    return math.floor(value + 0.5)


# ---------------------------------------------------------------- text as Qt handles it

def fold_case(text: str) -> str:
    """QString::toCaseFolded(): simple case folding, one character for one (ß stays ß)."""
    if text.isascii():
        return text.lower()
    out = []
    for char in text:
        folded = char.casefold()
        if len(folded) != 1:
            folded = char.lower()
            if len(folded) != 1:
                folded = char
        out.append(folded)
    return "".join(out)


def _units(text: str) -> int:
    """QString::size(): UTF-16 code units."""
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def _left(text: str, units: int) -> str:
    """QString::left(units), in UTF-16 code units."""
    if _units(text) <= units:
        return text
    return text.encode("utf-16-le")[:2 * units].decode("utf-16-le", "ignore")


def simplified(text: str) -> str:
    """QString::simplified(): trimmed, each run of whitespace one space."""
    return " ".join(text.split())


# ---------------------------------------------------------------- words and matching

_SEPARATOR = re.compile(r"[\W_]+")  # [^\p{L}\p{N}]+


def words_of(value: str) -> list[str]:
    return [word for word in _SEPARATOR.split(fold_case(value)) if word]


def query_tokens(query: str) -> list[str]:
    """Query words: case-insensitive, without stop words unless nothing else is left."""
    every, meaningful = [], []
    for word in words_of(query):
        if word in every:
            continue
        every.append(word)
        if word not in STOP_WORDS and (_units(word) > 1 or word[0].isdecimal()):
            meaningful.append(word)
    return meaningful or every


def word_matches(word: str, token: str) -> bool:
    """A word matches a query word it equals or starts with ("advantage" finds "advantages"), and a
    plural or inflected query word finds its stem ("advantages" finds "advantage")."""
    if word == token:
        return True
    if _units(token) >= 3 and word.startswith(token):
        return True
    return _units(word) >= 4 and token.startswith(word) and _units(token) - _units(word) <= 2


def field_matches(words: list[str], token: str) -> bool:
    return any(word_matches(word, token) for word in words)


def occurrences(words: list[str], token: str) -> int:
    return sum(1 for word in words if word_matches(word, token))


def phrase_matches(words: list[str], tokens: list[str]) -> bool:
    """Whether the query words appear one after another."""
    if len(tokens) < 2:
        return False
    return any(all(word_matches(words[start + i], tokens[i]) for i in range(len(tokens)))
               for start in range(len(words) - len(tokens) + 1))


# ---------------------------------------------------------------- times

def parse_utc(value) -> datetime | None:
    """QDateTime::fromString(value, Qt::ISODate).toUTC(); a time without an offset is local time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    millisecond = min(999, _qround(parsed.microsecond / 1000))
    return parsed.replace(microsecond=millisecond * 1000).astimezone(timezone.utc)


_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _time_key(at: datetime | None) -> tuple:
    """Sort key: an invalid QDateTime comes before every valid one."""
    return (False, _NEVER) if at is None else (True, at)


def _before(a: datetime | None, b: datetime | None) -> bool:
    """QDateTime a < b: invalid is less than valid, and equal to invalid."""
    if b is None:
        return False
    return a is None or a < b


def _at_or_before(a: datetime | None, b: datetime | None) -> bool:
    return _before(a, b) or a == b


_UUID = re.compile(r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?")


def uuid_text(value) -> str | None:
    """QUuid(value).toString(WithoutBraces), or None for a null (invalid) UUID."""
    if not isinstance(value, str) or not _UUID.fullmatch(value) or value.startswith("{") != value.endswith("}"):
        return None
    text = value.strip("{}").lower()
    return None if text == "00000000-0000-0000-0000-000000000000" else text


# ---------------------------------------------------------------- tags (ReviewTags)

def normalize_tag(text) -> str:
    """Trims and collapses whitespace; the built-in IDs match case-insensitively."""
    tag = _left(simplified(text if isinstance(text, str) else ""), 1000).strip()
    if not tag:
        return ""
    folded = fold_case(tag)
    return next((builtin for builtin in (CORRECT, WRONG) if builtin == folded), tag)


def tag_label(tag: str) -> str:
    return "Correct" if tag == CORRECT else "Wrong" if tag == WRONG else tag


def recorded_tags(event: dict) -> list[str]:
    """ReviewTags::ofEvent: the recorded tags; events from before tagging are mapped from their
    action and rating, and an answer recorded with an empty review_tags list has none."""
    tags = []
    for value in _list(event, "review_tags"):
        tag = normalize_tag(value)
        if tag and fold_case(tag) != SKIP and tag not in tags:
            tags.append(tag)
    if tags:
        return tags
    queue = _str(event, "queue_action")
    if isinstance(event.get("review_tags"), list) and not queue:
        return []
    action, rating = _str(event, "review_action"), _str(event, "rating")
    if queue == SKIP or action == SKIP or _list(event, "review_tags"):
        return []
    if queue or _str(event, "interval_action") or _str(event, "retracted_event_id") or not rating or rating == "delete":
        return []
    if action in ("wrong", "error") or rating in ("again", "ten_minutes"):
        return [WRONG]
    return [CORRECT]


def merge_mapping(merges: list[dict]) -> dict[str, str]:
    """Old tag -> final name; merges apply in (created_at, merge_id) order, as on Android."""
    parsed, seen = [], set()
    for payload in merges:
        merge_id, created_at, to = _str(payload, "merge_id"), _str(payload, "created_at"), _str(payload, "to").strip()
        sources = [value.strip() for value in _list(payload, "from") if isinstance(value, str) and value.strip()]
        if not merge_id.strip() or not to or not sources or merge_id in seen:
            continue
        seen.add(merge_id)
        parsed.append((created_at, merge_id, to, sources))
    parsed.sort(key=lambda merge: (merge[0], merge[1]))
    mapping: dict[str, str] = {}
    for _, _, to, sources in parsed:
        target = mapping.get(to, to)
        for source in sources:
            if source == target:
                continue
            mapping[source] = target
            # Keep every value final, so a later merge of a merge target carries its sources along.
            for key, value in mapping.items():
                if value == source:
                    mapping[key] = target
    return mapping


def resolve(tag: str, mapping: dict[str, str]) -> str:
    return mapping.get(tag, tag)


def apply_merges(tags: list[str], mapping: dict[str, str]) -> list[str]:
    if not mapping:
        return tags
    result = []
    for tag in tags:
        name = resolve(tag, mapping)
        if name not in result:
            result.append(name)
    return result


def group_notes(event: dict) -> dict[str, str]:
    """ReviewTags::groupNotesOfEvent: group_id (or "other", or "note" for the answer's own note) -> note."""
    notes = {}
    for key, value in _obj(event, "review_group_notes").items():
        note = value.strip() if isinstance(value, str) else ""
        if key and note:
            notes[key] = note
    return notes


@dataclass
class TagGroup:
    id: str
    name: str
    tags: list[str] = field(default_factory=list)
    scale: list[str] = field(default_factory=list)
    section: str = ""


def groups_error(groups: list[TagGroup]) -> str:
    """ReviewTags::groupsError: empty when the groups follow the shared schema."""
    if len(groups) > 50:
        return "too many groups"
    ids, names, total = set(), set(), 0
    for group in groups:
        if not group.id.strip() or len(group.id) > 64 or group.id in ids:
            return "a group has an invalid or repeated ID"
        ids.add(group.id)
        name = group.name.strip()
        if not name or len(name) > 80 or fold_case(name) == fold_case(OTHER_GROUP) or fold_case(name) in names:
            return "a group has an invalid or repeated name"
        names.add(fold_case(name))
        if any(not tag.strip() or len(tag) > 200 for tag in group.tags) or len(set(group.tags)) != len(group.tags):
            return "a group lists an invalid or repeated tag"
        if any(step not in group.tags for step in group.scale) or len(set(group.scale)) != len(group.scale):
            return "a slider step is not one of its group's tags"
        if (group.section and not group.section.strip()) or len(group.section.strip()) > 60:
            return "a section name is invalid"
        total += len(group.tags)
    return "too many grouped tags" if total > 1000 else ""


def parse_groups(record: dict) -> list[TagGroup] | None:
    """ReviewTags::parseGroups: the groups exactly as stored, or None when the record is not valid."""
    version = record.get("schema_version")
    if (_str(record, "layout_id") != GROUPS_ID or isinstance(version, bool) or not isinstance(version, (int, float))
            or version != 1 or not isinstance(record.get("groups"), list)):
        return None
    parsed, seen = [], set()
    for value in record["groups"]:
        if not isinstance(value, dict) or not isinstance(value.get("group_id"), str) or not isinstance(value.get("name"), str) \
                or not (isinstance(value.get("tags"), list) or "tags" not in value):
            return None
        group = TagGroup(value["group_id"], value["name"].strip())
        for tag in value.get("tags") or []:
            if not isinstance(tag, str):
                return None
            # A tag listed again, in this group or another, stays in its first group.
            if tag not in seen:
                group.tags.append(tag)
            seen.add(tag)
        if isinstance(value.get("scale"), list):
            for step in value["scale"]:
                if not isinstance(step, str) or not step.strip() or step in group.scale:
                    continue
                if step not in group.tags:
                    if step in seen:
                        continue
                    group.tags.append(step)
                    seen.add(step)
                group.scale.append(step)
        section = _str(value, "section").strip()
        if len(section) <= 60:
            group.section = section
        parsed.append(group)
    return None if groups_error(parsed) else parsed


def effective_groups(groups: list[TagGroup], mapping: dict[str, str]) -> list[TagGroup]:
    """ReviewTags::effectiveGroups: tags under their merged names, each shown once, Skip dropped."""
    def shown_name(stored: str) -> tuple[str, bool]:
        normalized = normalize_tag(stored)
        if not normalized or fold_case(normalized) == SKIP:
            return "", False
        tag = resolve(normalized, mapping)
        return tag, tag != normalized

    owner: dict[str, int] = {}
    for take_renamed in (False, True):
        for index, group in enumerate(groups):
            for stored in group.tags:
                tag, renamed = shown_name(stored)
                if tag and renamed == take_renamed and tag not in owner:
                    owner[tag] = index
    result = []
    for index, group in enumerate(groups):
        shown = TagGroup(group.id, group.name.strip(), section=group.section)
        for stored in group.tags:
            tag, _ = shown_name(stored)
            if tag and owner.get(tag, -1) == index and tag not in shown.tags:
                shown.tags.append(tag)
        for stored in group.scale:
            tag, _ = shown_name(stored)
            if tag and tag in shown.tags and tag not in shown.scale:
                shown.scale.append(tag)
        result.append(shown)
    return result


# ---------------------------------------------------------------- the schedule (ReviewStore::foldEvents)

def is_inferred_source_deletion(event: dict) -> bool:
    """A deletion the server inferred (markers removed from the source, a document deleted): not a
    deletion of the card (review_reanchor._deleted_review_card_ids)."""
    if _str(event, "rating") != "delete":
        return False
    if _str(event, "origin_id") == "thoughts-source-review" and _str(event, "reason") == "source_markers_removed":
        return True
    evidence = _obj(event, "document_deletion")
    return (_str(event, "origin_id") == "thoughts-document-deletion" and _str(event, "reason") == "document_deleted"
            and bool(_list(evidence, "source_files")) and not _list(evidence, "filenames")
            and evidence.get("project_document_removed") is not True)


@dataclass
class Schedule:
    due_at: datetime | None = None
    interval_days: int = 0
    deleted: bool = False
    retained: bool = False
    fixed_interval: bool = False
    learning_step: int = -1
    relearning: bool = False
    previous_interval_days: int = 0
    reviewed: bool = False
    review_count: int = 0
    lapse_count: int = 0
    last_reviewed_at: datetime | None = None


def _whole_days(start: datetime, end: datetime) -> int:
    """QDateTime::secsTo(end) / 86400."""
    return min((end - start) // timedelta(seconds=1) // 86400, 2 ** 31 - 1)


def next_schedule(previous: Schedule, rating: str, at: datetime, has_previous: bool, scheduler_version: int,
                  scheduled_due_at: datetime | None) -> Schedule:
    """ReviewStore::nextSchedule."""
    schedule = Schedule(reviewed=True, review_count=previous.review_count if has_previous else 0,
                        lapse_count=previous.lapse_count if has_previous else 0, last_reviewed_at=at)
    if rating == "delete":
        schedule.deleted, schedule.due_at = True, at
        return schedule
    schedule.review_count += 1
    if rating == "again" and (scheduler_version < CURRENT_SCHEDULER_VERSION or (has_previous and previous.interval_days > 0)):
        schedule.lapse_count += 1
    if rating == "retained":
        schedule.retained, schedule.due_at = True, at
        return schedule
    if rating == "custom_interval":
        if scheduled_due_at is None or scheduled_due_at <= at:
            return previous
        schedule.fixed_interval, schedule.due_at = True, scheduled_due_at
        schedule.interval_days = _whole_days(at, scheduled_due_at)
        return schedule
    if rating in ("ten_minutes", "next_day", "next_week"):
        schedule.fixed_interval = True
        schedule.interval_days = 7 if rating == "next_week" else 1 if rating == "next_day" else 0
        schedule.due_at = at + (timedelta(days=schedule.interval_days) if schedule.interval_days > 0 else timedelta(minutes=10))
        return schedule

    if scheduler_version < CURRENT_SCHEDULER_VERSION:
        if rating == "again":
            days = 0 if not has_previous or previous.deleted else (
                previous.interval_days if previous.interval_days > 0 else previous.previous_interval_days)
            schedule.relearning, schedule.previous_interval_days = days > 0, days
            schedule.due_at = at + timedelta(minutes=10)
            return schedule
        days = 0 if not has_previous or previous.deleted else previous.interval_days
        if rating == "hard":
            schedule.interval_days = 1 if days <= 0 else max(1, _qround(days * 1.2))
        elif rating == "good":
            schedule.interval_days = 3 if days <= 0 else max(days + 1, _qround(days * 2.5))
        elif rating == "easy":
            schedule.interval_days = 7 if days <= 0 else max(days + 1, _qround(days * 4.0))
        schedule.interval_days = min(MAX_INTERVAL_DAYS, schedule.interval_days)
        schedule.due_at = at + timedelta(days=schedule.interval_days)
        return schedule

    learning_step = previous.learning_step if previous.learning_step >= 0 else (
        0 if has_previous and previous.interval_days == 0 else -1)
    is_learning = learning_step >= 0
    previous_review_days = 0 if not has_previous or previous.deleted else (
        previous.previous_interval_days if is_learning else previous.interval_days)

    def learning(delay_minutes: int, step: int, relearning: bool) -> Schedule:
        schedule.interval_days, schedule.learning_step, schedule.relearning = 0, step, relearning
        schedule.previous_interval_days = previous_review_days
        schedule.due_at = at + timedelta(minutes=delay_minutes)
        return schedule

    def review_interval(passing: str) -> int:
        if passing == "hard":
            return max(1, _qround(previous_review_days * 1.2))
        if passing == "good":
            return max(previous_review_days + 1, _qround(previous_review_days * 2.5))
        return max(previous_review_days + 1, _qround(previous_review_days * 4.0))

    def review(days: int) -> Schedule:
        schedule.interval_days = min(MAX_INTERVAL_DAYS, days)
        schedule.due_at = at + timedelta(days=schedule.interval_days)
        return schedule

    if not has_previous or is_learning:
        relearning = has_previous and previous.relearning
        if rating == "again":
            return learning(RELEARNING_STEP_MINUTES if relearning else FIRST_LEARNING_STEP_MINUTES, 0, relearning)
        if rating == "hard":
            delay = RELEARNING_HARD_MINUTES if relearning else (
                SECOND_LEARNING_STEP_MINUTES if learning_step == 1 else FIRST_LEARNING_HARD_MINUTES)
            return learning(delay, learning_step if learning_step >= 0 else 0, relearning)
        if rating == "good":
            if relearning:
                return review(RELEARNING_INTERVAL_DAYS)
            if learning_step == 1:
                return review(GRADUATING_INTERVAL_DAYS)
            return learning(SECOND_LEARNING_STEP_MINUTES, 1, False)
        if rating == "easy":
            return review(review_interval(rating) if relearning and previous_review_days > 0 else EASY_INTERVAL_DAYS)
    if rating == "again":
        return learning(RELEARNING_STEP_MINUTES, 0, True)
    return review(review_interval(rating))


def _event_order(event: dict) -> tuple:
    return _time_key(parse_utc(_str(event, "reviewed_at"))), _str(event, "event_id")


def fold_events(events: list[dict]) -> dict[str, Schedule]:
    """ReviewStore::foldEvents: card_id -> schedule, from the review marks (queue actions aside)."""
    unique: dict[str, dict] = {}
    for event in events:
        if "queue_action" in event or is_inferred_source_deletion(event):
            continue
        event_id = _str(event, "event_id")
        if event_id and event_id not in unique:
            unique[event_id] = event
    retracted = set()
    for retraction in unique.values():
        target_id = _str(retraction, "retracted_event_id")
        target = unique.get(target_id)
        if (target_id and target is not None and _str(retraction, "card_id")
                and _str(retraction, "card_id") == _str(target, "card_id")
                and _str(target, "rating") in RATINGS and _str(target, "rating") != "delete"):
            retracted.add(target_id)
    schedules: dict[str, Schedule] = {}
    for event in sorted(unique.values(), key=_event_order):
        if _str(event, "event_id") in retracted or _str(event, "retracted_event_id"):
            continue
        card_id, rating = _str(event, "card_id"), _str(event, "rating")
        at = parse_utc(_str(event, "reviewed_at"))
        if _str(event, "interval_action") == "set":
            unit = _str(event, "interval_unit")
            due = parse_utc(_str(event, "scheduled_due_at"))
            amount = _number(event.get("interval_amount"))
            never = unit == "never"
            finite = unit in ("minutes", "hours", "days", "weeks", "months", "date")
            if not card_id or at is None or (not never and (not finite or due is None or due <= at or (
                    unit != "date" and (not math.isfinite(amount) or amount <= 0)))):
                continue
            schedule = copy(schedules.get(card_id) or Schedule())
            if schedule.deleted:
                continue
            schedule.retained, schedule.fixed_interval = never, True
            schedule.due_at = at if never else due
            schedule.interval_days = 0 if never else _whole_days(at, due)
            schedule.learning_step, schedule.relearning = -1, False
            schedules[card_id] = schedule
            continue
        if not card_id or rating not in RATINGS or at is None:
            continue
        if card_id in schedules and schedules[card_id].deleted:
            continue
        scheduled_due_at = parse_utc(_str(event, "scheduled_due_at"))
        if rating == "custom_interval" and (scheduled_due_at is None or scheduled_due_at <= at):
            continue
        schedules[card_id] = next_schedule(schedules.get(card_id) or Schedule(), rating, at, card_id in schedules,
                                           _int(event.get("scheduler_version")), scheduled_due_at)
    return schedules


def skipped_until_by_card(events: list[dict]) -> dict[str, datetime]:
    """The latest skip of each card still in effect (not undone): card_id -> skip_until."""
    skips, undone = [], set()
    for event in events:
        action, card_id, skip_id = _str(event, "queue_action"), _str(event, "card_id"), _str(event, "skip_id")
        at = parse_utc(_str(event, "reviewed_at"))
        if (_int(event.get("schema_version")) != 3 or uuid_text(_str(event, "event_id")) is None
                or uuid_text(card_id) is None or uuid_text(skip_id) is None or at is None):
            continue
        key = uuid_text(card_id) + "\n" + uuid_text(skip_id)
        if action == "undo_skip":
            undone.add(key)
        elif action == SKIP:
            until = parse_utc(_str(event, "skip_until"))
            if until is not None and until > at:
                skips.append((card_id, key, until))
    result: dict[str, datetime] = {}
    for card_id, key, until in skips:
        if key not in undone and (card_id not in result or result[card_id] < until):
            result[card_id] = until
    return result


def retired_cards(events: list[dict]) -> set[str]:
    """ReviewRetire::fold: the cards retired now. The latest retire or restore not undone (by
    reviewed_at, then event_id) decides; a deletion wins (thoughts-server protocol/review-card-retire.md)."""
    unique: dict[str, dict] = {}
    for event in events:
        event_id = _str(event, "event_id")
        if event_id and event_id not in unique:
            unique[event_id] = event
    actions: dict[str, tuple] = {}
    deleted, undos = set(), []
    for event in unique.values():
        card_id, action = _str(event, "card_id"), _str(event, "queue_action")
        if not card_id:
            continue
        if not action and _str(event, "rating") == "delete" and not is_inferred_source_deletion(event):
            deleted.add(card_id)
        elif action in ("retire", "restore"):
            at = parse_utc(_str(event, "reviewed_at"))
            if at is not None:
                actions[_str(event, "event_id")] = (at, _str(event, "event_id"), card_id, action)
        elif action == "undo_retire":
            undos.append((card_id, _str(event, "undone_event_id")))
    for card_id, undone in undos:
        if undone in actions and actions[undone][2] == card_id:
            del actions[undone]
    latest: dict[str, tuple] = {}
    for action in actions.values():
        current = latest.get(action[2])
        if current is None or current[:2] < action[:2]:
            latest[action[2]] = action
    return {card_id for card_id, action in latest.items() if action[3] == "retire" and card_id not in deleted}


# ---------------------------------------------------------------- cards (ReviewCard)

def _is_answer(event: dict) -> bool:
    """Not a skip (nor one an older client recorded as a "skip" answer or tag) and not an interval change."""
    if _str(event, "queue_action") or _str(event, "interval_action") or fold_case(_str(event, "review_action")) == SKIP:
        return False
    recorded = _list(event, "review_tags")
    return not (recorded and all(fold_case(normalize_tag(tag)) == SKIP for tag in recorded))


def effective_answers(card_id: str, history: list[dict]) -> list[dict]:
    """Answers still in effect, oldest first: ratings not undone and skips not cancelled."""
    unique: dict[str, dict] = {}
    for event in history:
        if _str(event, "card_id") == card_id:
            unique[_str(event, "event_id")] = event
    retracted, undone = set(), set()
    for event in unique.values():
        target = _str(event, "retracted_event_id")
        if target in unique and _str(unique[target], "rating") in RATINGS and _str(unique[target], "rating") != "delete":
            retracted.add(target)
        if _str(event, "queue_action") == "undo_skip":
            undone.add(_str(event, "skip_id"))
    answers = []
    for event in sorted(unique.values(), key=_event_order):
        if _str(event, "event_id") in retracted:
            continue
        at = parse_utc(_str(event, "reviewed_at"))
        if at is None:
            continue
        queue = _str(event, "queue_action")
        if queue:
            if queue == SKIP and _str(event, "skip_id") not in undone and _before(at, parse_utc(_str(event, "skip_until"))):
                answers.append(event)
            continue
        rating = _str(event, "rating")
        if rating not in RATINGS:
            continue
        if rating == "delete":
            break
        if rating == "custom_interval":
            due = parse_utc(_str(event, "scheduled_due_at"))
            if due is None or due <= at:
                continue
        answers.append(event)
    return answers


@dataclass
class Card:
    record: dict
    schedule: Schedule
    skipped_until: datetime | None
    history: list[dict]
    merges: dict[str, str]
    retired: bool = False
    # Its document is unpublished in its Project: kept, with its schedule, but never offered for review.
    unpublished: bool = False
    answers: list[dict] = field(default_factory=list)  # Still in effect, oldest first (ReviewCard::answers).

    @property
    def id(self) -> str:
        return _str(self.record, "card_id")

    @property
    def primary(self) -> dict:
        return _obj(self.record, "primary")

    @property
    def question(self) -> str:
        return _str(self.primary, "question").strip()

    @property
    def pdf_stem(self) -> str:
        return _str(self.primary, "pdf_stem")

    def tags_of_event(self, event: dict) -> list[str]:
        """An event's tags as shown and counted: recorded tags mapped through the tag merges."""
        return apply_merges(recorded_tags(event), self.merges)

    @property
    def was_answered(self) -> bool:
        return bool(self.answers)

    @property
    def current_tags(self) -> list[str]:
        return self.tags_of_event(self.answers[-1]) if self.answers else []


def valid_card_record(record: dict) -> bool:
    version = _int(record.get("schema_version"))
    question = _str(_obj(record, "primary"), "question").strip()
    return ((version == CARD_SCHEMA_VERSION or (version == 4 and "regions" in _obj(record, "primary")))
            and uuid_text(_str(record, "card_id")) is not None and bool(question) and _units(question) <= MAXIMUM_QUESTION_LENGTH)


def is_due_now(card: Card, now: datetime) -> bool:
    """ReviewStore::isDueNow: whether an active card belongs in the review queue at now."""
    schedule = card.schedule
    if card.retired or _before(now, card.skipped_until):
        return False
    return ((not schedule.reviewed and (schedule.due_at is None or schedule.due_at <= now))
            or (schedule.reviewed and _at_or_before(schedule.due_at, now))
            or (schedule.reviewed and schedule.interval_days == 0 and not schedule.fixed_interval
                and _at_or_before(schedule.due_at, now + LEARN_AHEAD_LIMIT)))


@dataclass
class Due:
    due: bool
    due_at: datetime | None
    overdue_days: int  # Whole local days since the due day, when it was before today.


def due_of(card: Card, now: datetime) -> Due:
    """scheduleOf in reviewknowledge.cpp."""
    schedule = card.schedule
    due_at = card.skipped_until if card.skipped_until is not None and _before(schedule.due_at, card.skipped_until) else schedule.due_at
    due = (_str(card.record, "status") == "approved" and not schedule.retained and not card.retired and not card.unpublished
           and is_due_now(card, now))
    overdue = 0
    if due and due_at is not None:
        due_day, today = due_at.astimezone(ZONE).date(), now.astimezone(ZONE).date()
        overdue = (today - due_day).days if due_day < today else 0
    return Due(due, due_at, overdue)


def unpublished_documents(projects: list[dict]) -> set[str]:
    """The documents only some Project lists unpublished (protocol/project-document-publication.md):
    the rule of Thoughts.documents()'s "published", and of the Mac's unpublishedDocumentIds."""
    listed = {_str(d, "document_id") for p in projects for d in _list(p, "documents")}
    return {_str(d, "document_id") for p in projects for d in _list(p, "unpublished_documents")} - listed - {""}


def card_document_id(card_record: dict, projects: list[dict]) -> str:
    """protocol/project-document-publication.md section 4: the PDF's document for a card made in an
    attachment, else the card's own document, else its Library PDF's (by filename)."""
    primary = _obj(card_record, "primary")
    for key in ("pdf_document_id", "document_id"):
        if _str(primary, key):
            return _str(primary, key)
    filename = _str(primary, "pdf_stem") + ".pdf"
    return next((_str(d, "document_id") for p in projects for key in ("documents", "unpublished_documents")
                 for d in _list(p, key) if _str(d, "filename") == filename), "")


@dataclass
class Library:
    cards: list[Card]
    merges: dict[str, str]
    groups: list[TagGroup]
    projects: list[dict]


def library_of(cards: dict, events: dict, projections: list, projects: list) -> Library:
    """ReviewStore::cards() and libraryFromSnapshot: the cards that exist (not deleted, no
    candidates or rejected cards), with their schedule, answers and tag merges.

    cards and events are what Thoughts.review() returns; projections the /v1/projections records
    (for the tag merges and groups); projects what Thoughts.projects() returns."""
    tag_records: dict[str, list[dict]] = {"review_tag_merge": [], "review_tag_groups": []}
    for projection in projections:
        kind, payload = projection.get("entity_type"), projection.get("payload")
        if kind not in tag_records or projection.get("deleted") or not isinstance(payload, dict):
            continue
        if projection.get("collection", TAGS_COLLECTION) != TAGS_COLLECTION:
            continue
        identity = _str(payload, "merge_id" if kind == "review_tag_merge" else "layout_id")
        if identity and identity == projection.get("entity_id", identity):
            tag_records[kind].append(payload)
    merges = merge_mapping(tag_records["review_tag_merge"])
    layouts = [record for record in tag_records["review_tag_groups"] if _str(record, "layout_id") == GROUPS_ID]
    newest = None
    for record in layouts:
        if newest is None or _str(record, "updated_at") > _str(newest, "updated_at"):
            newest = record
    stored = parse_groups(newest) if newest is not None else None
    groups = effective_groups(stored, merges) if stored else []

    # Every event of every card, in (reviewed_at, event_id) order; an event without its ID is not a
    # valid projection, and an inferred source deletion is not a deletion.
    every = [event for history in events.values() for event in history
             if isinstance(event, dict) and _str(event, "event_id") and not is_inferred_source_deletion(event)]
    every.sort(key=_event_order)
    histories: dict[str, list[dict]] = {}
    for event in every:
        histories.setdefault(_str(event, "card_id"), []).append(event)
    schedules = fold_events(every)
    queue = [event for event in every if not (
        _str(event, "queue_action") == SKIP and _card_reviewed_after(schedules.get(_str(event, "card_id")), event))]
    skipped = skipped_until_by_card(queue)
    retired = retired_cards(every)
    projects = [p for p in projects if isinstance(p, dict)]
    unpublished = unpublished_documents(projects)

    library = []
    for card_id in sorted(cards):
        record = cards[card_id]
        if not isinstance(record, dict) or _str(record, "card_id") != card_id or not valid_card_record(record):
            continue
        status = _str(record, "status")
        if record.get("deleted") is True or status in ("candidate", "rejected"):
            continue
        schedule = copy(schedules.get(card_id) or Schedule())
        if schedule.deleted:
            continue
        if not schedule.reviewed and schedule.due_at is None:
            schedule.due_at = parse_utc(_str(record, "initial_due_at"))
        history = histories.get(card_id, [])
        library.append(Card(record, schedule, skipped.get(card_id), history, merges, retired=card_id in retired,
                            unpublished=bool(unpublished) and card_document_id(record, projects) in unpublished,
                            answers=[event for event in effective_answers(card_id, history) if _is_answer(event)]))
    return Library(library, merges, groups, projects)


def _card_reviewed_after(schedule: Schedule | None, skip: dict) -> bool:
    """A skip recorded before the card's last review no longer hides it."""
    return _before(parse_utc(_str(skip, "reviewed_at")), schedule.last_reviewed_at if schedule else None)


# ---------------------------------------------------------------- the answer as text

def source_files_of(card: Card) -> list[str]:
    """Where the card's markers may be: as "Copy for agent" looks for them."""
    files: list[str] = []

    def add(value) -> None:
        trimmed = value.strip() if isinstance(value, str) else ""
        if trimmed and trimmed not in files:
            files.append(trimmed)

    primary = card.primary
    for value in _list(primary, "source_files"):
        add(value)
    anchor = "source_anchor" if "source_anchor" in primary else "source_selection"
    for segment in _list(_obj(primary, anchor), "segments"):
        add(_str(segment, "source_file"))
    # ReviewCard::linkingSource (the Mac's local source location index aside).
    segments = _list(_obj(primary, "source_anchor"), "segments")
    add(_str(segments[0], "source_file") if segments else _str(_obj(card.record, "source_link"), "source_file"))
    return files


_SPACE = r"[ \t\n\r\f\v]"


def source_passages(content: str, card_id: str) -> list[str]:
    """The text between the card's mask markers (TeX or Markdown) in one source, markers excluded,
    in line order: trailing whitespace trimmed on every line, blank lines just inside the markers dropped."""
    content = content.replace("\r\n", "\n")
    if not content or not card_id or card_id not in content:
        return []
    ident = re.escape(card_id)
    marker = re.compile(rf"\\ReviewCardMask(Start|End){_SPACE}*\{{{ident}\}}{_SPACE}*\{{([0-9]+)\}}%?"
                        rf"|<!--{_SPACE}*thoughts:v1:(start|end){_SPACE}+review:{ident}:answer:([0-9]+){_SPACE}*-->")
    opened: dict[str, int] = {}
    segments = []
    for match in marker.finditer(content):
        tex = match.group(1) is not None
        start = (match.group(1) if tex else match.group(3)).lower() == "start"
        segment = match.group(2) if tex else match.group(4)
        if start:
            opened[segment] = match.end()
            continue
        if segment not in opened:
            continue
        body_start = opened.pop(segment)
        first_line = content.count("\n", 0, body_start) + 1
        body = [line.rstrip() for line in content[body_start:match.start()].split("\n")]
        first, last = 0, len(body) - 1
        while first <= last and not body[first]:
            first += 1
        while last >= first and not body[last]:
            last -= 1
        if first <= last:
            segments.append((first_line + first, "\n".join(body[first:last + 1])))
    segments.sort(key=lambda item: item[0])
    return [text for _, text in segments]


async def answer_text(thoughts, card: Card, cache: dict[str, str]) -> str:
    """The card's answer for matching: the source text between its markers, else its answer
    anchor's text (the Mac never reads PDF text to match a topic)."""
    texts, seen = [], set()
    for file in source_files_of(card):
        path = posixpath.normpath(file)
        if path in seen:
            continue
        seen.add(path)
        if path not in cache:
            try:
                cache[path] = await thoughts.source(path) or ""
            except Exception as error:  # noqa: BLE001  (an unreadable source only loses this answer's words)
                log.warning("knowledge_profile: source %s unreadable (%s: %s)", path, type(error).__name__, error)
                cache[path] = ""
        texts += source_passages(cache[path], card.id)
    if texts:
        return "\n\n".join(texts)
    return _str(_obj(card.primary, "answer_anchor"), "normalized_text")


# ---------------------------------------------------------------- finding the cards (findCards)

@dataclass
class Entry:
    card: Card
    pdf_filename: str
    document_name: str
    document_id: str
    project: str
    current_tags: list[str]
    question: list[str] = field(default_factory=list)
    note: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    answer: list[str] = field(default_factory=list)
    score: float = 0.0


def error(code: str, message: str) -> dict:
    return {"schema_version": 1, "accepted": False, "error_code": code, "message": message}


def project_of_pdf(projects: list[dict], filename: str) -> tuple[dict, dict]:
    """The first Library project listing the PDF (filename or attachment_filename), and its entry there."""
    for project in projects:
        for document in _list(project, "documents"):
            if _str(document, "filename") == filename or _str(document, "attachment_filename") == filename:
                return project, document
    return {}, {}


def find_cards(library: Library, project: str, document: str) -> tuple[list[Entry], dict | None]:
    """The cards of the project and document filters (empty: all), or an error."""
    project_filter = None
    if project:
        names = []
        for candidate in library.projects:
            name = _str(candidate, "name").strip()
            names.append(name)
            if fold_case(name) == fold_case(project) or _str(candidate, "id") == project:
                project_filter = candidate
        if project_filter is None:
            return [], {**error("unknown_project", f'No Library project is called "{project}".'), "projects": names}
    project_files = {_str(d, key) for d in _list(project_filter, "documents") for key in ("filename", "attachment_filename")}
    project_files.discard("")

    wanted = fold_case(document)
    known = not wanted
    entries = []
    for card in library.cards:
        filename = card.pdf_stem + ".pdf"
        owner, listed = project_of_pdf(library.projects, filename)
        name = _str(listed, "name").strip() or card.pdf_stem
        document_id = _str(card.primary, "document_id")
        if wanted:
            if not any(fold_case(value) == wanted for value in (filename, card.pdf_stem, name, document_id)):
                continue
            known = True
        if project_filter is not None and filename not in project_files:
            continue
        entries.append(Entry(card, filename, name, document_id, _str(owner, "name").strip(), card.current_tags))
    if not known:
        found = False
        for candidate in library.projects:
            for listed in _list(candidate, "documents"):
                for key in ("filename", "name", "document_id"):
                    value = fold_case(_str(listed, key))
                    found = found or value == wanted or (key == "filename" and value[:len(value) - min(4, len(value))] == wanted)
        if not found:
            return [], error("unknown_document", f'No Library document is called "{document}"; give its PDF filename, '
                                                 "name or document ID.")
    return entries, None


def rank(entries: list[Entry], tokens: list[str], now: datetime) -> list[Entry]:
    """Best match first (BM25-like); without query words, by due date."""
    if tokens:
        # Rarer words count more (BM25's inverse document frequency); a word in the question counts
        # more than one in the answer.
        count = len(entries)
        rarity = {}
        for token in tokens:
            found = sum(1 for e in entries if any(field_matches(words, token) for words in (e.question, e.note, e.names, e.tags, e.answer)))
            rarity[token] = math.log(1.0 + (count - found + 0.5) / (found + 0.5))
        weights = (3.0, 2.0, 1.5, 1.5, 1.0)  # question, note, document, tags, answer
        for entry in entries:
            fields = (entry.question, entry.note, entry.names, entry.tags, entry.answer)
            found, score = 0, 0.0
            for token in tokens:
                best, seen = 0.0, 0
                for words, weight in zip(fields, weights):
                    hits = occurrences(words, token)
                    if hits:
                        seen += hits
                        best = max(best, weight)
                if best > 0:
                    found += 1
                    # Mentioned again and again: a little more about it.
                    score += rarity[token] * best * (1 + 0.15 * math.log(seen))
            if not found:
                continue
            score *= found / len(tokens)
            if phrase_matches(entry.question, tokens) or phrase_matches(entry.answer, tokens):
                score *= 1.25
            entry.score = _qround(score * 1000) / 1000
        entries = [entry for entry in entries if entry.score > 0]

    def key(entry: Entry) -> tuple:
        # Cards never asked again come last.
        card = entry.card
        due_at = None if card.schedule.retained or card.retired else due_of(card, now).due_at
        return (-entry.score if tokens else 0.0), (due_at is None), (due_at or _NEVER), card.id
    return sorted(entries, key=key)


# ---------------------------------------------------------------- knowledge levels

def load_levels(levels) -> tuple[list[tuple[str, str]], str] | dict:
    """The tag -> level mapping: the default one, or the one given (a {"<tag>": "<level>"} mapping, or a
    knowledge-tag-levels.json document {"schema_version": 1, "levels": {...}}); an error dict when invalid."""
    if levels is None:
        return list(DEFAULT_LEVELS), "default mapping"
    if isinstance(levels, dict) and "levels" in levels:
        if _int(levels.get("schema_version")) != 1 or not isinstance(levels.get("levels"), dict):
            return error("invalid_levels", 'levels is not a knowledge level mapping: expected {"schema_version": 1, '
                                           '"levels": {"<tag>": "solid"|"shaky"|"unknown"|"ignore"}}.')
        levels = levels["levels"]
    if not isinstance(levels, dict):
        return error("invalid_levels", 'levels must map each tag to "solid", "shaky", "unknown" or "ignore".')
    result = []
    # In key order, as a JSON object reads in Qt.
    for tag in sorted(levels):
        level = levels[tag]
        if not isinstance(tag, str) or not tag.strip() or level not in LEVEL_NAMES:
            return error("invalid_levels", f'levels: the level of "{tag}" must be one of {", ".join(LEVEL_NAMES)}.')
        result.append((tag, level))
    return result, "given mapping"


def level_value(level: str) -> float:
    return 1.0 if level == "solid" else 0.5 if level == "shaky" else 0.0


def short_question(question: str) -> str:
    line = simplified(question)
    return _left(line, 88).strip() + "…" if _units(line) > 90 else line


def _number_text(value: float) -> str:
    """QString::arg(double): the shortest form ("25", "2.5")."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def days_text(days: float) -> str:
    if days >= 1 or days == 0:
        return f"{_number_text(_qround(days * 10) / 10)} d"
    return f"{_number_text(max(1.0, _qround(days * 24)))} h"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _sorted(value):
    """Keys in order, as a QJsonObject writes them."""
    if isinstance(value, dict):
        return {key: _sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted(item) for item in value]
    return value


def _optional_text(value, name: str) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if not isinstance(value, str) or _units(value) > MAXIMUM_TEXT_LENGTH:
        return "", f"{name} must be a string of at most {MAXIMUM_TEXT_LENGTH} characters."
    return value.strip(), None


async def knowledge_profile(thoughts, *, topic: str, project: str | None = None, document: str | None = None,
                            limit: int = DEFAULT_LIMIT, now: datetime | None = None, levels: dict | None = None) -> dict:
    """What the user knows about topic, from their review cards (read-only): the Mac's knowledge-profile.

    thoughts is the connector's Thoughts server wrapper (review, ttl, projects and source are read).
    Returns the Mac's JSON (schema_version 1): accepted, topic, matched, counts and cards per class
    (solid, shaky, unknown, unreviewed), summary, index, mapping, and project/document when given;
    or accepted false with error_code (invalid_request, invalid_levels, unknown_project,
    unknown_document) and message."""
    loaded = load_levels(levels)
    if isinstance(loaded, dict):
        return loaded
    level_tags, level_source = loaded
    topic, problem = _optional_text(topic, "topic")
    if problem is None and (isinstance(limit, bool) or not isinstance(limit, (int, float))
                            or not float(limit).is_integer() or not 1 <= limit <= MAXIMUM_LIMIT):
        problem = f"limit must be a whole number from 1 to {MAXIMUM_LIMIT}."
    if problem is None and not topic:
        problem = 'topic must name what to profile, for example "GRPO advantage".'
    project, project_problem = _optional_text(project, "project")
    document, document_problem = _optional_text(document, "document")
    if problem or project_problem or document_problem:
        return error("invalid_request", problem or project_problem or document_problem)
    limit = int(limit)
    now = (now or datetime.now(timezone.utc))
    now = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    cards, events = await thoughts.review()
    # The tag merges and groups of the same projection snapshot (review() read it a moment ago).
    projections = (await thoughts.ttl("projections", "/v1/projections")).get("projections") or []
    library = library_of(cards, events, projections, await thoughts.projects())

    entries, failure = find_cards(library, project, document)
    if failure:
        return _sorted(failure)
    tokens = query_tokens(topic)
    sources: dict[str, str] = {}
    for entry in entries:
        card = entry.card
        entry.question = words_of(card.question)
        notes = [_str(card.record, "note")] + [note for answer in card.answers for note in group_notes(answer).values()]
        entry.note = words_of(" ".join(notes))
        entry.names = words_of(" ".join((entry.document_name, card.pdf_stem, entry.project)))
        entry.tags = words_of(" ".join(entry.current_tags))
        entry.answer = words_of(await answer_text(thoughts, card, sources))
    entries = rank(entries, tokens, now)
    # Weak matches (one common word somewhere in a long answer) say little about the topic.
    if entries:
        best = entries[0].score
        entries = [entry for entry in entries if not entry.score < best * 0.2]
    matched = len(entries)
    entries = entries[:limit]

    # Tag -> level, by the name each tag is shown under now.
    level_of: dict[str, str] = {}
    mapping: dict[str, str] = {}
    for tag, level in level_tags:
        normalized = normalize_tag(tag)
        level_of.setdefault(fold_case(resolve(normalized, library.merges)), level)
        mapping[tag_label(normalized)] = level
    unmapped: list[str] = []
    for group in library.groups:
        # The Understanding group, and the steps of every slider: each step should have a level.
        understanding = fold_case(group.name) == "understanding"
        for tag in group.tags if understanding else group.scale:
            if fold_case(tag) not in level_of and tag_label(tag) not in unmapped:
                unmapped.append(tag_label(tag))

    by_class: dict[str, list] = {name: [] for name in CLASSES}
    questions: dict[str, list] = {name: [] for name in CLASSES}
    for entry in entries:
        card, schedule = entry.card, entry.card.schedule
        overdue = due_of(card, now).overdue_days
        reason = []
        if not card.was_answered:
            level = "unreviewed"
            reason.append("never answered")
        else:
            # Newest first: each answer at the lowest level of its mapped tags.
            rated = []
            for answer in reversed(card.answers):
                value, shown = None, []
                for tag in card.tags_of_event(answer):
                    tag_level = level_of.get(fold_case(tag), "")
                    if not tag_level or tag_level == "ignore":
                        continue
                    value = min(1.0 if value is None else value, level_value(tag_level))
                    shown.append(tag_label(tag))
                if value is not None:
                    rated.append((value, shown))
            score = 0.0
            if not rated:
                # Answered, but never with a tag that says how well: only the interval chosen tells.
                score = 0.8 if schedule.interval_days >= 21 and schedule.lapse_count == 0 else 0.5
                reason.append(f"{_plural(len(card.answers), 'answer')}, none with an understanding tag")
            else:
                # The latest answer weighs most: each older one half as much as the next.
                weight, total = 1.0, 0.0
                for value, _ in rated:
                    score += weight * value
                    total += weight
                    weight /= 2
                score /= total
                shown = ["+".join(tags) for _, tags in rated[:3]]
                reason.append(f"last answer: {shown[0]}" if len(rated) == 1 else f"last {len(shown)} answers: {', '.join(shown)}")
            if schedule.lapse_count > 0:
                score -= 0.1 * min(schedule.lapse_count, 3)
            if schedule.interval_days >= 30:
                score += 0.1
            # Long past due: it may have faded since.
            if overdue > max(7, schedule.interval_days):
                score -= 0.15
            level = "solid" if score >= 0.75 else "shaky" if score >= 0.4 else "unknown"
            if schedule.retained:
                reason.append("never asked again")
            elif schedule.interval_days > 0:
                reason.append(f"interval {days_text(schedule.interval_days)}")
            if schedule.lapse_count > 0:
                reason.append(_plural(schedule.lapse_count, "lapse"))
        if overdue > 0:
            reason.append(f"overdue {overdue} d")
        if card.retired:
            reason.append("retired")
        item = {"card_id": card.id, "question": card.question, "document": entry.document_name, "reason": "; ".join(reason)}
        if entry.project:
            item["project"] = entry.project
        by_class[level].append(item)
        questions[level].append(short_question(card.question))

    if not entries:
        summary = [f'No review cards match "{topic}": there is no record of what the user knows about it.']
    else:
        n = {name: len(by_class[name]) for name in CLASSES}
        summary = [f'"{topic}": {len(entries)} matching review card{"" if len(entries) == 1 else "s"}: {n["solid"]} solid, '
                   f'{n["shaky"]} shaky, {n["unknown"]} not understood, {n["unreviewed"]} not yet reviewed.']
        for name, label in (("solid", "Solid"), ("shaky", "Shaky"), ("unknown", "Not understood"), ("unreviewed", "Not yet reviewed")):
            shown = questions[name]
            if shown:
                summary.append(f"{label}: " + "; ".join(shown[:3]) + (f" (+{len(shown) - 3} more)" if len(shown) > 3 else ""))
    mapping_info = {"source": level_source, "levels": mapping}
    if unmapped:
        mapping_info["unmapped_understanding_tags"] = unmapped
    result = {"schema_version": 1, "accepted": True, "topic": topic, "matched": matched,
              "counts": {name: len(by_class[name]) for name in CLASSES}, "cards": by_class, "summary": "\n".join(summary),
              # No index on the server: the server's projections are read live (cached for 30 s).
              "index": {"indexed_at": None, "tag_groups_available": True, "cards": len(library.cards)},
              "mapping": mapping_info}
    if project:
        result["project"] = project
    if document:
        result["document"] = document
    return _sorted(result)
