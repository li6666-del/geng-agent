"""Offline authentication transfer tests; all credentials are dummy fixtures."""
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from geng_agent import writer_auth


class FakeDomain:
    def __init__(self, failure=None):
        self.granted = []
        self.failure = failure

    def allow_write(self, path):
        self.granted.append(Path(path))
        if self.failure:
            raise self.failure


@pytest.fixture
def homes(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "auth.json").write_text('{"dummy_auth":"not-a-real-credential"}', encoding="utf-8")
    (original / "config.toml").write_text('model_provider = "dummy"\n', encoding="utf-8")
    (original / ".sandbox-secrets").mkdir()
    (original / ".sandbox-secrets" / "never-copy.txt").write_text("dummy-only", encoding="utf-8")
    (original / "personal-mcp.json").write_text("{}", encoding="utf-8")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    return original, temporary


def config(**kwargs):
    return SimpleNamespace(**({"managed": True, "provider": "deepseek", "base_url": "https://dummy.invalid",
                              "env_key": "DUMMY_API_KEY"} | kwargs))


@pytest.mark.parametrize("model,credential,expected", [
    (config(), None, set()),
    (config(provider="openai", base_url=None, env_key=None), "dummy-api-key", set()),
    (config(provider="openai", base_url=None, env_key=None), None, {"auth.json"}),
    (config(managed=False), None, {"auth.json", "config.toml"}),
])
def test_only_selected_login_files_are_transferred(homes, model, credential, expected):
    original, temporary = homes
    env = {"CODEX_HOME": str(original), "TEMP": str(temporary)}
    if credential:
        env["OPENAI_API_KEY"] = credential
    before = env.copy()
    domain = FakeDomain()
    with writer_auth.writer_auth_home(domain, model, env) as child:
        home = Path(child["CODEX_HOME"])
        assert home.parent == temporary
        assert home != original
        assert {p.name for p in home.iterdir()} == expected
        assert domain.granted == [home]
        for name in expected:
            assert (home / name).read_bytes() == (original / name).read_bytes()
    assert not home.exists()
    assert env == before
    assert (original / "auth.json").is_file()
    assert (original / ".sandbox-secrets" / "never-copy.txt").is_file()


def test_original_real_home_is_selected_before_domain_child_overrides(monkeypatch, homes):
    original, temporary = homes
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: original.parent))
    original.rename(original.parent / ".codex")
    with writer_auth.writer_auth_home(FakeDomain(), config(managed=False), {"TMP": str(temporary)}) as child:
        assert (Path(child["CODEX_HOME"]) / "auth.json").is_file()


def test_cleanup_runs_after_grant_failure_and_invocation_failure(homes):
    original, temporary = homes
    env = {"CODEX_HOME": str(original), "TEMP": str(temporary)}
    failed = FakeDomain(RuntimeError("grant refused"))
    with pytest.raises(RuntimeError, match="grant refused"):
        with writer_auth.writer_auth_home(failed, config(managed=False), env):
            pytest.fail("a failed grant must not yield")
    assert not failed.granted[0].exists()
    with pytest.raises(ValueError, match="dummy invocation"):
        with writer_auth.writer_auth_home(FakeDomain(), config(managed=False), env) as child:
            home = Path(child["CODEX_HOME"])
            raise ValueError("dummy invocation")
    assert not home.exists()


def test_case_insensitive_environment_has_one_codex_home(homes):
    original, temporary = homes
    with writer_auth.writer_auth_home(FakeDomain(), config(managed=False),
                                     {"codex_home": str(original), "Temp": str(temporary)}) as child:
        assert "codex_home" not in child
        assert (Path(child["CODEX_HOME"]) / "auth.json").is_file()


def test_api_mode_never_reads_personal_authentication(homes, monkeypatch):
    original, temporary = homes
    monkeypatch.setattr(writer_auth.shutil, "copyfile", lambda *_: pytest.fail("API mode copied a login file"))
    with writer_auth.writer_auth_home(FakeDomain(), config(),
                                     {"CODEX_HOME": str(original), "TEMP": str(temporary)}):
        pass


def test_cleanup_refuses_an_unrelated_directory(homes):
    original, temporary = homes
    with pytest.raises(RuntimeError, match="unexpected"):
        writer_auth._remove_private_home(original, temporary)
    assert (original / "auth.json").is_file()


def test_cleanup_removes_a_reparse_root_without_traversing_it(homes, monkeypatch):
    original, temporary = homes
    home = temporary / "geng-writer-auth-dummy"
    home.mkdir()
    original_lstat = Path.lstat
    def simulated_reparse(path):
        if path == home:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(path)
    monkeypatch.setattr(Path, "lstat", simulated_reparse)
    monkeypatch.setattr(writer_auth.shutil, "rmtree", lambda *_: pytest.fail("must not traverse a reparse root"))
    writer_auth._remove_private_home(home, temporary)
    assert not home.exists()
    assert (original / "auth.json").is_file()
