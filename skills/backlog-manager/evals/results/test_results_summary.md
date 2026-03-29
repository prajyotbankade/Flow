# Flow Test Results Summary — 2026-03-28

## Run Config
- Model: `qwen2.5-coder:7b` (switched from 14b to avoid timeouts)
- Tests: 19 run, 4 deselected (3 Anthropic-gated + 1 hanging `Pulse_Coordination_Context`)
- Duration: 17 min 6 sec

## Results: 15 passed, 4 failed

### Passed (15)
| Test | Type |
|------|------|
| `test_flow_skill_live[Impact_vs_Unblock]` | Flow Scoring |
| `test_flow_skill_live[Critical_Bug_Boost]` | Flow Scoring |
| `test_flow_skill_live[Readiness_Partial_Block]` | Flow Scoring |
| `test_tribunal_justification[Tribunal_Counterfactual_Reasoning]` | Tribunal |
| `test_graph_cascade_correctness` | Graph (structural) |
| `test_graph_conflict_detection` | Graph (structural) |
| `test_graph_no_conflict_same_agent` | Graph (structural) |
| `test_pulse_payload_completeness` | Pulse (structural) |
| `test_pulse_agent_filter` | Pulse (structural) |
| `test_graph_coordination[Conflict_Detection_Response]` | Graph (LLM) |
| `test_policy_list_structure` | Policy |
| `test_policy_crud_lifecycle` | Policy |
| `test_policy_log_structure` | Policy |
| `test_policy_log_limit_param` | Policy |
| `test_policy_evaluate_structure` | Policy |

### Failed (4)

#### 1. `test_flow_skill_live[Unblocker_Strategy]` — REASONING QUALITY
- **Score:** 0.0 (threshold 0.6)
- **Root cause:** LLM didn't reference `unblock_score_breakdown` field or `unblocking_multiplier` as reason for Root Task outranking Blocked Task. Also didn't explain `blocked_penalty` proportional to blocker readiness (not flat -3.0).
- **Fix needed:** Improve prompt in `flow_skill.py` to more explicitly instruct the LLM to cite `unblock_score_breakdown` and `unblocking_multiplier` fields when explaining unblocker strategy recommendations.

#### 2. `test_tribunal_justification[Tribunal_Leverage_Wins]` — TIMEOUT
- **Score:** 0.0
- **Root cause:** `run_flow_tribunal` returned a read timeout error instead of an actual answer.
- **Fix needed:** Check `run_flow_tribunal()` in test file — likely still uses 14b model or has tight timeout. Align with 7b model and increase timeout.

#### 3. `test_tribunal_justification[Tribunal_Confidence_Low]` — TIMEOUT
- **Score:** 0.0
- **Root cause:** Same timeout issue as Tribunal_Leverage_Wins.
- **Fix needed:** Same as above.

#### 4. `test_graph_coordination[Critical_Path_Identification]` — TIMEOUT
- **Score:** 0.0
- **Root cause:** `run_flow_graph` returned a read timeout error.
- **Fix needed:** Check `run_flow_graph()` — same timeout/model issue.

### Skipped / Deselected (4)
| Test | Reason |
|------|--------|
| `test_policy_evaluate_fires` | Requires Anthropic API key |
| `test_policy_rule_engine` | Requires Anthropic API key |
| `test_policy_evaluate_fires_llm` | Requires Anthropic API key |
| `test_graph_coordination[Pulse_Coordination_Context]` | Hung indefinitely, manually skipped |

## Key Files to Fix
- `test_flow_live.py` — `run_flow_tribunal()` and `run_flow_graph()` likely use 14b model or have tight timeouts
- `flow_skill.py` — Prompt needs stronger instruction to cite `unblock_score_breakdown` and `unblocking_multiplier` fields

## Changes Already Made This Session
1. `test_flow_live.py` line with `local_qwen` — switched from `qwen2.5-coder:14b` to `qwen2.5-coder:7b`
2. `flow_skill.py` — added IMPORTANT instruction about explaining `blocked_penalty` with blocker readiness %
