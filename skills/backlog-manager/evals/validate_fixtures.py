import json

with open("stress-tests/tribunal_ties_fixture.json") as f:
    ties = json.load(f)

with open("stress-tests/critical_path_fixture.json") as f:
    cp = json.load(f)

tie_items = ties["items"]
cp_items = cp["items"]
scenarios = ties["_meta"]["scenarios"]

assert len([i for i in tie_items if i["id"].startswith("TIE")]) == 10
assert len([i for i in cp_items if i.get("is_critical_path")]) == 7
assert len([i for i in cp_items if i["id"].startswith("NOISE")]) == 23

chain_ids = {i["id"]: i for i in cp_items if i.get("is_critical_path")}
all_ids = {i["id"] for i in cp_items}
for item in chain_ids.values():
    for blocker in item["blocked_by"]:
        assert blocker in all_ids, f"{item['id']} has unknown blocker {blocker}"
    for blocked in (item.get("blocks") or []):
        assert blocked in all_ids, f"{item['id']} blocks unknown id {blocked}"

print("Fixtures valid")
print(f"  Tie items:        {len(tie_items)}")
print(f"  Critical chain:   {len(chain_ids)} items")
print(f"  Noise items:      {len([i for i in cp_items if i['id'].startswith('NOISE')])}")
print(f"  Scenarios mapped: {list(scenarios.keys())}")

VALID_STATUSES = {"todo", "in-progress", "done", "discarded", "blocked"}
RECOMMEND_ELIGIBLE = {"todo", "blocked"}
errors = []

# Schema: valid statuses
for item in cp_items + tie_items:
    if item["status"] not in VALID_STATUSES:
        errors.append(f"{item['id']} has invalid status: {item['status']!r}")

# Noise isolation: no NOISE item links to critical chain
for item in cp_items:
    if not item["id"].startswith("NOISE"):
        continue
    for ref in (item.get("blocks") or []) + (item.get("blocked_by") or []):
        if ref in chain_ids:
            errors.append(f"{item['id']} links to critical chain item {ref}")

# Recommend eligibility: each scenario has at least one eligible item
for scenario, ids in scenarios.items():
    pair = [i for i in tie_items if i["id"] in ids]
    if not any(i["status"] in RECOMMEND_ELIGIBLE for i in pair):
        errors.append(f"Scenario {scenario!r} has no recommend-eligible items")

# Pair symmetry: shared fields must be identical within each tie pair
COMPARE_FIELDS = ["priority", "tags", "assigned_to"]
tie_scenarios = {
    "Tribunal_Tie_Breaking_Complexity": ("TIE-001", "TIE-002"),
    "Tribunal_Tie_Breaking_Equal":      ("TIE-005", "TIE-006"),
    "Tribunal_Tie_Breaking_Reopens":    ("TIE-009", "TIE-010"),
}
tie_map = {i["id"]: i for i in tie_items}
for scenario, (a_id, b_id) in tie_scenarios.items():
    a, b = tie_map[a_id], tie_map[b_id]
    for field in COMPARE_FIELDS:
        if a.get(field) != b.get(field):
            errors.append(f"{scenario}: {a_id}.{field}={a.get(field)!r} != {b_id}.{field}={b.get(field)!r}")

if errors:
    print("Additional checks FAILED:")
    for e in errors:
        print(f"  x {e}")
else:
    print("  Additional checks: schema, noise isolation, eligibility, pair symmetry ✅")
