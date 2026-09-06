# Independent delivery and scientific cost baselines

Final assembly writes `README.md`, `requirements.repro.txt`, `constraints.repro.txt`, and `installation.json`. Writer instructions are retained in `task_notes/`. The pinned package set starts at the declared requirements and follows metadata dependencies in the selected execution interpreter; it does not install unrelated host inventory. Accelerator-local versions such as `+cu126` remain pinned. A PyTorch wheel index inferred from a build tag is explicitly labelled a reconstruction source, not the original download provenance.

`execution_evidence.json` keeps original host receipts and maps their original paths/hashes to packaged files. When assembly renames configurations or normalizes text bytes, only necessary original executed inputs are preserved under `execution_records/`; archived Python files use `.py.original` so they are evidence rather than a second source tree. This mapping is not a new scientific execution. Writer-modifiable receipt copies are replaced from host records; unavailable original bytes are reported explicitly and produce a portability warning. Runtime queues and temporary caches are omitted.

Installation export compares its selected dependency versions with the existing execution receipts. A version changed after a task finished is listed by task in `execution_evidence.json`, `installation.json`, and the README warnings. Only the intersection of exported dependencies and observed packages is compared; unrelated host packages do not create warnings. This check adds no interpreter probe and does not change scientific outcomes.

Two checks have different meanings. Relocated smoke tests relative paths using an existing runtime. Clean reconstruction creates a virtual environment without system site packages, installs only declared requirements under exported constraints, performs `pip check`, and executes the relocated smoke with that new interpreter. The final package performs clean reconstruction once; the installation cache is keyed by dependency files and interpreter/platform identity. Cache hits still execute the current project's smoke. An unavailable wheel, installation timeout, or smoke failure is recorded in `audit/03c_project_portability.json` under `clean_environment`; it does not rewrite scientific outcomes.

To check a delivered project locally with the selected Python environment (replace the case and cache paths with real local paths):

```sh
python -m geng_agent.environment_rebuild PATH/TO/CASE/repro_project --cache-dir PATH/TO/clean-environment-cache --output PATH/TO/CASE/clean_environment_check.json
```

The stdlib-only fixture in `tests/test_delivery_quality_cost.py::DeliveryQualityCostTests::test_real_clean_venv_smoke_and_cached_environment` creates a real temporary virtual environment and verifies both a first install and cached installation. It makes no package downloads.

`python -m pytest tests/test_delivery_end_to_end.py -q` exercises code generation, a real guarded scientific process, independent evidence reading, final report publication, package assembly, and clean-environment smoke. Only the Codex text generator/reviewer is substituted with deterministic scripts. This validates engineering integration; it is not evidence of a real model's scientific judgement or cross-paper success rate.

Every Codex invocation requests JSONL events. Completed-turn `usage` is recorded before transcript truncation in a unique `codex_usage_events/<invocation_id>.json`; repeated stage labels never overwrite costs. `run_cost.json` includes invocation-local and cumulative Codex totals. Missing token usage is `null` with observed tokens and missing-call counts, not zero. Dollar costs are not inferred for subscription/account-based Codex usage. Historical costs cannot be reconstructed if the original CLI did not expose usage.

An offline quality benchmark may contain `quality_baseline.json` with a version and `tasks` entries specifying `task_id`, `expected_outcome`, `paper_family`, `failure_mode`, `pair_id`, and optionally `expected_rerun_allowed`. Actual review results come from task Reporter statuses, or `quality_results.json` with `tasks` entries containing `task_id`, `outcome`, and `host_action` for blind fixtures. The benchmark reports false successes, false failures, rerun errors, missing assessments, per-family counts, and cumulative Codex usage. Baseline labels must be withheld from the reviewer. The paired fixtures in `tests/fixtures/scientific_calibration/` exercise broad scientific failure classes rather than one paper's appearance.

All three final Markdown reports receive a host-rendered task outcome and criterion table. Missing Editor prose no longer removes a failed task's terminal facts. The Editor still writes explanations and is not given authority to change the scientific results.
