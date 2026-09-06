# Offline Benchmark

Cases: 3

Cost columns use cumulative case costs when a run ledger is available. Older cases combine observed Codex history with only the last recorded API/time invocation; missing usage is unknown.

| Case | Stages | Facts | Tasks | Runtime | Matched | Explained gap | Failed | Wall clock (s) | LLM calls | Total tokens | Cost (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| case_observe_owc_2504_02134_20260823 | 18/19 | 32 | 4 | 4/4 | 0 | 0 | 0 | 23.411 | 27 | unknown | unknown |
| case_twc_otfs_multisat_20260719_023016 | 0/0 | 33 | 7 | 1/7 | 0 | 0 | 6 | - | 37 | unknown | unknown |
| deepsc_s_2102_12605_full_20260722_001 | 6/19 | 31 | 7 | - | 0 | 0 | 0 | - | 5 | unknown | unknown |
| **Total** | - | 96 | 18 | - | 0 | 0 | 6 | - | 69 | unknown | unknown |

## Stage Status

| Stage | OK | Not OK | Total |
| --- | ---: | ---: | ---: |
| paper | 2 | 0 | 2 |
| engineering_facts | 2 | 0 | 2 |
| paper_thesis | 2 | 0 | 2 |
| repro_tasks | 2 | 0 | 2 |
| execution_plan | 1 | 1 | 2 |
| experiment_index | 2 | 0 | 2 |
| scientific_architecture | 2 | 0 | 2 |
| environment_lock | 1 | 1 | 2 |
| foundation_manifest | 1 | 1 | 2 |
| repro_project_manifest | 0 | 2 | 2 |
| repro_project | 1 | 1 | 2 |
| runtime | 1 | 1 | 2 |
| verification_result | 1 | 1 | 2 |
| reproduction_report | 1 | 1 | 2 |
| result_review | 1 | 1 | 2 |
| review | 1 | 1 | 2 |
| review_docx | 1 | 1 | 2 |
| reproduction_report_docx | 1 | 1 | 2 |
| result_review_docx | 1 | 1 | 2 |

## Scientific outcomes

Runtime coverage measures process completion. These terminal scientific results are recorded separately.

| Case | Reproduced | With assumptions | Missing information | Not reproduced | Execution failed | Unassessed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| case_observe_owc_2504_02134_20260823 | 0 | 0 | 0 | 4 | 0 | 0 |
| case_twc_otfs_multisat_20260719_023016 | 0 | 0 | 0 | 0 | 0 | 7 |
| deepsc_s_2102_12605_full_20260722_001 | 0 | 0 | 0 | 0 | 0 | 7 |
| **Total** | 0 | 0 | 0 | 4 | 0 | 14 |

## Scientific quality and cumulative Codex cost

Independent labels are optional. Missing labels/results are unassessed, never counted as correct.

Assessed/labeled: 0/0; false success: 0; false failure: 0; unjustified/missed reruns: 0.

Cumulative Codex calls: 69; tokens: unknown; calls without complete usage: 69.
