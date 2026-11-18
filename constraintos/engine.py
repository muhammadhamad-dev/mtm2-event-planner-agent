import hashlib
import math
import re
import time
from calendar import month_name
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from . import provider
from .planner import build
from .schema import Extraction
from .state import load, save, totals, transaction

AREAS = ("venue", "schedule", "food", "judging", "budget")
DEPENDENCIES = {
    "venue": {"budget"}, "schedule": set(), "food": {"budget"},
    "judging": {"schedule", "venue", "food", "budget"}, "budget": set(),
}
KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_][a-z0-9_-]*)+\Z")
MONTHS = {name.lower(): number for number, name in enumerate(month_name) if name}
MONTHS.update({name[:3]: number for name, number in tuple(MONTHS.items())})
MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
SOURCE_DATES = (
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[\s-]+(?P<month>{MONTH})\b\.?(?:[\s,-]+(?P<year>\d{{4}})\b)?", re.I),
    re.compile(rf"\b(?P<month>{MONTH})\.?[\s-]+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:[\s,-]+(?P<year>\d{{4}})\b)?", re.I),
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b"),
    re.compile(r"(?<![\d/-])(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{4}))?(?![\d/-])"),
)
AVAILABILITY = re.compile(
    r"\b(?:late|later|delay(?:ed)?|early|arriv\w*|availability|make it|attend\w*|der se|pohnch\w*|pahunch\w*)\b", re.I
)
PERSONAL = re.compile(r"\b(?:i|my|main|mein|meri|mera)\b", re.I)
TENTATIVE = re.compile(r"\b(?:may|might|could|possibly|probably|tentative|shayad|mumkin)\b", re.I)
ARRIVAL_TIMING = re.compile(r"\b(?:late|later|delay(?:ed)?|early|arriv\w*|der se|pohnch\w*|pahunch\w*)\b", re.I)


