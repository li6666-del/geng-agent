from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Heavy imports (pipeline, status, config) are loaded lazily inside each
# command branch, so `geng-agent doctor` still runs on a machine that is missing
# orchestrator dependencies — exactly the situation you need it to diagnose.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geng-agent",
        description="通信领域论文工程复现审查 CLI。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="审查论文并生成复现项目。")
    _add_common_review_args(review, include_resume=False)
    review.add_argument("--no-resume", action="store_true", help="不复用已有阶段产物，从头重新运行。")

    status = subparsers.add_parser("status", help="检查已有 case 目录的断点续跑状态。")
    status.add_argument("out", type=Path, help="已有输出目录。")

    subparsers.add_parser(
        "doctor",
        help="自检本机环境（Python 版本 + 运行依赖 + 复现白名单库）；建议在输入 PDF 前先运行。",
    )
    return parser


def _add_common_review_args(parser: argparse.ArgumentParser, *, include_resume: bool) -> None:
    parser.add_argument("paper", type=Path, help="论文文件，支持 PDF/TXT/Markdown。")
    parser.add_argument("--out", type=Path, required=True, help="输出目录。")
    parser.add_argument("--api-key", default=None, help="OpenAI 兼容 API key。")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容 API base URL。")
    parser.add_argument("--model", default=None, help="模型名。")
    parser.add_argument("--max-pages", type=int, default=None, help="PDF 最多读取页数。")
    parser.add_argument("--temperature", type=float, default=0.1, help="LLM 采样温度，默认 0.1。")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次 LLM 请求超时时间，单位秒。")
    parser.add_argument("--tasks-timeout", type=float, default=300.0, help="第二轮生成复现任务的单次 LLM 请求超时时间，单位秒。")
    parser.add_argument("--project-timeout", type=float, default=1200.0, help="第三轮生成复现项目的单次 LLM 请求超时时间，单位秒。")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default=None, help="DeepSeek V4 Pro thinking 开关。")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default=None, help="推理模型 reasoning_effort 参数。")
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--run-repro", dest="run_repro", action="store_true", help="显式运行生成的复现代码，并启用受限返修。")
    run_group.add_argument("--no-run-repro", dest="run_repro", action="store_false", help="不自动运行生成代码；这是默认行为。")
    parser.set_defaults(run_repro=False)
    parser.add_argument("--no-result-review", action="store_true", help="在 --run-repro 成功后也不执行结果级多模态二次审查。")
    if include_resume:
        resume_group = parser.add_mutually_exclusive_group()
        resume_group.add_argument("--resume", dest="resume", action="store_true", help="复用已有阶段产物；这是默认行为。")
        resume_group.add_argument("--no-resume", dest="resume", action="store_false", help="不复用已有阶段产物，从头重新运行。")
        parser.set_defaults(resume=True)
    parser.add_argument("--no-template-fallback", action="store_true", help="禁用本地确定性兜底；默认启用以提高端到端稳定性。")
    parser.add_argument("--repair-attempts", type=int, default=2, help="复现代码运行失败后的自动修复次数。")
    parser.add_argument("--repair-backend", choices=("llm", "hybrid", "openhands"), default="hybrid", help="复现代码运行失败后的修复后端；默认 hybrid，先 LLM 后 OpenHands。")
    parser.add_argument("--openhands-timeout", type=float, default=900.0, help="OpenHands 候选修复和验收运行超时时间，单位秒。")
    parser.add_argument("--openhands-max-iterations", type=int, default=25, help="OpenHands 候选修复最大迭代步数。")
    parser.add_argument("--run-timeout", type=float, default=120.0, help="单次复现运行超时时间，单位秒。")
    parser.add_argument("--json-repair-attempts", type=int, default=5, help="每轮 JSON 结构审查失败后的返修次数（默认 5：实跑发现最难的科学文件 src/modulation.py 偶发语法/结构错，3 次重试不够会拖垮整个逐任务项目→兜底；只在失败时才追加重试，文件一次过则无额外开销）。")
    parser.add_argument("--code-review", action="store_true", help="生成复现项目后运行代码忠实度审查（对照已抽取的事实/任务做内容审查并按需返修）；默认关闭。")
    parser.add_argument("--code-review-attempts", type=int, default=5, help="代码忠实度审查发现 blocking 问题后的返修轮数；默认 5（每轮含一次修复+一次复审，慢模型会更耗时）。")
    parser.add_argument("--code-review-model", default=None, help="代码忠实度审查使用的模型（异构审查者）；默认取 GENG_CODE_REVIEW_MODEL，未设则与主模型相同。")
    parser.add_argument("--generation-model", default=None, help="仅用于第三轮逐文件代码生成 + Phase-D 科学返修的“专用 coder”模型（默认取 GENG_GEN_MODEL，未设则与主模型相同）。主模型须保留多模态（事实/思路/plan/结果审查都用图），但逐文件代码生成不用图——可在此指向更强、哪怕无多模态的代码模型（如 mimo-v2.5-pro）去啃最难的 src/modulation.py 并真正修对科学建模。Key/base 缺省回退 GENG_LLM_*（同端点变体只填模型名即可）。")
    parser.add_argument("--code-review-timeout", type=float, default=1200.0, help="代码忠实度审查单次 LLM 请求超时时间，单位秒，默认 1200（整项目审查较慢，给足时间避免超时）。")
    parser.add_argument("--facts-gap-rounds", type=int, default=3, help="第一轮事实抽取后的“查漏补缺”追加轮数（确定性覆盖校验图/表锚点 + 定向补抽遗漏事实），循环到一轮无新增为止；默认 3，设 0 关闭。")
    parser.add_argument("--tasks-gap-rounds", type=int, default=3, help="第二轮复现任务设计后的“查漏补缺”追加轮数（确定性校验每个可复现实验是否都有任务 + 为遗漏实验补任务），循环到全覆盖或无新增为止；默认 3，设 0 关闭。")
    parser.add_argument("--per-task-layout", action="store_true", help="第三轮按“每任务一脚本”布局生成：LLM 只生成共享 src + 每个复现任务一个 tasks/<module>.py（薄驱动调 _io），run_experiment.py 由本地注入分发器；运行时每个任务起独立子进程+独立超时+部分成功+按脚本返修（硬隔离）。默认关闭（单脚本布局）。")
    parser.add_argument("--science-loop", action="store_true", help="开启“论文思路”闭环：第一轮后额外提炼论文核心主张/机制/方法预期排序（paper_thesis），锚进生成与结果审查，并按预期排序对复现做一致性校验+回喂科学返修。建议与 --per-task-layout 同用（按任务隔离才能定位返修）。默认关闭。")
    parser.add_argument("--science-repair-rounds", type=int, default=1, help="论文思路闭环里“科学返修”的最大轮数：当结果审查判定某实验未支持论文结论（多为方法排序与论文相反）时，按诊断回喂、定向重写涉事 src/ 与任务脚本并重跑+复审；每轮可逆，仅在错配严格减少且不丢覆盖时保留，否则回滚。默认 1，设 0 关闭（仅锚定+审查、不自动返修）。仅在 --science-loop + --per-task-layout 下生效。")
    parser.add_argument("--science-repair-backend", choices=("llm", "codex"), default="llm", help="科学返修的执行后端。llm（默认）=一把式逐文件重写；codex=把项目交给无头 Codex CLI 会话迭代调试（复跑任务、打印中间量、定位再改），适合“公式抄对了但建模没建对”的深层问题。codex 后端要求本机已装 Codex CLI 并登录（或设 GENG_CODEX_CMD 指定命令）；受信任文件（_io.py/分发器/manifest/requirements）即使被改也会被确定性还原，最终留不留仍由“错配严格减少且不丢覆盖”的闸裁决。")
    parser.add_argument("--science-repair-timeout", type=float, default=1800.0, help="codex 科学返修单轮会话的超时时间，单位秒，默认 1800（半小时；agent 要反复跑仿真验证，给足时间）。")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        from .preflight import check_environment, format_report

        report = check_environment()
        print(format_report(report))
        return 1 if report.fatal else 0

    if args.command == "review":
        _warn_if_environment_incomplete()
        from .config import build_generation_client, build_secondary_extraction_client
        from .pipeline import ReviewPipeline

        client = _build_client_or_error(args, parser)
        code_review_client = _build_code_review_client_or_error(args, parser) if args.code_review else None
        # Optional second multimodal extraction model (GENG_LLM2_*) for the round-1 ensemble;
        # None when unconfigured -> single-model behavior.
        extraction_client_2 = build_secondary_extraction_client(temperature=args.temperature, timeout=args.timeout)
        # Optional separate coder for round-3 codegen + Phase-D repair (e.g. mimo-v2.5-pro,
        # which has no multimodal -> can't be the main client). None -> main client codes.
        generation_client = build_generation_client(
            model=args.generation_model,
            temperature=args.temperature,
            timeout=args.timeout,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
        )
        result = ReviewPipeline(
            client=client,
            code_review_client=code_review_client,
            extraction_client_2=extraction_client_2,
            generation_client=generation_client,
        ).run(
            paper_path=args.paper,
            output_dir=args.out,
            max_pages=args.max_pages,
            run_repro=args.run_repro,
            repair_attempts=args.repair_attempts,
            run_timeout=args.run_timeout,
            repair_backend=args.repair_backend,
            openhands_timeout=args.openhands_timeout,
            openhands_max_iterations=args.openhands_max_iterations,
            json_repair_attempts=args.json_repair_attempts,
            tasks_timeout=args.tasks_timeout,
            project_timeout=args.project_timeout,
            result_review=not args.no_result_review,
            resume=not args.no_resume,
            template_fallback=not args.no_template_fallback,
            code_review=args.code_review,
            code_review_attempts=args.code_review_attempts,
            facts_gap_rounds=args.facts_gap_rounds,
            tasks_gap_rounds=args.tasks_gap_rounds,
            per_task_layout=args.per_task_layout,
            science_loop=args.science_loop,
            science_repair_rounds=args.science_repair_rounds,
            science_repair_backend=args.science_repair_backend,
            science_repair_timeout=args.science_repair_timeout,
        )
        print(f"审查完成：{result.output_dir}")
        print(f"报告：{result.review_path}")
        print(f"Word 主报告：{result.review_docx_path}")
        print(f"复现项目：{result.repro_project_dir}")
        print(f"自动运行结果：{result.runtime_passed}")
        print(f"结果级二次审查：{result.result_review_passed}")
        print(f"Word 结果审查：{result.result_review_docx_path}")
        return 0

    if args.command == "status":
        from .json_utils import pretty_json
        from .status import inspect_case_status

        print(pretty_json(inspect_case_status(args.out)))
        return 0

    parser.error(f"未知命令：{args.command}")
    return 2


