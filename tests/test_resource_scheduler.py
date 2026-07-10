import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from geng_agent.resource_runtime import ResourceBroker, acquire_resource_lease, run_guarded_process, subprocess_environment
from geng_agent.resource_scheduler import WriterConcurrencyController, build_resource_plan


def _hardware(*, total_ram: float = 16.0, available_ram: float = 12.0, gpu_count: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "cpu": {"physical_cores": 16, "logical_processors": 32},
        "memory": {"total_gb": total_ram, "available_gb": available_ram},
        "gpus": [
            {
                "index": index,
                "name": f"GPU {index}",
                "total_vram_gb": 8.0,
                "available_vram_gb": 7.0,
            }
            for index in range(gpu_count)
        ],
        "torch": {"available": True, "cuda_available": gpu_count > 0},
    }


def _cpu_contract() -> dict:
    return {
        "backend": {"requested": "cpu", "allow_cpu_fallback": True},
        "resources": {
            "execution_class": "cpu_heavy",
            "cpu_cores": 4,
            "ram_gb": 3.0,
            "gpu_count": 0,
            "vram_gb": 0.0,
            "confidence": "high",
        },
    }


class ResourcePlanTests(unittest.TestCase):
    def test_plan_starts_two_writers_and_keeps_full_resources_separate(self) -> None:
        plan = build_resource_plan(task_count=7, hardware=_hardware())
        self.assertEqual(plan["writer"]["initial_concurrency"], 2)
        self.assertEqual(plan["writer"]["max_concurrency"], 4)
        self.assertEqual(plan["execution"]["gpus"][0]["max_full_jobs"], 1)
        self.assertGreaterEqual(plan["execution"]["ram_budget_gb"], 3.0)

    def test_explicit_writer_limit_is_respected_without_gpu_capping(self) -> None:
        plan = build_resource_plan(task_count=7, requested_writer_concurrency=3, hardware=_hardware())
        self.assertEqual(plan["writer"]["initial_concurrency"], 3)
        self.assertEqual(plan["writer"]["max_concurrency"], 3)

    def test_controller_increases_after_stability_and_halves_on_capacity(self) -> None:
        controller = WriterConcurrencyController(
            {
                "minimum_concurrency": 1,
                "initial_concurrency": 2,
                "max_concurrency": 4,
                "successes_before_increase": 3,
            }
        )
        controller.record_success()
        controller.record_success()
        self.assertEqual(controller.current, 2)
        controller.record_success()
        self.assertEqual(controller.current, 3)
        controller.record_capacity_error()
        self.assertEqual(controller.current, 2)


class ResourceLeaseTests(unittest.TestCase):
    def test_task_child_environment_does_not_receive_broker_credentials(self) -> None:
        allocation = {"cpu_cores": 1, "gpu_indices": []}
        with patch.dict(
            __import__("os").environ,
            {
                "GENG_TASK_WRITER_BROKER_TOKEN": "secret",
                "GENG_TASK_WRITER_BROKER_CHANNEL": "outside",
                "GENG_TASK_CONTRACT_PATH": "contract.json",
            },
        ):
            env = subprocess_environment(allocation, real_python=__import__("sys").executable)
        self.assertNotIn("GENG_TASK_WRITER_BROKER_TOKEN", env)
        self.assertNotIn("GENG_TASK_WRITER_BROKER_CHANNEL", env)
        self.assertNotIn("GENG_TASK_CONTRACT_PATH", env)

    def test_shared_budget_serializes_conflicting_full_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = build_resource_plan(task_count=2, hardware=_hardware(gpu_count=0))
            plan["execution"]["cpu_cores_budget"] = 4
            plan["execution"]["ram_budget_gb"] = 4.0
            plan["execution"]["resource_poll_seconds"] = 0.05
            events = root / "events.jsonl"
            state_path = root / "host_only" / "state.json"
            broker = ResourceBroker(plan=plan, events_path=events, state_path=state_path)
            with broker:
                first_channel = broker.register_channel(task_id="first", channel_dir=root / "first" / ".broker")
                second_channel = broker.register_channel(task_id="second", channel_dir=root / "second" / ".broker")
                first, _, _ = acquire_resource_lease(
                    channel_dir=Path(first_channel["channel_dir"]),
                    channel_token=first_channel["token"],
                    contract=_cpu_contract(),
                    task_id="first",
                    wait_timeout_s=2.0,
                )
                acquired = threading.Event()
                holder = {}

                def acquire_second() -> None:
                    lease, _, _ = acquire_resource_lease(
                        channel_dir=Path(second_channel["channel_dir"]),
                        channel_token=second_channel["token"],
                        contract=_cpu_contract(),
                        task_id="second",
                        wait_timeout_s=2.0,
                    )
                    holder["lease"] = lease
                    acquired.set()

                thread = threading.Thread(target=acquire_second)
                thread.start()
                self.assertFalse(acquired.wait(0.2))
                first.release()
                self.assertTrue(acquired.wait(2.0))
                holder["lease"].release()
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
            self.assertTrue(state_path.exists())
            self.assertFalse((root / "first" / ".broker" / "resource_state.json").exists())

    def test_guarded_process_terminates_ram_overuse(self) -> None:
        allocation = {
            "cpu_cores": 1,
            "ram_gb": 0.25,
            "gpu_indices": [],
            "vram_gb": 0.0,
            "monitor_poll_seconds": 0.05,
        }
        script = "import time; payload=bytearray(320 * 1024 * 1024); time.sleep(2)"
        result = run_guarded_process(
            command=[__import__("sys").executable, "-c", script],
            env=subprocess_environment(allocation, real_python=__import__("sys").executable),
            timeout=10.0,
            allocation=allocation,
        )
        self.assertEqual(result["returncode"], 125)
        self.assertIn("RAM limit exceeded", result["resource_violation"])


if __name__ == "__main__":
    unittest.main()
