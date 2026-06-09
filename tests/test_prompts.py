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

    def test_code_review_prompts_carry_whitelist_awareness(self) -> None:
        book = PromptBook()

        review_prompt = book.render(
            "review_repro_project_code.md",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            project_files="[]",
            paper_context_json="[]",
        )
        revise_prompt = book.render(
            "repair_repro_project_for_review.md",
            review_findings_json="[]",
            engineering_facts_json="{}",
            repro_tasks_json="{}",
            project_files="[]",
        )

        for prompt in (review_prompt, revise_prompt):
            self.assertIn("第三方库只能从", prompt)  # dependency policy is injected
            self.assertIn("白名单", prompt)
        # the revise step is where the reviewer LLM actually adds code/libraries, so it must
        # be explicit that any new third-party import has to be declared in requirements.txt.
        self.assertIn("requirements.txt", revise_prompt)

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

        # complex -> real, plain-type/JSON-safe serialization, write-then-verify, no false success
        self.assertIn("np.real", file_prompt)
        self.assertIn(".tolist()", file_prompt)
        self.assertIn("Inf", file_prompt)
        self.assertIn("json.load", file_prompt)
        self.assertIn("谎报", file_prompt)
        # rerun2 fixes: forbid float(array), and summary.json must be written unconditionally
        self.assertIn("length-1", file_prompt)
        self.assertIn("无条件", file_prompt)
        # the runtime-repair prompt carries the same discipline
        self.assertIn("np.real", repair_prompt)
        self.assertIn("谎报", repair_prompt)
        self.assertIn("无条件", repair_prompt)

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
