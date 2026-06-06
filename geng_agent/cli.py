from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Heavy imports (pipeline, supervisor, status, config) are loaded lazily inside each
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

    supervise = subparsers.add_parser("supervise", help="用监督/反思层调度审查流水线。")
    _add_common_review_args(supervise, include_resume=True)
    supervise.add_argument("--max-supervisor-steps", type=int, default=12, help="监督循环最大步数。")
    supervise.add_argument("--max-stage-retries", type=int, default=2, help="同一阶段最大重试次数。")
    supervise.add_argument("--no-llm-reflection", action="store_true", help="只使用本地确定性监督决策，不调用 LLM 反思。")

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
    parser.add_argument("--json-repair-attempts", type=int, default=3, help="每轮 JSON 结构审查失败后的返修次数。")
    parser.add_argument("--code-review", action="store_true", help="生成复现项目后运行代码忠实度审查（对照已抽取的事实/任务做内容审查并按需返修）；默认关闭。")
    parser.add_argument("--code-review-attempts", type=int, default=1, help="代码忠实度审查发现 blocking 问题后的返修次数。")
    parser.add_argument("--code-review-model", default=None, help="代码忠实度审查使用的模型（异构审查者）；默认取 GENG_CODE_REVIEW_MODEL，未设则与主模型相同。")
    parser.add_argument("--code-review-timeout", type=float, default=1200.0, help="代码忠实度审查单次 LLM 请求超时时间，单位秒，默认 1200（整项目审查较慢，给足时间避免超时）。")


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
        from .pipeline import ReviewPipeline

        client = _build_client_or_error(args, parser)
        code_review_client = _build_code_review_client_or_error(args, parser) if args.code_review else None
        result = ReviewPipeline(client=client, code_review_client=code_review_client).run(
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
        )
        print(f"审查完成：{result.output_dir}")
        print(f"报告：{result.review_path}")
        print(f"Word 主报告：{result.review_docx_path}")
        print(f"复现项目：{result.repro_project_dir}")
        print(f"自动运行结果：{result.runtime_passed}")
        print(f"结果级二次审查：{result.result_review_passed}")
        print(f"Word 结果审查：{result.result_review_docx_path}")
        return 0

    if args.command == "supervise":
        _warn_if_environment_incomplete()
        from .json_utils import pretty_json
        from .supervisor import SuperviseOptions, run_supervised_review

        client = _build_client_or_error(args, parser)
        code_review_client = _build_code_review_client_or_error(args, parser) if args.code_review else None
        result = run_supervised_review(
            client=client,
            paper_path=args.paper,
            output_dir=args.out,
            code_review_client=code_review_client,
            options=SuperviseOptions(
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
                resume=args.resume,
                template_fallback=not args.no_template_fallback,
                code_review=args.code_review,
                code_review_attempts=args.code_review_attempts,
                max_supervisor_steps=args.max_supervisor_steps,
                max_stage_retries=args.max_stage_retries,
                use_llm_reflection=not args.no_llm_reflection,
            ),
        )
        print(f"监督完成：{result.output_dir}")
        print(f"是否完成：{result.completed}")
        print(f"最终决策：{pretty_json(result.final_decision)}")
        print(f"反思记录：{result.final_reflection_path}")
        return 0

    if args.command == "status":
        from .json_utils import pretty_json
        from .status import inspect_case_status

        print(pretty_json(inspect_case_status(args.out)))
        return 0

    parser.error(f"未知命令：{args.command}")
    return 2


def _warn_if_environment_incomplete() -> None:
    """Print a stderr warning at the start of review/supervise when the local
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
