import unittest

from geng_agent.prompts import PromptBook
from geng_agent.security import dependency_policy_prompt_text


class PromptTests(unittest.TestCase):
    def test_dependency_policy_describes_task_writer_requirements_contract(self) -> None:
        policy = dependency_policy_prompt_text()

        self.assertIn("第三方库", policy)
        self.assertIn("requirements.txt", policy)
        self.assertIn("不是包白名单", policy)
        self.assertIn("environment request", policy)
        self.assertIn("可信来源", policy)
        self.assertIn("numpy", policy)
        self.assertIn("torch", policy)
        self.assertIn("commpy", policy)
        self.assertIn("优先调", policy)
        self.assertIn("Never silently replace", policy)
        self.assertNotIn("清单外的库不要 import", policy)

    def test_active_analysis_prompts_render(self) -> None:
        book = PromptBook()
        architecture = "## Contract rules" + book.load("design_scientific_architecture.md").split("## Contract rules", 1)[1].split("## Host capability inventory", 1)[0]
        rendered = {
            "understanding": book.render("understand_paper.md", paper_context="original paper evidence"),
            "planning": book.render("plan_experiments.md", architecture_rules=architecture,
                paper_context="original paper evidence", understanding="{}", revision_context="{}", host_capabilities="{}"),
            "backfill": book.render("targeted_fact_backfill.md", round_index="1", existing_facts_json="{}",
                targeted_requests_json="[]", current_tasks_json="{}", search_ledger_json="{}", paper_context_json="[]"),
        }
        for name, prompt in rendered.items():
            self.assertTrue(prompt.strip(), name)
            self.assertNotIn("{{", prompt, name)
        self.assertIn("engineering_facts", rendered["understanding"])
        self.assertIn("central scientific claims", rendered["understanding"])
        self.assertIn("never label that correction as literal paper text", rendered["understanding"])
        self.assertIn("missing_fact_requests", rendered["planning"])
        self.assertIn("backfill_handoff", rendered["planning"])
        self.assertIn("scientific_acceptance", rendered["planning"])
        self.assertIn("schedulable dependencies", rendered["planning"])
        self.assertIn("multiple architecture bindings", rendered["planning"])
        self.assertIn("environment gap, never permission to replace the scientific algorithm", rendered["planning"])
        self.assertNotIn("exactly one host experiment entry per task", rendered["planning"])
        self.assertIn("request_resolutions", rendered["backfill"])
        self.assertIn("定向", rendered["backfill"])


if __name__ == "__main__":
    unittest.main()
