"""Read-only projections from the saved run trace."""

from collections import Counter

from .engine import digest, grounded_messages


def inbox_records(state):
    records = []
    batch = []
    for event in state["trace"]:
        if event["type"] != "run_summary":
            batch.append(event)
            continue
        calls = [item for item in batch if item["type"] == "model_call"]
        if calls:
            records.extend(batch_records(calls[-1], batch, state, event["status"]))
        batch.clear()
    return records


def batch_records(call, events, state, status):
    raw = next((item.get("content") for item in call.get("input", {}).get("messages", [])
                if item.get("role") == "user"), None)
    if not isinstance(raw, str):
        return []
    try:
        content = call["output"]["choices"][0]["message"]["content"]
        messages = grounded_messages(raw, content)
    except (ValueError, KeyError, IndexError, TypeError):
        messages = []
    if not messages:
        return [{"source": "Unparsed upload", "date": "", "body": raw, "facts": [],
                 "classification": None, "outcome": "Extraction unavailable", "events": []}]
    sources = Counter(message.source for message in messages)
    bodies = Counter(digest(message.evidence) for message in messages)
    stored_facts = list(state["facts"].values()) + list(state["rules"].values())
    active_ids = {item.get("message_id") for item in stored_facts}
    rows = []
    for message in messages:
        message_id = digest(message.evidence)
        linked = []
        for event in events:
            if event["type"] == "model_call":
                continue
            exact = event.get("message") == message.evidence
            unique_source = bool(message.source and event.get("source") == message.source
                                 and sources[message.source] == 1)
            advisory = (event.get("status") == "advisory" and unique_source
                        and event.get("message") and event["message"] in message.evidence)
            exact_link = bodies[message_id] == 1 and (
                event.get("message_id") == message_id or (exact and event.get("source") == message.source)
            )
            if (exact_link
                    or advisory or (unique_source and event.get("status") in {"analyzed", "accepted"})):
                linked.append(event)
        outcome = "Outcome unavailable"
        if any(item["type"] == "rejection" for item in linked):
            outcome = "Rejected"
        elif any(item.get("status") == "accepted" for item in linked):
            outcome = "Accepted"
        elif any(item.get("status") == "duplicate" for item in linked):
            outcome = "Duplicate"
        elif any(item.get("status") == "advisory" for item in linked):
            outcome = "Advisory"
        elif any(item.get("status") == "ignored" for item in linked):
            outcome = "Ignored"
        elif message_id in active_ids and status == "complete":
            outcome = "In active plan"
        elif any(item.get("status") == "analyzed" for item in linked):
            outcome = "Analyzed"
        rows.append({
            "source": message.source or "Unknown sender", "date": message.date,
            "body": message.evidence, "classification": message.disposition,
            "facts": [fact.model_dump() for fact in message.facts], "outcome": outcome,
            "events": linked,
        })
    return rows
