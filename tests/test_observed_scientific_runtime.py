"""Exercise a real scientific runtime and persisted state through the host boundary."""
import importlib.util
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from geng_agent.execution_receipts import ExecutionBroker, validate_receipt
from geng_agent.outputs import write_json


@pytest.fixture
def scientific_workspace():
    # Ordinary case directories inherit Windows ACLs. tempfile's mode 0700
    # cannot be opened by the restricted token used by the real OS sandbox.
    root = Path(tempfile.gettempdir()) / ("geng-scientific-test-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="optional PyTorch scientific runtime is not installed")
def test_real_training_shared_checkpoint_and_stale_producer_rejection(scientific_workspace):
    tmp_path = scientific_workspace
    project, audit = tmp_path / "project", tmp_path / "audit"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks" / "__init__.py").write_text("", encoding="utf-8")
    write_json(project / "config.json", {"seed": 13})
    entries = [{"task_id": task, "module": task, "output_subdir": task,
                "config_full": "config.json"} for task in ("train", "evaluate")]
    write_json(project / "tasks_manifest.json", {"tasks": entries})
    train_source = project / "tasks" / "train.py"
    train_source.write_text('''import json
from pathlib import Path
import torch
def main(config):
    torch.manual_seed(json.loads(Path(config).read_text())["seed"])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = torch.nn.Linear(2, 2).to(device)
    before = model.weight.detach().clone()
    x = torch.randn(64, 2, device=device)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.08)
    for _ in range(16):
        optimizer.zero_grad()
        loss = (model(x) - x).square().mean()
        loss.backward()
        optimizer.step()
    assert not torch.equal(before, model.weight)
    checkpoint = Path("execution_units/shared/model.pt")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.cpu().state_dict(), checkpoint)
    Path("outputs/train/results.csv").write_text("loss\\n" + str(loss.item()) + "\\n")
    Path("outputs/train/summary.json").write_text(json.dumps({"device":device,"trained":True}))
''', encoding="utf-8")
    (project / "tasks" / "evaluate.py").write_text('''from pathlib import Path
import torch
def main(config):
    model = torch.nn.Linear(2, 2)
    model.load_state_dict(torch.load("execution_units/shared/model.pt", weights_only=True))
    torch.manual_seed(31)
    x = torch.randn(64, 2)
    error = (model(x) - x).square().mean().item()
    Path("outputs/evaluate/results.csv").write_text("nmse\\n" + str(error) + "\\n")
''', encoding="utf-8")
    broker = ExecutionBroker(project, audit, Path(sys.executable))
    training = broker.execute({"task_id": "train", "mode": "full"})
    assert training["returncode"] == 0, training.get("stderr_tail")
    assert validate_receipt(project, training, task_id="train")["passed"]
    assert "execution_units/shared/model.pt" in training["produced_artifacts"]
    evaluation = broker.execute({"task_id": "evaluate", "mode": "full",
        "inputs": ["execution_units/shared/model.pt"]})
    assert evaluation["returncode"] == 0, evaluation.get("stderr_tail")
    assert validate_receipt(project, evaluation, task_id="evaluate")["passed"]
    assert evaluation["input_hashes"]["execution_units/shared/model.pt"] == training["produced_artifacts"]["execution_units/shared/model.pt"]
    train_source.write_text(train_source.read_text(encoding="utf-8").replace("lr=0.08", "lr=0.02"), encoding="utf-8")
    with pytest.raises(ValueError, match="no current producer receipt"):
        broker.execute({"task_id": "evaluate", "mode": "full", "inputs": ["execution_units/shared/model.pt"]})
