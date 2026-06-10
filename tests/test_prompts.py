import unittest

from geng_agent.prompts import PromptBook
from geng_agent.security import dependency_policy_prompt_text


class PromptTests(unittest.TestCase):
    def test_dependency_policy_describes_import_requirements_contract(self) -> None:
        policy = dependency_policy_prompt_text()

        self.assertIn("第三方库", policy)
        self.assertIn("requirements.txt", policy)
        self.assertIn("当前环境已安装且允许使用", policy)
        self.assertIn("numpy", policy)
        # steer codegen to prefer whitelisted comms libraries over hand-rolling primitives
        self.assertIn("commpy", policy)
        self.assertIn("优先调", policy)

    def test_project_and_repair_prompts_include_dependency_policy(self) -> None:
        book = PromptBook()

        plan_prompt = book.render(
            "generate_repro_project_plan.md",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
        )
        file_prompt = book.render(
            "generate_repro_project_file.md",
            target_path="run_experiment.py",
            project_plan_json="{}",
            generated_files_context_json="[]",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
            review_feedback_json="",
        )
        repair_prompt = book.render(
            "repair_repro_project.md",
            command="[]",
            returncode="1",
            stdout="",
            stderr="",
            validation_json="{}",
            code_context="",
        )

        for prompt in (plan_prompt, file_prompt, repair_prompt):
            self.assertIn("第三方库只能从", prompt)
            self.assertIn("只要 Python 代码里出现第三方 import", prompt)
            self.assertIn("requirements.txt 里写对应包名", prompt)

    def test_generation_prompts_require_random_seed(self) -> None:
        book = PromptBook()

        plan_prompt = book.render(
            "generate_repro_project_plan.md",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
        )
        file_prompt = book.render(
            "generate_repro_project_file.md",
            target_path="run_experiment.py",
            project_plan_json="{}",
            generated_files_context_json="[]",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
            review_feedback_json="",
        )

        for prompt in (plan_prompt, file_prompt):
            self.assertIn("随机种子", prompt)
        self.assertIn("seed", file_prompt)
        self.assertIn("summary.json", file_prompt)

    def test_generation_and_repair_prompts_guard_serialization_and_false_success(self) -> None:
        book = PromptBook()

        file_prompt = book.render(
            "generate_repro_project_file.md",
            target_path="run_experiment.py",
            project_plan_json="{}",
            generated_files_context_json="[]",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
            review_feedback_json="",
        )
        repair_prompt = book.render(
            "repair_repro_project.md",
            command="[]",
            returncode="1",
            stdout="",
            stderr="",
            validation_json="{}",
            code_context="",
        )

        # Serialization / NaN-Inf scrubbing / write-then-verify are now delegated to the
        # trusted src/_io runtime (deterministic p≈1), not re-derived per generated file:
        # assert the prompt steers codegen to CALL it rather than hand-roll the plumbing.
        self.assertIn("_io.begin", file_prompt)
        self.assertIn("_io.write_table", file_prompt)
        self.assertIn("_io.finish", file_prompt)
        self.assertIn("受信任", file_prompt)
        self.assertIn("json.load", file_prompt)  # described as _io's self-check
        self.assertIn("Inf", file_prompt)
        # science-side numeric correctness the model must still get right before calling _io
        self.assertIn("np.real", file_prompt)
        self.assertIn("length-1", file_prompt)
        # honest-failure discipline preserved: summary written unconditionally, no faked success
        self.assertIn("无条件", file_prompt)
        self.assertIn("粉饰", file_prompt)
        # the runtime-repair prompt carries the same discipline
        self.assertIn("np.real", repair_prompt)
        self.assertIn("谎报", repair_prompt)
        self.assertIn("无条件", repair_prompt)

    def test_generation_prompt_guards_snr_convention_and_zeroing_guards(self) -> None:
        # The 2603 LEO paper produced an all-zero figure because the generated code used a raw
        # absolute link budget (Friis x thermal noise -> -7 dB) AND an absolute-threshold guard
        # (1/trace(inv(G)) < 1e-12 -> return zeros) that hard-zeroed the SINR. The codegen prompt
        # must steer away from BOTH, and flag all-zero output as a failure signal.
        file_prompt = PromptBook().render(
            "generate_repro_project_file.md",
            target_path="src/metrics.py",
            project_plan_json="{}",
            generated_files_context_json="[]",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
            review_feedback_json="",
        )
        self.assertIn("SNR 约定", file_prompt)      # use the paper's normalized SNR convention
        self.assertIn("归一化", file_prompt)
        self.assertIn("链路预算", file_prompt)        # not a raw absolute link budget
        self.assertIn("条件数", file_prompt)          # relative guard, not absolute-threshold zeroing
        self.assertIn("全 0", file_prompt)            # all-zero is a failure signal

    def test_result_review_prompts_require_chinese_report_text(self) -> None:
        book = PromptBook()

        overview_prompt = book.render(
            "review_reproduction_results.md",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            paper_context_json="[]",
            result_evidence_json="{}",
        )
        experiment_prompt = book.render(
            "review_reproduction_experiment.md",
            task_json="{}",
            engineering_facts_json="{}",
            paper_context_json="[]",
            result_evidence_json="{}",
        )

        for prompt in (overview_prompt, experiment_prompt):
            self.assertIn("所有自然语言字段必须使用中文", prompt)
            self.assertIn("最终 Markdown 和 Word 报告", prompt)
            self.assertIn("不要输出英文说明句", prompt)
            self.assertIn("复现逻辑", prompt)
            self.assertIn("趋势走向", prompt)
            self.assertIn("baseline 排序", prompt)
            self.assertIn("结论支持度", prompt)
            self.assertIn("dimension_reviews", prompt)


if __name__ == "__main__":
    unittest.main()
