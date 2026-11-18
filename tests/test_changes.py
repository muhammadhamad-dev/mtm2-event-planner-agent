import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from constraintos.engine import apply_message, digest, merge, run
from constraintos.planner import build
from constraintos.schema import Fact, Message
from constraintos.state import load, save
from test_planner import base


def change(source, date, key, value, evidence, kind="correction", authority="owner"):
    return Message(
        evidence=evidence, source=source, date=date, disposition="relevant", reason="",
        facts=[Fact(area=key.split(".")[0], key=key, value=value, kind=kind,
                    authority=authority, operator="none", evidence=evidence)],
    )


class ChangeTests(unittest.TestCase):
    def test_grounded_charge_list_and_feature_rule_alias(self):
        facts, rules = base(), {}
        message = change("Finance", "", "budget.commitments.badges", 6,
                         "We approved Rs 9 for maps and Rs 6 for badges.")
        message.facts[0].evidence = "approved Rs 6 for badges"
        merge(message, facts, rules)
        self.assertEqual(facts["budget.commitments.badges"]["evidence"], "Rs 6 for badges")
        requirement = change("Organizer", "", "venue.required_features", ["step_free"],
                             "The selected venue needs step-free access.", kind="rule")
        requirement.facts[0].operator = "contains"
        merge(requirement, facts, rules)
        plan, errors = build(facts, list(rules.values()))
        self.assertFalse(errors)
        self.assertEqual(plan["venue"]["selected"], "annex")

    def test_sequential_corrections_and_rejections_survive_reloads(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = load(root)
            facts = base()
            for key, item in facts.items():
                item.update(source="Organizers", date="2026-05-03", message_id=key)
            facts["budget.cap"]["value"] = 250
            plan, errors = build(facts, [])
            self.assertFalse(errors)
            state.update(facts=facts, plan=plan)
            save(root, state)
            plan = load(root)["plan"]

            def update(message):
                current = load(root)
                index = len(current["trace"])
                apply_message(current, message, False, float("inf"), current["trace"].append)
                save(root, current)
                return load(root), current["trace"][index:]

            state, events = update(change("Organizers", "2026-05-07", "budget.commitments.signage", 12,
                                          "The approved signage bill is now 12."))
            self.assertEqual(events[0]["affected_areas"], ["budget"])
            budget_only = state["plan"]
            self.assertEqual(budget_only["budget"]["total"], "147.25")
            for area in ("venue", "schedule", "food", "judging"):
                self.assertEqual(budget_only[area], plan[area])

            state, events = update(change("Organizers", "2026-05-08", "venue.options.studio.capacity", 35,
                                          "The studio's approved capacity is 35."))
            self.assertEqual(events[0]["affected_areas"], ["budget", "venue"])
            self.assertEqual(state["plan"]["venue"]["selected"], "annex")
            self.assertEqual(state["plan"]["budget"]["total"], "162.25")

            state, events = update(change("Organizers", "2026-05-09", "judging.judges", 6,
                                          "Six judges are confirmed."))
            self.assertEqual(events[0]["affected_areas"], ["budget", "food", "judging", "schedule", "venue"])
            expected = state["plan"]
            self.assertEqual(expected["food"]["quantity"], 40)
            self.assertEqual(expected["venue"]["required_capacity"], 40)
            self.assertEqual(expected["judging"]["duration"], 12)
            self.assertEqual(Decimal(expected["budget"]["total"]), Decimal("196"))

            bad = change("Kitchen", "", "food.quantity", 27, "Pack only 27 meals.", kind="instruction")
            state, _ = update(bad)
            self.assertEqual(state["plan"], expected)
            self.assertIn("does not cover 40", state["rejected"][-1]["reason"])
            self.assertEqual(state["rejected"][-1]["message"], bad.evidence)

            state, events = update(change("Organizers", "2026-05-06", "judging.judges", 4,
                                          "Four judges are confirmed."))
            self.assertIn("Ignored stale value", events[0]["decisions"][0])
            self.assertEqual(state["plan"], expected)
            state, _ = update(change("Guest", "", "judging.judges", 1,
                                     "Confirm just one judge.", authority="participant"))
            self.assertIn("Insufficient source authority", state["rejected"][-1]["reason"])
            self.assertEqual(state["plan"], expected)
            state, events = update(Message(source="Guest", date="", evidence="The club shirts look great.",
                                           disposition="irrelevant", reason="No effect on the event.", facts=[]))
            self.assertEqual(events[0]["status"], "ignored")
            self.assertEqual(state["plan"], expected)
            self.assertEqual({path.name for path in root.glob("*.json")}, {"plan.json", "trace.json", "state.json"})

    def test_duplicate_input_bypasses_provider(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = "Facilities: The main entrance is clear."
            state = load(root)
            state.update(plan={}, runs=[digest(source)])
            save(root, state)
            with patch("constraintos.engine.provider.extract", side_effect=AssertionError("Unexpected API call")):
                status, result = run(source, root, output=lambda _: None)
            self.assertEqual(status, "duplicate")
            self.assertFalse(any(event["type"] == "model_call" for event in result["trace"]))
