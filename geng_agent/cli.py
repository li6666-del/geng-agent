from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis_limits import (
    DEFAULT_ANALYSIS_AGENT_WIDTH,
    MAX_ANALYSIS_AGENT_WIDTH,
    normalize_analysis_agent_width,
)

# Heavy imports (pipeline, status, config) are loaded lazily inside each
# command branch, so `geng-agent doctor` still runs on a machine that is missing
# orchestrator dependencies — exactly the situation you need it to diagnose.


def _analysis_agent_width_arg(value: str) -> int:
    try:
        return normalize_analysis_agent_width(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
    benchmark = subparsers.add_parser("benchmark", help="离线汇总多个 case 的复现覆盖、结论、耗时和成本。")
    benchmark.add_argument("cases", nargs="+", type=Path, help="一个或多个 case 输出目录。")
    benchmark.add_argument("--out", type=Path, required=True, help="benchmark JSON/Markdown 输出目录。")
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
    parser.add_argument("--project-timeout", type=float, default=1200.0, help="第三轮单个 task writer 子进程的默认超时，单位秒；没有总墙钟预算。")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default=None, help="DeepSeek V4 Pro thinking 开关。")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default=None, help="推理模型 reasoning_effort 参数。")
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--run-repro", dest="run_repro", action="store_true", help="允许每个 task writer 运行自己的 full 并进行最多 5 轮自我修正。")
    run_group.add_argument("--no-run-repro", dest="run_repro", action="store_false", help="不自动运行生成代码；这是默认行为。")
    parser.set_defaults(run_repro=False)
    parser.add_argument("--no-result-review", action="store_true", help="不生成 task writer 自审结果对比报告。")
    if include_resume:
        resume_group = parser.add_mutually_exclusive_group()
        resume_group.add_argument("--resume", dest="resume", action="store_true", help="复用已有阶段产物；这是默认行为。")
        resume_group.add_argument("--no-resume", dest="resume", action="store_false", help="不复用已有阶段产物，从头重新运行。")
        parser.set_defaults(resume=True)
    parser.add_argument("--no-analysis-fallback", action="store_true", help="禁用前两阶段本地确定性兜底；默认启用以提高端到端稳定性。")
    parser.add_argument("--run-timeout", type=float, default=120.0, help="单次复现运行超时时间，单位秒。")
    parser.add_argument("--json-repair-attempts", type=int, default=5, help="前两阶段 Codex/兼容 LLM 输出未通过 JSON schema 时的重试次数；只在结构校验失败时追加调用。")
    parser.add_argument("--facts-gap-rounds", type=int, default=6, help="第一轮事实抽取后的查漏补缺轮数；连续两轮没有新增、补强或纠错才停止，默认最多 6 轮。")
    parser.add_argument("--tasks-gap-rounds", type=int, default=6, help="第二轮任务查漏补缺轮数；全覆盖可立即停止，否则连续两轮无有效变化才停止，默认最多 6 轮。")
    parser.add_argument(
        "--analysis-agent-width",
        type=_analysis_agent_width_arg,
        default=DEFAULT_ANALYSIS_AGENT_WIDTH,
        help=f"Codex 前两阶段每轮并行 analysis 子智能体数量；默认 2，设 1 回退旧单子智能体，最大 {MAX_ANALYSIS_AGENT_WIDTH}。",
    )
    parser.add_argument(
        "--analysis-backend",
        choices=("codex", "llm"),
        default="codex",
        help="前两阶段事实抽取/任务拆解 backend；默认 codex，llm 为旧 OpenAI-compatible 兼容路径。",
    )
    parser.add_argument(
        "--codex-analysis-timeout",
        type=float,
        default=None,
        help="前两阶段单个 Codex analysis 子进程超时，单位秒；默认 600。",
    )
    parser.add_argument(
        "--codex-agent-rounds",
        type=int,
        default=5,
        help="第三轮每个自治 task writer 的最大内部科学迭代轮数；默认 5，未完全匹配前要求持续迭代。",
    )
    parser.add_argument("--codex-agent-timeout", type=float, default=None, help="单个自治 task writer 子进程超时，单位秒；默认复用 --project-timeout，未设置时 1800。")


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
        from .config import build_secondary_extraction_client
        from .pipeline import ReviewPipeline

        if args.analysis_backend == "llm":
            client = _build_client_or_error(args, parser)
            # Optional second multimodal extraction model (GENG_LLM2_*) for the round-1 ensemble;
            # None when unconfigured -> single-model behavior.
            extraction_client_2 = build_secondary_extraction_client(temperature=args.temperature, timeout=args.timeout)
        else:
            client = None
            extraction_client_2 = None
        result = ReviewPipeline(
            client=client,
            extraction_client_2=extraction_client_2,
        ).run(
            paper_path=args.paper,
            output_dir=args.out,
            max_pages=args.max_pages,
            run_repro=args.run_repro,
            run_timeout=args.run_timeout,
            json_repair_attempts=args.json_repair_attempts,
            tasks_timeout=args.tasks_timeout,
            project_timeout=args.project_timeout,
            result_review=not args.no_result_review,
            resume=not args.no_resume,
            analysis_fallback=not args.no_analysis_fallback,
            facts_gap_rounds=args.facts_gap_rounds,
            tasks_gap_rounds=args.tasks_gap_rounds,
            analysis_agent_width=args.analysis_agent_width,
            analysis_backend=args.analysis_backend,
            codex_analysis_timeout=args.codex_analysis_timeout,
            codex_agent_rounds=args.codex_agent_rounds,
            codex_agent_timeout=args.codex_agent_timeout,
        )
        print(f"审查完成：{result.output_dir}")
        print(f"报告：{result.review_path}")
        print(f"Word 主报告：{result.review_docx_path}")
        print(f"复现项目：{result.repro_project_dir}")
        print(f"自动运行结果：{result.runtime_passed}")
        print(f"Writer 自审对比报告：{result.result_review_passed}")
        print(f"Word 结果审查：{result.result_review_docx_path}")
        return 0

    if args.command == "benchmark":
        from .benchmark import build_benchmark, write_benchmark_reports

        report = build_benchmark(args.cases)
        json_path, markdown_path = write_benchmark_reports(
            report,
            json_path=args.out / "benchmark.json",
            markdown_path=args.out / "benchmark.md",
        )
        print(f"Benchmark JSON：{json_path}")
        print(f"Benchmark Markdown：{markdown_path}")
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
    environment is missing libraries that weaken reproduction quality. Best-effort:
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


