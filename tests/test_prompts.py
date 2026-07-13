import unittest

from geng_agent.prompts import PromptBook
from geng_agent.security import dependency_policy_prompt_text


class PromptTests(unittest.TestCase):
    def test_dependency_policy_describes_task_writer_requirements_contract(self) -> None:
        policy = dependency_policy_prompt_text()

        self.assertIn("第三方库", policy)
        self.assertIn("requirements.txt", policy)
        self.assertIn("当前环境已安装且允许使用", policy)
        self.assertIn("numpy", policy)
        self.assertIn("torch", policy)
        self.assertIn("commpy", policy)
        self.assertIn("优先调", policy)

    def test_active_analysis_prompts_render(self) -> None:
        book = PromptBook()
        rendered = {
            "facts": book.render("extract_engineering_facts.md", paper_chunks_json="[]"),
            "backfill": book.render(
                "targeted_fact_backfill.md",
                existing_facts_json="{}",
                targeted_requests_json="[]",
                preliminary_tasks_json="{}",
                paper_context_json="[]",
            ),
            "thesis": book.render(
                "extract_paper_thesis.md",
                engineering_facts_json="{}",
                paper_chunks_json="[]",
            ),
            "tasks": book.render(
                "build_repro_tasks.md",
                engineering_facts_json="{}",
                fact_coverage_json="{}",
                paper_context_json="[]",
            ),
            "finalize_tasks": book.render(
                "finalize_repro_tasks.md",
                preliminary_tasks_json="{}",
                final_engineering_facts_json="{}",
                backfill_resolution_json="{}",
            ),
        }

        for name, prompt in rendered.items():
            self.assertTrue(prompt.strip(), name)
            self.assertNotIn("{{", prompt, name)

        self.assertIn("JSON", rendered["facts"])
        self.assertIn("机制", rendered["thesis"])
        self.assertIn("required_facts", rendered["tasks"])
        self.assertIn("missing_fact_requests", rendered["tasks"])
        self.assertIn("定向", rendered["backfill"])


if __name__ == "__main__":
    unittest.main()
