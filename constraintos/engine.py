import hashlib
import math
import re
import time
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
    "judging": {"schedule", "venue", "food", "budget"}, "budget": set(AREAS),
}
KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_][a-z0-9_-]*)+\Z")


def digest(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def timestamp(value):
    if not value:
        return None
    return date.fromisoformat("2000-" + value[2:] if value.startswith("--") else value)


def older(incoming, previous):
    first, second = timestamp(incoming), timestamp(previous)
    if first is None or second is None:
        return False
    if incoming.startswith("--") or previous.startswith("--"):
        return (first.month, first.day) < (second.month, second.day)
    return first < second


def reject(state, emit, message, source, reason):
    entry = {"source": source, "message": message, "reason": reason}
    state["rejected"].append(entry)
    emit({"type": "rejection", **entry})


def merge(message, facts, rules):
    notes = []
    proposals = []
    changed = set()
    timestamp(message.date)
    for fact in message.facts:
        key, value = fact.key, fact.value
        if not fact.evidence.strip() or fact.evidence not in message.evidence:
            raise ValueError(f"Evidence for {key} does not occur in the source message.")
        if not KEY.fullmatch(key) or not key.startswith(fact.area + "."):
            raise ValueError(f"Invalid or mismatched planning key {key!r}.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Invalid numeric value for {key}.")
        row = {**fact.model_dump(), "source": message.source, "date": message.date,
               "message_id": digest(message.evidence)}
        if fact.kind == "proposal":
            proposals.append(row)
            notes.append(f"Recorded unapproved proposal for {key}.")
            continue
        if fact.authority != "owner" or not message.source.strip():
            notes.append(f"Insufficient source authority for {key}.")
            continue
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
                raise ValueError(f"Conflicting claims for {key} on the same date.")
            if not message.date and prior["date"] and fact.kind not in {"correction", "instruction"}:
                raise ValueError(f"Undated claim cannot supersede the dated value for {key}.")
            row["supersedes"] = prior["message_id"]
            notes.append(f"Superseded {key} from {prior['source']} ({prior['date'] or 'undated'}).")
        target[identity] = row
        changed.add(fact.area)
    return changed, proposals, notes


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
            return finish(root, state, current, output, started, "duplicate")

        initializing = state["plan"] is None
        deadline = started + (114 if initializing else 55)
        timeout = deadline - time.monotonic() - 4
        context = [
            {field: item[field] for field in ("key", "value", "source", "date")}
            for item in (*state["facts"].values(), *state["rules"].values())
        ]
        number = 1 + sum(item["type"] == "model_call" for item in state["trace"])
        event, content = provider.extract(raw, context, number, timeout)
        emit(event)
        if not content or not event["usage"] or event["output"]["choices"][0]["finish_reason"] != "stop":
            emit({"type": "validation", "status": "provider_error",
                  "reason": "No complete structured response with reported token usage."})
            return finish(root, state, current, output, started, "provider_error")

        try:
            messages = Extraction.model_validate_json(content).messages
            cursor = 0
            for message in messages:
                if not message.evidence.strip():
                    raise ValueError("Extracted message has no evidence.")
                cursor = raw.index(message.evidence, cursor) + len(message.evidence)
            if not messages:
                raise ValueError("No source messages were extracted.")
        except (ValidationError, ValueError):
            emit({"type": "validation", "status": "extraction_error",
                  "reason": "The response did not provide grounded messages in source order."})
            return finish(root, state, current, output, started, "extraction_error")

        for message in messages:
            message_id = digest(message.evidence)
            if message_id in state["messages"]:
                emit({"type": "validation", "status": "duplicate", "message_id": message_id})
                continue
            state["messages"].append(message_id)
            if message.disposition == "invalid":
                reject(state, emit, message.evidence, message.source, message.reason or "Invalid instructions.")
                continue
            if message.disposition == "irrelevant":
                emit({"type": "validation", "status": "ignored", "source": message.source,
                      "message": message.evidence, "reason": message.reason})
                continue

            facts, rules = state["facts"].copy(), state["rules"].copy()
            try:
                changed, proposals, notes = merge(message, facts, rules)
                impacted = set().union(changed, *(DEPENDENCIES[area] for area in changed))
                emit({"type": "validation", "status": "analyzed", "source": message.source,
                      "changed_areas": sorted(changed), "affected_areas": sorted(impacted), "decisions": notes})
                if not changed and not proposals and any(note.startswith("Insufficient") for note in notes):
                    raise ValueError(" ".join(notes))
                if not initializing and changed:
                    candidate, errors = build(facts, list(rules.values()), deadline)
                    if errors:
                        raise ValueError(" ".join(errors))
                    state["plan"] = candidate
                    emit({"type": "validation", "status": "accepted", "source": message.source,
                          "validated_areas": list(AREAS)})
            except ValueError as exc:
                reject(state, emit, message.evidence, message.source, str(exc))
                continue
            state["facts"], state["rules"] = facts, rules
            state["proposals"].extend(proposals)

        if initializing:
            try:
                candidate, errors = build(state["facts"], list(state["rules"].values()), deadline)
            except ValueError as exc:
                candidate, errors = None, [str(exc)]
            if errors:
                emit({"type": "validation", "status": "incomplete", "reasons": errors})
            else:
                state["plan"] = candidate
                emit({"type": "validation", "status": "accepted", "validated_areas": list(AREAS)})
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
