import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile


def encode(value):
    def decimal(number):
        if isinstance(number, Decimal):
            return format(number, "f")
        raise TypeError(type(number).__name__)

    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=decimal)


def atomic_write(path, content):
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                            dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def transaction(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "state.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another ConstraintOS run is already updating this state.") from None
        yield


def load(root):
    path = root / "state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "facts": {}, "rules": {}, "messages": [], "runs": [], "plan": None,
        "proposals": [], "rejected": [], "trace": [],
    }


def totals(trace):
    calls = [event for event in trace if event["type"] == "model_call"]
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    reported = [event["usage"] for event in calls if event["usage"] and all(
        type(event["usage"].get(field)) is int for field in fields
    )]
    return {
        "type": "token_totals", "model_calls": len(calls),
        "calls_without_reported_usage": len(calls) - len(reported),
        **{field: sum(item[field] for item in reported) for field in fields},
    }


def plan_document(state):
    return {
        "plan": state["plan"], "facts": state["facts"], "rules": list(state["rules"].values()),
        "proposals": state["proposals"], "rejected": state["rejected"],
    }


def save(root, state):
    atomic_write(root / "state.json", encode(state) + "\n")
    atomic_write(root / "plan.json", encode(plan_document(state)) + "\n")
    atomic_write(root / "trace.json", encode(state["trace"] + [totals(state["trace"])]) + "\n")
