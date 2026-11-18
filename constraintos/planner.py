from decimal import Decimal
from itertools import product
from operator import eq, ge, le
from time import monotonic


def put(root, key, value):
    *parents, leaf = key.split(".")
    for part in parents:
        root = root.setdefault(part, {})
        if not isinstance(root, dict):
            raise ValueError(f"Conflicting paths at {key}")
    if isinstance(root.get(leaf), dict):
        raise ValueError(f"Conflicting paths at {key}")
    root[leaf] = value


def get(root, key):
    for part in key.split("."):
        if not isinstance(root, dict):
            return None
        root = root.get(part)
    return root


def integer(value):
    return type(value) is int and value >= 0


def money(value):
    return type(value) in (int, float, Decimal) and Decimal(str(value)).is_finite() and value >= 0


def requirements(plan, rules):
    errors = []
    for rule in rules:
        key, operator, expected = (rule[k] for k in ("key", "operator", "value"))
        actual = get(plan, key)
        try:
            if operator in ("eq", "ge", "le"):
                passed = actual is not None and {"eq": eq, "ge": ge, "le": le}[operator](actual, expected)
            elif operator in ("contains", "excludes"):
                passed = isinstance(actual, (list, str)) and (
                    (expected in actual) == (operator == "contains")
                )
            else:
                errors.append(f"Cannot evaluate rule {key}: {expected}")
                continue
        except TypeError:
            passed = False
        if not passed:
            errors.append(f"{key} requires {operator} {expected!r}; candidate has {actual!r}")
    return errors


def schedule_plan(schedule, rules, judging_minutes, deadline):
    activities = {name: dict(activity) for name, activity in schedule.get("activities", {}).items()}
    total = schedule.get("event_minutes")
    start = schedule.get("start", 0)
    end = schedule.get("end")
    if total is None and integer(end) and integer(start) and end > start:
        total = end - start
    if not integer(start) or not integer(total) or total == 0:
        return schedule, ["A positive event duration and valid start are required."]
    if end is not None and end != start + total:
        return schedule, ["Event start, end and duration disagree."]
    end = start + total
    for rule in rules:
        pieces = rule["key"].split(".")
        if len(pieces) == 4 and pieces[:2] == ["schedule", "activities"]:
            _, _, name, field = pieces
            activity = activities.setdefault(name, {})
            if field in ("minutes", "start", "end") and rule["operator"] == "eq":
                activity.setdefault(field, rule["value"])
            if field == "minutes" and rule["operator"] == "ge":
                if field not in activity:
                    activity[field] = rule["value"]
                elif rule["operator"] == "ge" and integer(activity[field]) and integer(rule["value"]):
                    activity[field] = max(activity[field], rule["value"])
    judging = activities.setdefault("judging", {})
    judging.setdefault("minutes", judging_minutes)
    if not integer(judging["minutes"]) or judging["minutes"] < judging_minutes:
        return schedule, ["The judging block is too short for the teams and judge count."]
    scheduled = {}
    occupied = []

    def reserve(name, activity, begin, finish):
        if begin < start or finish > end or finish <= begin:
            return False
        if any(begin < b and a < finish for a, b in occupied):
            return False
        occupied.append((begin, finish))
        occupied.sort()
        scheduled[name] = {**activity, "start": begin, "end": finish, "minutes": finish - begin}
        return True

    floating = []
    for name, activity in activities.items():
        begin, finish, duration = (activity.get(key) for key in ("start", "end", "minutes"))
        if any(value is not None and not integer(value) for value in (begin, finish, duration)):
            return schedule, [f"Invalid time or duration for {name}."]
        if begin is None and finish is None:
            if not duration:
                return schedule, [f"Missing duration for {name}."]
            floating.append((name, activity))
            continue
        if duration is not None:
            if begin is None:
                begin = finish - duration
            if finish is None:
                finish = begin + duration
        if begin is None or finish is None or (duration is not None and finish - begin != duration):
            return schedule, [f"Inconsistent time bounds for {name}."]
        if not reserve(name, activity, begin, finish):
            return schedule, [f"{name} overlaps another activity or exceeds the event window."]

    def valid_order():
        ordered = sorted((block["order"], block["start"]) for block in scheduled.values()
                         if integer(block.get("order")))
        return all(left[0] == right[0] or left[1] <= right[1] for left, right in zip(ordered, ordered[1:]))

    def place(index):
        if monotonic() >= deadline:
            raise ValueError("Schedule search reached its time limit.")
        if index == len(floating):
            fields = {"schedule": {**schedule, "start": start, "end": end,
                                   "event_minutes": total, "activities": scheduled}}
            return valid_order() and not requirements(fields, schedule_rules)
        name, activity = floating[index]
        duration = activity["minutes"]
        edges = {start, end - duration, *(b for _, b in occupied),
                 *(a - duration for a, _ in occupied)}
        for rule in schedule_rules:
            if rule["key"] == f"schedule.activities.{name}.start" and integer(rule["value"]):
                edges.add(rule["value"])
            if rule["key"] == f"schedule.activities.{name}.end" and integer(rule["value"]):
                edges.add(rule["value"] - duration)
        for begin in sorted(edges, reverse=name == "judging"):
            finish = begin + duration
            if reserve(name, activity, begin, finish):
                if place(index + 1):
                    return True
                occupied.remove((begin, finish))
                del scheduled[name]
        return False

    schedule_rules = [rule for rule in rules if rule["key"].startswith("schedule.")]
    if not place(0):
        details = "; ".join(f'{rule["key"]} {rule["operator"]} {rule["value"]}'
                            for rule in schedule_rules)
        return schedule, [f"Required activities cannot fit inside the event window. Rules: {details}"]
    timeline = []
    cursor = start
    for name, block in sorted(scheduled.items(), key=lambda pair: pair[1]["start"]):
        if cursor < block["start"]:
            timeline.append({"activity": "Team work / flexible event time", "start": cursor, "end": block["start"]})
        timeline.append({"activity": block.get("name", name.replace("_", " ").title()), **block})
        cursor = block["end"]
    if cursor < end:
        timeline.append({"activity": "Team work / flexible event time", "start": cursor, "end": end})
    return {**schedule, "start": start, "end": end, "event_minutes": total,
            "activities": scheduled, "timeline": timeline, "relative_times": "start" not in schedule}, []


