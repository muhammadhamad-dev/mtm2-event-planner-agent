import json
import unittest

from constraintos.presentation import inbox_records


class InboxTests(unittest.TestCase):
    def test_ambiguous_sender_keeps_exact_rejection_without_guessing_dependencies(self):
        decision = "Cancel the reserved room."
        other = "The replacement room is available."
        call = {
            "type": "model_call",
            "input": {"messages": [{"role": "user", "content": f"5 Oct\n{decision}\n\n6 Oct\n{other}"}]},
            "output": {"choices": [{"message": {"content": json.dumps({"messages": [
                {"source": "Facilities", "date": day, "evidence": body,
                 "disposition": "relevant", "reason": "", "facts": []}
                for day, body in (("--10-05", decision), ("--10-06", other))
            ]})}}]},
        }
        analyzed = {"type": "validation", "source": "Facilities", "status": "analyzed",
                    "affected_areas": ["venue"]}
        rejected = {"type": "rejection", "source": "Facilities", "message": decision,
                    "reason": "The reservation is fixed."}
        state = {"facts": {}, "rules": {},
                 "trace": [call, analyzed, rejected, {"type": "run_summary", "status": "complete"}]}
        first, second = inbox_records(state)
        self.assertEqual((first["body"], first["date"], first["outcome"]), (decision, "--10-05", "Rejected"))
        self.assertEqual(first["events"], [rejected])
        self.assertEqual(second["outcome"], "Outcome unavailable")
        self.assertFalse(second["events"])

    def test_failure_preserves_upload_and_skipped_file_does_not_create_new_record(self):
        source = "A real uploaded message."
        state = {"facts": {}, "rules": {}, "trace": [
            {"type": "model_call", "input": {"messages": [{"role": "user", "content": source}]}, "output": None},
            {"type": "run_summary", "status": "provider_error"},
            {"type": "run_summary", "status": "duplicate"},
        ]}
        record, = inbox_records(state)
        self.assertEqual(record["body"], source)
        self.assertEqual(record["outcome"], "Extraction unavailable")
