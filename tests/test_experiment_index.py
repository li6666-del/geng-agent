import unittest

from geng_agent.experiment_index import build_local_experiment_index


class ExperimentIndexTests(unittest.TestCase):
    def test_builds_experiment_index_from_mock_documents(self) -> None:
        facts = {
            "engineering_facts": [
                {
                    "type": "channel_model",
                    "name": "AWGN",
                    "value": {"snr_db": [0, 2, 4]},
                    "source": {"chunk_id": "c1", "page": 3, "section": "Simulation", "quote": "AWGN channel"},
                    "confidence": "high",
                    "used_for_reproduction": True,
                },
                {
                    "type": "metric",
                    "name": "bit_error_rate",
                    "value": {"formula": "bit_errors / total_bits"},
                    "source": {"chunk_id": "c2", "page": 4, "section": "Results", "quote": "BER results"},
                    "confidence": "high",
                    "used_for_reproduction": True,
                },
            ]
        }
        tasks = {
            "repro_tasks": [
                {
                    "task_id": "reproduce_fig_2",
                    "target": "BER vs SNR under AWGN",
                    "metric": "bit_error_rate",
                    "figure_or_claim": "Fig. 2",
                    "required_facts": [
                        {"type": "channel_model", "name": "AWGN"},
                        {"type": "metric", "name": "bit_error_rate"},
                    ],
                }
            ]
        }
        paper = {
            "chunks": [
                {"chunk_id": "c1", "page": 3, "section": "Simulation", "text": "The AWGN channel is used."},
                {"chunk_id": "c3", "page": 5, "section": "Results", "text": "Fig. 2 shows BER versus SNR."},
            ]
        }

        index = build_local_experiment_index(facts, tasks, paper)

        self.assertEqual(len(index["experiments"]), 1)
        experiment = index["experiments"][0]
        self.assertEqual(experiment["experiment_id"], "exp_reproduce_fig_2")
        self.assertEqual(experiment["title"], "BER vs SNR under AWGN")
        self.assertEqual(experiment["figure_or_table"], "Fig. 2")
        self.assertEqual(experiment["task_id"], "reproduce_fig_2")
        self.assertEqual(experiment["metric"], "bit_error_rate")
        self.assertEqual(experiment["required_facts"][0], {"type": "channel_model", "name": "AWGN"})
        self.assertNotIn("status", experiment)
        self.assertNotIn("reproducibility_mode", experiment)
        self.assertEqual(experiment["limitations"], [])

    def test_source_pages_and_chunk_ids_link_back_to_facts_and_paper_chunks(self) -> None:
        index = build_local_experiment_index(
            {
                "engineering_facts": [
                    {
                        "type": "baseline",
                        "name": "uncoded_bpsk",
                        "value": {},
                        "source": {"chunk_id": "fact_chunk", "page": 7, "section": "Baseline", "quote": "baseline"},
                    }
                ]
            },
            {
                "repro_tasks": [
                    {
                        "task_id": "task_a",
                        "target": "Baseline throughput",
                        "metric": "throughput",
                        "figure_or_claim": "Table 1",
                        "required_facts": [{"type": "baseline", "name": "uncoded_bpsk"}],
                    }
                ]
            },
            {"paper_chunks": [{"chunk_id": "paper_chunk", "page": 8, "text": "Table 1 reports throughput."}]},
        )

        experiment = index["experiments"][0]
        self.assertEqual(experiment["source_pages"], [7, 8])
        self.assertEqual(experiment["source_chunk_ids"], ["fact_chunk", "paper_chunk"])

    def test_missing_information_is_recorded_in_limitations(self) -> None:
        index = build_local_experiment_index(
            {"engineering_facts": []},
            {
                "repro_tasks": [
                    {
                        "task_id": "thin_task",
                        "metric": "",
                        "figure_or_claim": "",
                        "required_facts": [{"type": "channel_model", "name": "Rayleigh"}],
                    }
                ]
            },
            {"chunks": []},
        )

        experiment = index["experiments"][0]
        self.assertNotIn("status", experiment)
        self.assertTrue(any("Missing figure_or_claim" in item for item in experiment["limitations"]))
        self.assertTrue(any("Missing metric" in item for item in experiment["limitations"]))
        self.assertTrue(any("Required fact not found" in item for item in experiment["limitations"]))
        self.assertTrue(any("No source_pages" in item for item in experiment["limitations"]))
        self.assertTrue(any("No source_chunk_ids" in item for item in experiment["limitations"]))


if __name__ == "__main__":
    unittest.main()
