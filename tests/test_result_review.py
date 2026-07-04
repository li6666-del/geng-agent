from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from geng_agent.llm import LLMImage, OpenAICompatibleClient
from geng_agent.result_review import (
    build_result_json_retry_prompt,
    encode_png_for_llm,
    render_result_review_markdown,
    select_paper_pages,
    select_paper_pages_for_task,
    summarize_csv_file,
)
from geng_agent.schemas import validate_stage


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
REVIEW_DIMENSIONS = [
    "artifact_coverage",
    "reproduction_logic",
    "trend_shape",
    "metric_axis_scale",
    "baseline_comparison",
    "statistical_reliability",
    "conclusion_support",
]


def scientific_dimension_reviews(rating: str = "acceptable") -> list[dict]:
    return [
        {
            "dimension": dimension,
            "rating": rating,
            "finding": f"{dimension} 维度有基础证据。",
            "evidence": ["outputs/results.csv"],
        }
        for dimension in REVIEW_DIMENSIONS
    ]


class ResultReviewUnitTests(unittest.TestCase):
    def test_summarize_csv_file_extracts_rows_and_numeric_stats(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.csv"
            path.write_text("snr_db,ber,label\n0,0.1,a\n2,0.05,b\n4,0.02,c\n", encoding="utf-8")

            summary = summarize_csv_file(path)

            self.assertEqual(summary["header"], ["snr_db", "ber", "label"])
            self.assertEqual(summary["total_data_rows"], 3)
            self.assertEqual(summary["numeric_columns"]["snr_db"]["trend"], "increasing")
            self.assertEqual(summary["numeric_columns"]["ber"]["trend"], "decreasing")

    def test_encode_png_for_llm_accepts_real_png_and_rejects_fake_png(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good = root / "plot.png"
            bad = root / "bad.png"
            good.write_bytes(base64.b64decode(PNG_B64))
            bad.write_text("png placeholder", encoding="utf-8")

            image = encode_png_for_llm(good, label="local_output:plot.png")

            self.assertEqual(image.mime_type, "image/png")
            self.assertEqual(image.label, "local_output:plot.png")
            with self.assertRaises(RuntimeError):
                encode_png_for_llm(bad, label="bad")

    def test_select_paper_pages_prefers_figure_claim_pages(self) -> None:
        paper = {
            "chunks": [
                {"chunk_id": "p1_c1", "page": 1, "section": "Intro", "text": "background"},
                {"chunk_id": "p5_c1", "page": 5, "section": "Simulation Results", "text": "Fig. 2 BER vs SNR"},
                {"chunk_id": "p6_c1", "page": 6, "section": "Table", "text": "baseline throughput"},
            ]
        }
        facts = {
            "engineering_facts": [
                {"source": {"page": 4}},
                {"source": {"page": 5}},
            ]
        }
        tasks = {"repro_tasks": [{"figure_or_claim": "Fig. 2"}]}

        pages = select_paper_pages(paper=paper, facts=facts, tasks=tasks, max_pages=3)

        self.assertEqual(pages, [5, 4, 6])

    def test_select_paper_pages_for_task_prioritizes_required_anchor_source(self) -> None:
        paper = {
            "chunks": [
                {"chunk_id": "p1_c1", "page": 1, "section": "Abstract", "text": "BPSK overview"},
                {"chunk_id": "p2_c1", "page": 2, "section": "Model", "text": "RS code and AWGN channel"},
                {"chunk_id": "p3_c1", "page": 3, "section": "Results", "text": "BER at SNR 3 dB is .000035303"},
            ]
        }
        facts = {
            "engineering_facts": [
                {
                    "type": "algorithm",
                    "name": "WiMAX PHY",
                    "source": {"page": 2, "quote": "RS code and AWGN channel"},
                },
                {
                    "type": "modulation",
                    "name": "BPSK",
                    "source": {"page": 1, "quote": "BPSK"},
                },
                {
                    "type": "simulation_parameter",
                    "name": "Reported BER value for BPSK at SNR=3 dB",
                    "source": {"page": 3, "quote": "BER at SNR 3 dB is .000035303"},
                },
            ]
        }
        task = {
            "task_id": "reproduce_numeric_anchor_awgn",
            "figure_or_claim": "Numerical anchor: BPSK at SNR=3 dB BER=.000035303",
            "required_facts": [
                {"type": "algorithm", "name": "WiMAX PHY"},
                {"type": "modulation", "name": "BPSK"},
                {"type": "simulation_parameter", "name": "Reported BER value for BPSK at SNR=3 dB"},
            ],
        }

        pages = select_paper_pages_for_task(paper=paper, facts=facts, task=task, max_pages=2)

        self.assertEqual(pages[0], 3)
        self.assertIn(3, pages)

    def test_select_paper_pages_for_task_prefers_target_figure_image_source(self) -> None:
        paper = {
            "chunks": [
                {"chunk_id": "p9_c1", "page": 9, "section": "Results", "text": "\nFig. 6. Heat map over p and q."},
                {"chunk_id": "p10_c1", "page": 10, "section": "Results", "text": "Fig. 6 illustrates the operating regime in text."},
            ]
        }
        facts = {
            "engineering_facts": [
                {
                    "type": "simulation_parameter",
                    "name": "asymptotic scaling assumptions",
                    "source": {"source_kind": "text", "page": 5, "quote": "K=M^p"},
                },
                {
                    "type": "metric",
                    "name": "Theorem 1 ULA spatial ZF scaling law",
                    "source": {"source_kind": "text", "page": 6, "quote": "ZF threshold"},
                },
                {
                    "type": "metric",
                    "name": "Theorem 2 ULA STAB scaling law",
                    "source": {"source_kind": "text", "page": 7, "quote": "STAB threshold"},
                },
                {
                    "type": "figure_claim",
                    "name": "Fig. 6 heat map setup",
                    "source": {"source_kind": "text", "page": 10, "quote": "Fig. 6 illustrates a heat map"},
                },
                {
                    "type": "figure_claim",
                    "name": "Fig. 6 visual heatmap structure",
                    "source": {"source_kind": "figure", "page": 9, "figure_ref": "Fig.6 STAB gain heat map"},
                },
            ]
        }
        task = {
            "task_id": "reproduce_fig_6_pq_stab_gain_heatmap",
            "figure_or_claim": "Fig. 6 heat map: STAB gain over the (p,q)-plane",
            "required_facts": [
                {"type": "simulation_parameter", "name": "asymptotic scaling assumptions"},
                {"type": "metric", "name": "Theorem 1 ULA spatial ZF scaling law"},
                {"type": "metric", "name": "Theorem 2 ULA STAB scaling law"},
                {"type": "figure_claim", "name": "Fig. 6 heat map setup"},
                {"type": "figure_claim", "name": "Fig. 6 visual heatmap structure"},
            ],
        }

        pages = select_paper_pages_for_task(paper=paper, facts=facts, task=task, max_pages=4)

        self.assertEqual(pages[0], 9)
        self.assertIn(9, pages)
        self.assertNotEqual(pages[0], 10)

    def test_result_review_schema_rejects_empty_experiment_reviews(self) -> None:
        issues = validate_stage(
            "result_review",
            {
                "overall_result_credibility": "medium",
                "overall_alignment": "partial_match",
                "experiment_reviews": [],
                "cross_experiment_findings": [],
                "recommended_human_checks": [],
                "note": "No experiments.",
            },
        )

        self.assertTrue(issues)

    def test_result_review_schema_requires_scientific_rubric(self) -> None:
        issues = validate_stage(
            "result_review_experiment",
            {
                "task_id": "reproduce_fig_1",
                "local_result_credibility": "medium",
                "paper_alignment": "partial_match",
                "paper_result_summary": "论文显示 BER 随 SNR 下降。",
                "local_result_summary": "本地 CSV 显示 BER 下降。",
                "differences": ["仅生成 smoke-scale 数据。"],
                "possible_causes": ["样本量少于论文。"],
                "evidence": ["outputs/results.csv"],
                "limitations": ["未精确读图。"],
                "confidence": "medium",
            },
        )

        issue_text = "\n".join(f"{issue.path}: {issue.message}" for issue in issues)
        self.assertIn("scientific_verdict", issue_text)
        self.assertIn("dimension_reviews", issue_text)

    def test_result_review_schema_rejects_invalid_dimension_and_rating(self) -> None:
        bad_dimensions = scientific_dimension_reviews()
        bad_dimensions[0] = {
            "dimension": "numeric_only",
            "rating": "great",
            "finding": "错误维度。",
            "evidence": ["outputs/results.csv"],
        }

        issues = validate_stage(
            "result_review_experiment",
            {
                "task_id": "reproduce_fig_1",
                "local_result_credibility": "medium",
                "paper_alignment": "partial_match",
                "scientific_verdict": "partially_supports_paper_claim",
                "dimension_reviews": bad_dimensions,
                "paper_result_summary": "论文显示 BER 随 SNR 下降。",
                "local_result_summary": "本地 CSV 显示 BER 下降。",
                "differences": ["趋势一致但样本少。"],
                "possible_causes": ["smoke 配置样本量较小。"],
                "evidence": ["outputs/results.csv"],
                "limitations": ["未精确读图。"],
                "confidence": "medium",
            },
        )

        issue_text = "\n".join(f"{issue.path}: {issue.message}" for issue in issues)
        self.assertIn("dimension_reviews[0].dimension", issue_text)
        self.assertIn("dimension_reviews[0].rating", issue_text)

    def test_render_result_review_markdown_includes_dimension_reviews(self) -> None:
        markdown = render_result_review_markdown(
            {
                "overall_result_credibility": "medium",
                "overall_alignment": "partial_match",
                "experiment_reviews": [
                    {
                        "task_id": "reproduce_fig_1",
                        "local_result_credibility": "medium",
                        "paper_alignment": "partial_match",
                        "scientific_verdict": "partially_supports_paper_claim",
                        "dimension_reviews": scientific_dimension_reviews(),
                        "paper_result_summary": "论文显示 BER 随 SNR 下降。",
                        "local_result_summary": "本地 CSV 显示 BER 下降。",
                        "differences": ["只有 smoke-scale 数据。"],
                        "possible_causes": ["样本量较少。"],
                        "evidence": ["outputs/results.csv"],
                        "limitations": ["未精确读图。"],
                        "confidence": "medium",
                    }
                ],
                "cross_experiment_findings": ["趋势基本存在。"],
                "recommended_human_checks": ["运行完整配置。"],
                "note": "仅评估复现风险。",
            },
            evidence={"output_images": [], "paper_images": []},
        )

        self.assertIn("**多维审查**", markdown)
        self.assertIn("复现逻辑", markdown)
        self.assertIn("趋势走向", markdown)
        self.assertIn("partially_supports_paper_claim", markdown)

    def test_result_review_retry_prompt_keeps_chinese_language_contract(self) -> None:
        prompt = build_result_json_retry_prompt(
            "原始任务：输出中文审查。",
            "{bad",
            "JSON parse error",
            schema_label="result_review_experiment",
        )

        self.assertIn("must be written in Chinese", prompt)
        self.assertIn("dimension findings", prompt)
        self.assertIn("differences, causes, evidence, limitations, and notes in Chinese", prompt)
        self.assertIn("all seven dimension_reviews", prompt)


class LLMClientMultimodalTests(unittest.TestCase):
    def test_complete_multimodal_sends_image_url_parts_without_fallback(self) -> None:
        class CaptureClient(OpenAICompatibleClient):
            def __init__(self) -> None:
                super().__init__(api_key="key", base_url="https://example.test", model="model")
                self.payload = {}
                self.allow_fallback = True

            def _post_chat_completion(self, payload: dict, *, allow_response_format_fallback: bool = True) -> str:
                self.payload = payload
                self.allow_fallback = allow_response_format_fallback
                return json.dumps({"choices": [{"message": {"content": "{}"}}]})

        client = CaptureClient()

        client.complete_multimodal(
            "review",
            images=[LLMImage(label="plot", mime_type="image/png", data_b64=PNG_B64)],
            response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        )

        content = client.payload["messages"][0]["content"]
        self.assertFalse(client.allow_fallback)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[2]["type"], "image_url")
        self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