def digest(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def quote_span(text, quote, start=0):
    exact = text.find(quote, start)
    if exact >= 0:
        return exact, exact + len(quote)
    # Match PDF wraps and newline styles while saving original source characters.
    pieces = re.split(r"(\s+)", quote)
    pattern = "".join(r"\s+" if part.isspace() else re.escape(part) for part in pieces)
    match = re.compile(pattern).search(text, start)
    if match:
        return match.span()
    raise ValueError("Quote does not occur in its source text.")


def fact_span(text, quote, key):
    try:
        return quote_span(text, quote)
    except ValueError:
        fragment = quote.rstrip(".,;!?")
        if key.startswith("budget.commitments."):
            verb, _, charge = fragment.partition(" ")
            if (verb.lower() in {"committed", "paid", "ordered", "approved"}
                    and re.search(rf"\b{verb}\b", text, re.I)
                    and re.match(r"(?:rs\.?|pkr|usd|[$£€])\s*\d", charge, re.I)):
                fragment = charge
        if not fragment.strip() or fragment == quote:
            raise
        return quote_span(text, fragment)


def timestamp(value):
    if not value:
        return None
    return date.fromisoformat("2000-" + value[2:] if value.startswith("--") else value)


def grounded_date(proposed, metadata):
    dates = set()
    for pattern in SOURCE_DATES:
        for match in pattern.finditer(metadata):
            month = match["month"].lower()
            number = int(month) if month.isdigit() else MONTHS[month]
            value = f"{match['year'] or '-'}-{number:02}-{int(match['day']):02}"
            try:
                timestamp(value)
            except ValueError:
                continue
            dates.add(value)
    if len(dates) == 1:
        return dates.pop()
    if re.fullmatch(r"(?:\d{4}|-)-\d{2}-\d{2}", proposed):
        matches = {value for value in dates if value[-5:] == proposed[-5:]}
        if len(matches) == 1:
            return matches.pop()
    return ""


def older(incoming, previous):
    first, second = timestamp(incoming), timestamp(previous)
    if first is None or second is None:
        return False
    if incoming.startswith("--") != previous.startswith("--"):
        return False
    if incoming.startswith("--"):
        return (first.month, first.day) < (second.month, second.day)
    return first < second


def availability_notices(text):
    return [
        part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if AVAILABILITY.search(part)
        and (TENTATIVE.search(part) or (PERSONAL.search(part) and ARRIVAL_TIMING.search(part)))
    ]


def grounded_messages(raw, content):
    messages = Extraction.model_validate_json(content).messages
    if not messages:
        raise ValueError("No source messages were extracted.")
    cursor = 0
    for message in messages:
        if not message.evidence.strip():
            raise ValueError("Extracted message has no evidence.")
        start, end = quote_span(raw, message.evidence, cursor)
        metadata = raw[cursor:start]
        first_line, newline, _ = raw[start:end].partition("\n")
        if re.match(r"\s*(?:date|sent|posted)\s*:", first_line, re.I) or (
            newline and message.source and message.source.casefold() in first_line.casefold()
        ):
            metadata += "\n" + first_line
        message.date = grounded_date(message.date, metadata)
        message.evidence = raw[start:end]
        cursor = end
    return messages


def cites_replaced_amount(evidence, old_value):
    if type(old_value) not in (int, float):
        return False
    marker = re.search(r"\b(?:not|instead of|rather than|nahi|nahin|ki jagah)\b", evidence, re.I)
    if marker is None:
        return False
    amounts = re.findall(r"\d[\d,]*(?:\.\d+)?", evidence[marker.end():])
    return any(float(amount.replace(",", "")) == old_value for amount in amounts)


def dependencies(changed, rules):
    edges = {area: downstream.copy() for area, downstream in DEPENDENCIES.items()}
    for rule in rules:
        operand = rule["value"]
        if isinstance(operand, str) and operand.startswith("$"):
            source = operand[1:].split(".", 1)[0]
            if source in edges:
                edges[source].add(rule["area"])
    affected = set(changed)
    pending = list(changed)
    while pending:
        new = edges[pending.pop()] - affected
        affected.update(new)
        pending.extend(new)
    return sorted(affected)


def reject(state, emit, message, source, reason):
    entry = {"source": source, "message": message, "reason": reason}
    state["rejected"].append(entry)
    emit({"type": "rejection", **entry})


def accepted(plan, source=None):
    return {
        "type": "validation", "status": "accepted", "source": source,
        "validated_areas": list(AREAS),
        "checks": {
            "venue": {"seats": plan["venue"]["capacity"], "required": plan["venue"]["required_capacity"]},
            "schedule": {"event_minutes": plan["schedule"]["event_minutes"]},
            "food": {"quantity": plan["food"]["quantity"], "required": plan["food"]["headcount"]},
            "judging": {"team_capacity": plan["judging"]["team_capacity"], "teams": plan["judging"]["teams"]},
            "budget": {"total": plan["budget"]["total"], "cap": plan["budget"]["cap"]},
        },
    }


def merge(message, facts, rules):
    notes = []
    proposals = []
    advisories = []
    changed = set()
    timestamp(message.date)
    for fact in message.facts:
        key, value = fact.key, fact.value
        declared_area = fact.area.split(".", 1)[0]
        if key.split(".", 1)[0] not in AREAS:
            key = f"{fact.area}.{key}"
        original_key = key
        if fact.kind == "rule" and key in {"venue.required_features", "food.required_features"}:
            key = key.replace(".required_features", ".features")
        if key in {"schedule.staff", "budget.commitments.staff"}:
            if re.search(r"\bstaff\b", fact.evidence, re.I) and not re.search(r"\b(?:rs|pkr|usd)\b|[$£€]", fact.evidence, re.I):
                key = "judging.staff"
        if key == "budget.commitments.honorarium" and re.search(r"\b(?:each|per judge)\b", fact.evidence, re.I):
            key = "judging.honorarium"
        area = key.split(".", 1)[0]
        if area == "judging" and (
            key == "judging.judges" or re.search(r"lateness|availability|arrival", key)
        ):
            notice = next((part for part in availability_notices(message.evidence)
                           if len(message.facts) == 1 or fact.evidence.rstrip(".,;!?") in part), None)
            if notice:
                advisories.append(notice)
                continue
        try:
            if not fact.evidence.strip():
                raise ValueError("Empty quote.")
            start, end = fact_span(message.evidence, fact.evidence, key)
        except ValueError:
            raise ValueError(f"Evidence for {key} does not occur in the source message.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Invalid numeric value for {key}.")
        row = {**fact.model_dump(), "key": key, "area": area, "evidence": message.evidence[start:end],
               "source": message.source, "date": message.date,
               "message_id": digest(message.evidence)}
        if fact.kind == "proposal":
            proposals.append({**row, "area": "proposal", "key": "proposal"})
            notes.append("Recorded unapproved proposal.")
            continue
        if fact.authority != "owner" or not message.source.strip():
            notes.append(f"Insufficient source authority for {key}.")
            continue
        if (not KEY.fullmatch(key) or area not in AREAS
                or (area != declared_area and key == original_key)):
            raise ValueError(f"Invalid or mismatched planning key {key!r}.")
        if fact.kind == "rule":
            # Keep independent limits and feature requirements from different sources.
            # A dated revision by the same owner can replace its previous limit.
            identity = f"{message.source.casefold()}:{key}:{fact.operator}"
            if fact.operator in {"contains", "excludes"}:
                identity += ":" + repr(value)
            target = rules
        else:
            identity, target = key, facts
        prior = target.get(identity)
        if prior:
            if older(message.date, prior["date"]):
                notes.append(f"Ignored stale value for {key} from {message.source}.")
                continue
            if type(prior["value"]) is type(value) and prior["value"] == value:
                if message.date and (not prior["date"] or older(prior["date"], message.date)):
                    target[identity] = {**prior, **row}
                notes.append(f"{key} already has this value.")
                continue
            if prior["source"].casefold() != message.source.casefold() and fact.kind != "correction":
                raise ValueError(f"Conflicting claims for {key} from {prior['source']} and {message.source}.")
            if message.date and prior["date"] and not older(prior["date"], message.date) and fact.kind != "correction":
                raise ValueError(f"Dates do not establish a newer claim for {key}.")
            explicit_replacement = (
                prior["source"].casefold() == message.source.casefold()
                and cites_replaced_amount(message.evidence, prior["value"])
            )
            if (not message.date and prior["date"] and fact.kind not in {"correction", "instruction"}
                    and not explicit_replacement):
                raise ValueError(f"Undated claim cannot supersede the dated value for {key}.")
            row["supersedes"] = prior["message_id"]
            notes.append(f"Superseded {key} from {prior['source']} ({prior['date'] or 'undated'}).")
        target[identity] = row
        changed.add(area)
    return changed, proposals, notes, list(dict.fromkeys(advisories))


def apply_message(state, message, initializing, deadline, emit):
    message_id = digest(message.evidence)
    if message_id in state["messages"]:
        emit({"type": "validation", "status": "duplicate", "message_id": message_id})
        return
    state["messages"].append(message_id)
    if message.disposition == "invalid":
        reject(state, emit, message.evidence, message.source, message.reason or "Invalid instructions.")
        return
    if message.disposition == "irrelevant":
        notice = availability_notices(message.evidence)
        emit({"type": "validation", "status": "advisory" if notice else "ignored", "source": message.source,
              "message": message.evidence, "reason": message.reason})
        return

    facts, rules = state["facts"].copy(), state["rules"].copy()
    try:
        changed, proposals, notes, advisories = merge(message, facts, rules)
        for notice in advisories:
            emit({"type": "validation", "status": "advisory", "source": message.source,
                  "message": notice, "date": message.date,
                  "reason": "Availability notice leaves confirmed attendance unchanged."})
        emit({"type": "validation", "status": "analyzed", "source": message.source,
              "changed_areas": sorted(changed), "affected_areas": dependencies(changed, rules.values()),
              "decisions": notes})
        if not changed and not proposals and not advisories and any(note.startswith("Insufficient") for note in notes):
            raise ValueError(" ".join(notes))
        if not initializing and changed:
            candidate, errors = build(facts, list(rules.values()), deadline)
            if errors:
                raise ValueError(" ".join(errors))
            state["plan"] = candidate
            emit(accepted(candidate, message.source))
    except ValueError as exc:
        reject(state, emit, message.evidence, message.source, str(exc))
        return
    state["facts"], state["rules"] = facts, rules
    state["proposals"].extend(proposals)


def run(raw, root=Path(".constraintos"), output=print):
    started = time.monotonic()
    if not raw.strip():
        raise ValueError("Message input is empty.")
    key = provider.load_key()
    if key and key in raw:
        raise ValueError("Message file contains the configured API credential.")
    with transaction(root):
        state = load(root)
        current = []

        def emit(entry):
            state["trace"].append(entry)
            current.append(entry)
            output(entry)

        raw_id = digest(raw)
        if raw_id in state["runs"]:
            emit({"type": "validation", "status": "duplicate", "reason": "This exact input was already processed."})
            status = "duplicate" if state["plan"] is not None else "incomplete"
            return finish(root, state, current, output, started, status)

        initializing = state["plan"] is None
        deadline = started + (114 if initializing else 55)
        context = {}
        for item in (*state["facts"].values(), *state["rules"].values()):
            context.setdefault(item["source"], []).append(item["key"])
        number = 1 + sum(item["type"] == "model_call" for item in state["trace"])
        schema_retry = rate_retry = False
        for attempt in range(3):
            timeout = deadline - time.monotonic() - 4
            event, content = provider.extract(raw, context, number + attempt, timeout, initializing, schema_retry)
            emit(event)
            remaining = deadline - time.monotonic() - 4
            try:
                delay = float(event["rate_limits"].get("retry-after", "0"))
            except ValueError:
                delay = 0
            if event.get("status") == 429 and not rate_retry and attempt < 2 and 0 < delay < remaining - 3:
                rate_retry = True
                emit({"type": "validation", "status": "rate_limit_retry", "wait_seconds": delay})
                time.sleep(delay)
                continue
            body = event["output"]
            if (event.get("status") == 400 and not schema_retry and attempt < 2 and remaining > 8
                    and isinstance(body, dict) and isinstance(body.get("error"), dict)
                    and body["error"].get("code") == "json_validate_failed"):
                schema_retry = True
                emit({"type": "validation", "status": "structured_output_retry"})
                continue
            break
        counts = ("prompt_tokens", "completion_tokens", "total_tokens")
        usage = event["usage"] or {}
        if (not isinstance(content, str) or not content or event.get("finish_reason") != "stop"
                or event["model"] != provider.MODEL
                or any(type(usage.get(field)) is not int for field in counts)):
            emit({"type": "validation", "status": "provider_error",
                  "reason": "No complete structured response with reported token usage."})
            return finish(root, state, current, output, started, "provider_error")

        try:
            messages = grounded_messages(raw, content)
        except (ValidationError, ValueError):
            emit({"type": "validation", "status": "extraction_error",
                  "reason": "The response did not provide grounded messages in source order."})
            return finish(root, state, current, output, started, "extraction_error")

        for message in messages:
            apply_message(state, message, initializing, deadline, emit)

        if initializing:
            try:
                candidate, errors = build(state["facts"], list(state["rules"].values()), deadline)
            except ValueError as exc:
                candidate, errors = None, [str(exc)]
            if errors:
                emit({"type": "validation", "status": "incomplete", "reasons": errors})
            else:
                state["plan"] = candidate
                emit(accepted(candidate))
        state["runs"].append(raw_id)
        status = "complete" if state["plan"] is not None else "incomplete"
        return finish(root, state, current, output, started, status)


def finish(root, state, current, output, started, status):
    elapsed = round(time.monotonic() - started, 3)
    summary = {"type": "run_summary", "status": status, "wall_seconds": elapsed}
    state["trace"].append(summary)
    save(root, state)
    output(summary)
    output(totals(current))
    return status, state
