import unittest

from geng_agent.schemas import validate_stage
from geng_agent.task_contract import build_task_contract_draft, contract_hash


class TaskContractTests(unittest.TestCase):
    def test_draft_is_valid_and_preserves_memory_snapshot(self) -> None:
        task = {
            "task_id": "reproduce_fig_9a",
            "experiment_id": "exp_fig_9a",
            "target": "BER curve",
            "metric_formula": "BER=errors/bits",
            "reproducibility_mode": "scaled_full",
            "acceptance_criteria": ["ordering matches"],
            "required_facts": [{"type": "metric", "name": "BER"}],
        }
        contract = build_task_contract_draft(task, memory_snapshot_hash="snapshot")
        self.assertEqual(validate_stage("task_contract", contract), [])
        self.assertEqual(contract["memory_snapshot_hash"], "snapshot")
        self.assertEqual(contract["reproducibility_mode"], "scaled_full")
        self.assertEqual(contract["resources"]["execution_class"], "unknown")
        self.assertGreaterEqual(contract["resources"]["cpu_cores"], 1)

    def test_hash_is_order_independent_but_content_sensitive(self) -> None:
        first = build_task_contract_draft({"task_id": "t", "target": "x"}, memory_snapshot_hash="m")
        second = dict(reversed(list(first.items())))
        self.assertEqual(contract_hash(first), contract_hash(second))
        second["seed"] = 2
        self.assertNotEqual(contract_hash(first), contract_hash(second))


if __name__ == "__main__":
    unittest.main()
