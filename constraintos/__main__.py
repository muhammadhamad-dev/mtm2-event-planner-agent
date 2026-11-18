import argparse
import json
import sys
from pathlib import Path

from . import provider
from .engine import run
from .state import encode, totals


def print_event(event):
    print(encode(event), flush=True)


def smoke():
    schema = {
        "type": "object", "additionalProperties": False, "required": ["word"],
        "properties": {"word": {"type": "string"}},
    }
    event, content = provider.call(
        [{"role": "user", "content": "Return JSON with the word ready."}],
        schema, "smoke_test", 1, 15,
    )
    print_event(event)
    print_event(totals([event]))
    return 0 if (content and event["model"] == provider.MODEL and event["usage"]
                 and json.loads(content) == {"word": "ready"}) else 1


def main():
    parser = argparse.ArgumentParser(description="Build or update a source-aware event plan.")
    parser.add_argument("messages", type=Path, nargs="?", help="UTF-8 text file of event messages")
    parser.add_argument("--check-provider", action="store_true", help="Run a separate Groq connectivity check")
    args = parser.parse_args()
    if args.check_provider == (args.messages is not None):
        parser.error("Provide a message file or run --check-provider.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.check_provider:
            return smoke()
        raw = args.messages.read_bytes().decode("utf-8")
        status, _ = run(raw, output=print_event)
        return 0 if status in {"complete", "duplicate"} else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ConstraintOS: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