def build(facts, rules, deadline=float("inf")):
    data = {area: {} for area in ("venue", "schedule", "food", "judging", "budget")}
    for fact in sorted(facts.values(), key=lambda item: item["key"]):
        put(data, fact["key"], fact["value"])
    venue, schedule, food, judging, budget = data.values()
    errors = []
    students, judges, team_size, teams, staff, per_team, limit = (
        judging.get(key) for key in (
            "students", "judges", "team_size", "teams", "staff",
            "minutes_per_team", "max_teams_per_judge",
        )
    )
    if teams is None and integer(students) and integer(team_size) and team_size:
        teams = (students + team_size - 1) // team_size
    required = (students, judges, teams, per_team, limit)
    if any(not integer(value) or value == 0 for value in required):
        errors.append("Need positive student, team and judge counts, judging minutes and teams-per-judge limit.")
    elif teams > judges * limit:
        errors.append(f"{judges} judges can cover only {judges * limit} of {teams} teams.")
    else:
        waves = (teams + judges - 1) // judges
        judging.update(teams=teams, team_capacity=judges * limit, waves=waves, duration=waves * per_team)
        if teams <= 10000 and judges <= 10000:
            judging["assignments"] = [
                {"judge": judge + 1, "teams": list(range(judge + 1, teams + 1, judges))}
                for judge in range(min(judges, teams))
            ]
        schedule, failures = schedule_plan(schedule, rules, judging["duration"], deadline)
        errors.extend(failures)

    total_people = students + judges + (staff or 0) if (
        integer(students) and integer(judges) and (staff is None or integer(staff))
    ) else None
    headcount = food.get("headcount", total_people)
    quantity = food.get("quantity", headcount)
    if "quantity" not in food and integer(quantity):
        lower_bounds = [rule["value"] for rule in rules if rule["key"] == "food.quantity"
                        and rule["operator"] in {"ge", "eq"} and integer(rule["value"])]
        quantity = max(quantity, *lower_bounds) if lower_bounds else quantity
    if not integer(headcount) or not integer(quantity) or quantity < headcount or headcount == 0:
        errors.append(f"Food order of {quantity} does not cover {headcount} required meals.")
    required_capacity = venue.get("required_capacity", total_people)
    if not integer(required_capacity) or required_capacity <= 0:
        errors.append("Need a positive venue headcount.")
    cap, honorarium = budget.get("cap"), judging.get("honorarium")
    commitments = budget.get("commitments", {})
    if not money(cap) or not money(honorarium) or not all(map(money, commitments.values())):
        errors.append("Need a valid budget cap, judge honorarium and committed expenses.")
    if errors:
        return data, errors

    def amount(value):
        return Decimal(str(value))

    committed = sum(map(amount, commitments.values()), Decimal(0))
    judge_cost = judges * amount(honorarium)
    venues = [(key, item) for key, item in venue.get("options", {}).items()
              if integer(item.get("capacity")) and item["capacity"] >= required_capacity
              and money(item.get("cost")) and item.get("available", True) is not False
              and venue.get("selected", key) == key]
    meals = [(key, item) for key, item in food.get("options", {}).items()
             if money(item.get("unit_cost")) and money(item.get("fixed_cost", 0))
             and item.get("available", True) is not False and food.get("selected", key) == key]
    if not venues or not meals:
        return data, [*(["No priced venue meets attendance and selection requirements."] if not venues else []),
                      *(["No priced meal option meets selection requirements."] if not meals else [])]

    best = None
    closest = None
    for (venue_id, room), (food_id, meal) in product(sorted(venues), sorted(meals)):
        if monotonic() >= deadline:
            raise ValueError("Venue and meal search reached its time limit.")
        food_cost = quantity * amount(meal["unit_cost"]) + amount(meal.get("fixed_cost", 0))
        total = amount(room["cost"]) + food_cost + judge_cost + committed
        candidate = {
            "venue": {**venue, **room, "selected": venue_id, "required_capacity": required_capacity},
            "schedule": schedule,
            "food": {**food, **meal, "selected": food_id, "headcount": headcount,
                     "quantity": quantity, "cost": food_cost},
            "judging": {**judging, "cost": judge_cost},
            "budget": {**budget, "total": total, "remaining": amount(cap) - total,
                       "lines": {"venue": amount(room["cost"]), "food": food_cost,
                                 "judging": judge_cost, "commitments": committed}},
        }
        failures = requirements(candidate, rules)
        if total > amount(cap):
            failures.append(f"Total {total} exceeds budget cap {cap}.")
        if not failures and (best is None or total < best[0]):
            best = total, candidate
        if failures and (closest is None or (len(failures), total) < closest[:2]):
            closest = len(failures), total, failures
    if best is not None:
        return best[1], []
    return data, closest[2]