def _warn_if_environment_incomplete() -> None:
    """Print a stderr warning at the start of review when the local
    environment is missing libraries that lead to template fallback. Best-effort:
    never blocks the run and never raises."""
    try:
        from .preflight import check_environment, environment_warning

        warning = environment_warning(check_environment())
    except Exception:
        return
    if warning:
        print(warning, file=sys.stderr)


def _build_client_or_error(args: argparse.Namespace, parser: argparse.ArgumentParser):
    from .config import build_llm_client, get_config_value

    try:
        return build_llm_client(
            api_key=args.api_key or get_config_value("GENG_LLM_API_KEY"),
            base_url=args.base_url or get_config_value("GENG_LLM_BASE_URL"),
            model=args.model or get_config_value("GENG_LLM_MODEL"),
            temperature=args.temperature,
            timeout=args.timeout,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
        )
    except ValueError as exc:
        parser.error(str(exc))


def _build_code_review_client_or_error(args: argparse.Namespace, parser: argparse.ArgumentParser):
    """Client for the code-faithfulness review. Uses the heterogeneous reviewer model
    when GENG_CODE_REVIEW_MODEL / --code-review-model is set; otherwise reviews with the
    main model. Either way the review gets its own (longer) --code-review-timeout so a slow
    model does not time out mid-review."""
    from .config import build_code_review_client, build_llm_client, get_config_value

    try:
        client = build_code_review_client(
            model=args.code_review_model,
            temperature=args.temperature,
            timeout=args.code_review_timeout,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
        )
        if client is not None:
            return client
        return build_llm_client(
            api_key=args.api_key or get_config_value("GENG_LLM_API_KEY"),
            base_url=args.base_url or get_config_value("GENG_LLM_BASE_URL"),
            model=args.model or get_config_value("GENG_LLM_MODEL"),
            temperature=args.temperature,
            timeout=args.code_review_timeout,
            thinking=args.thinking,
            reasoning_effort=args.reasoning_effort,
        )
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
