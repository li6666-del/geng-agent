import hashlib

import pytest

from geng_agent.task_reporter_snapshot import (
    _copy_writer_source_snapshot,
    _looks_like_text_source,
)


@pytest.mark.parametrize("character", ["é", "信", "📡"])
def test_source_snapshot_keeps_utf8_character_split_by_sample(tmp_path, character):
    sandbox = tmp_path / "writer"
    sandbox.mkdir()
    source = sandbox / "实验汇总.py"
    # The first byte of this character is byte 8191 (zero-based). The
    # 8192-byte sample therefore ends inside each multibyte character.
    content = b"#" + b"x" * 8190 + character.encode("utf-8") + b"\nVALUE = 1\n"
    source.write_bytes(content)
    target = tmp_path / "reporter_source"

    copied, warnings = _copy_writer_source_snapshot(
        source_sandbox=sandbox, target_root=target
    )

    assert warnings == []
    assert [item["sandbox_relative_path"] for item in copied] == ["实验汇总.py"]
    assert copied[0]["declared_by_manifest"] is False
    assert copied[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert (target / "实验汇总.py").read_bytes() == content


@pytest.mark.parametrize(
    "content",
    [b"VALUE = 1\n", "# 信道与算法\nVALUE = 1\n".encode("utf-8"),
     b"\xef\xbb\xbf" + "# 信道\n".encode("utf-8")],
)
def test_text_source_still_accepts_normal_utf8(tmp_path, content):
    source = tmp_path / "source.py"
    source.write_bytes(content)
    assert _looks_like_text_source(source) is True


@pytest.mark.parametrize(
    "content",
    [b"VALUE = \x00\n", b"#\xff invalid\n",
     b"#\xe4\xff invalid\n" + b"x" * 9000,
     b"#\xe0\x80 invalid\n" + b"x" * 9000],
)
def test_text_source_still_rejects_binary_and_invalid_utf8(tmp_path, content):
    source = tmp_path / "source.py"
    source.write_bytes(content)
    assert _looks_like_text_source(source) is False


@pytest.mark.parametrize("size", [5, 8192])
def test_text_source_rejects_incomplete_utf8_at_actual_file_end(tmp_path, size):
    source = tmp_path / "source.py"
    source.write_bytes(b"#" + b"x" * (size - 2) + b"\xe4")
    assert _looks_like_text_source(source) is False


@pytest.mark.parametrize("size", [0, 10_000_001])
def test_text_source_resource_bounds_are_unchanged(tmp_path, size):
    source = tmp_path / "source.py"
    source.write_bytes(b"x" * size)
    assert _looks_like_text_source(source) is False
