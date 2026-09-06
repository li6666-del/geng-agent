from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from geng_agent.case_environment import (
    CommandResult,
    EnvironmentPolicyError,
    RequirementRequest,
    TrustedIndex,
    _environment_hash,
    _run,
    _unprivileged_executable_path,
    build_environment_manifest,
    build_pip_install_argv,
    locked_distributions,
    normalize_requirement,
    resolve_case_environment,
    subprocess_argv_runner,
    validate_pip_report,
)


_PROBE_PREFIX = "GENG_CASE_ENVIRONMENT_JSON:"
_FIXED_TIME = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _result(argv: object, payload: dict[str, object], returncode: int = 0) -> CommandResult:
    normalized = (str(argv),) if isinstance(argv, str) else tuple(str(part) for part in argv)
    return CommandResult(
        argv=normalized,
        returncode=returncode,
        stdout=_PROBE_PREFIX + json.dumps(payload, sort_keys=True),
    )


def _is_pip_action(argv: object, action: str) -> bool:
    normalized = (str(argv),) if isinstance(argv, str) else tuple(str(part) for part in argv)
    return (
        len(normalized) >= 5
        and normalized[1:5] == ("-I", "-m", "pip", action)
    ) or (
        len(normalized) >= 4
        and normalized[1:4] == ("-m", "pip", action)
    )


def _identity_payload(executable: str = "/case/python") -> dict[str, object]:
    return {
        "executable": executable,
        "python_full_version": "3.11.9",
        "python_version": "3.11",
        "implementation": "CPython",
        "marker_environment": {
            "implementation_name": "cpython",
            "implementation_version": "3.11.9",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_release": "test",
            "platform_system": "Linux",
            "platform_version": "test",
            "python_full_version": "3.11.9",
            "platform_python_implementation": "CPython",
            "python_version": "3.11",
            "sys_platform": "linux",
            "extra": "",
        },
    }


class CaseEnvironmentPolicyTests(unittest.TestCase):
    def test_normalizes_pep508_without_restricting_distribution_name(self) -> None:
        normalized = normalize_requirement(
            RequirementRequest(
                requirement=(
                    "Unlisted_Paper_Library[CUDA]>=2.0,!=2.1; "
                    "python_version < '4'"
                ),
                import_names=("paper_lib.cuda",),
                requested_by="foundation",
                reason="paper implementation requires its kernels",
            )
        )

        self.assertEqual(normalized.distribution, "unlisted-paper-library")
        self.assertEqual(normalized.extras, ("cuda",))
        self.assertEqual(normalized.import_names, ("paper_lib.cuda",))
        self.assertIn("!=2.1", normalized.specifier)
        self.assertIn(">=2.0", normalized.specifier)
        self.assertIn("python_version < \"4\"", normalized.requirement)
        self.assertEqual(normalized.requested_by, "foundation")

    def test_rejects_writer_controlled_urls_vcs_paths_archives_and_options(self) -> None:
        rejected = (
            "--index-url https://evil.invalid/simple",
            "-e ./local-project",
            "paper-lib @ https://evil.invalid/paper-lib.whl",
            "git+https://evil.invalid/repository.git",
            "./local-project",
            "/tmp/local-project",
            r"C:\local-project",
            "paper-lib.whl",
        )

        for requirement in rejected:
            with self.subTest(requirement=requirement):
                with self.assertRaises(EnvironmentPolicyError):
                    normalize_requirement(requirement)

    def test_trusted_index_is_selected_by_identity_and_rechecked_at_argv_boundary(self) -> None:
        trusted = TrustedIndex(
            identity="research",
            url="https://packages.example.test/simple/",
            artifact_hosts=("wheels.example.test",),
        )
        catalog = {"research": trusted}
        manifest = build_environment_manifest(
            case_id="case-index",
            target_interpreter="/case/python",
            requirements=("novel-library>=1",),
            index_identity="research",
            trusted_indexes=catalog,
        )

        argv = build_pip_install_argv(
            manifest,
            trusted_indexes=catalog,
            report_path="/case/pip-report.json",
            cache_dir="/case/pip-cache",
        )

        self.assertEqual(argv[0], "/case/python")
        self.assertEqual(argv[1:4], ("-I", "-m", "pip"))
        self.assertEqual(argv[argv.index("--index-url") + 1], "https://packages.example.test/simple")
        self.assertEqual(argv[-1], "novel-library>=1")
        self.assertNotIn("--no-cache-dir", argv)
        self.assertTrue(Path(argv[argv.index("--report") + 1]).is_absolute())
        self.assertTrue(Path(argv[argv.index("--cache-dir") + 1]).is_absolute())
        self.assertIn("--no-compile", argv)
        self.assertEqual(argv[argv.index("--only-binary") + 1], ":all:")
        with self.assertRaises(EnvironmentPolicyError):
            build_pip_install_argv(manifest)
        with self.assertRaises(EnvironmentPolicyError):
            build_pip_install_argv(manifest, trusted_indexes={})

        for field, value in (
            ("url", "https://evil.invalid/simple"),
            ("artifact_hosts", ["evil.invalid"]),
            ("fingerprint", "0" * 64),
        ):
            with self.subTest(field=field):
                tampered = deepcopy(manifest)
                tampered["index"][field] = value
                with self.assertRaises(EnvironmentPolicyError):
                    build_pip_install_argv(tampered, trusted_indexes=catalog)

    def test_completed_process_string_args_do_not_become_character_argv(self) -> None:
        expected = ("/case/python", "-m", "pip", "--version")

        def runner(argv, *, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                args="synthetic command string",
                returncode=0,
                stdout="ok",
                stderr="",
            )

        result = _run(runner, expected, cwd=None, timeout=1)

        self.assertEqual(result.argv, expected)

    def test_subprocess_runner_does_not_expose_host_credentials_to_probes(self) -> None:
        process = Mock(pid=1234, returncode=0)
        process.communicate.return_value = ("", "")
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "must-not-leak", "HTTPS_PROXY": "https://proxy.example"},
            clear=False,
        ), patch("geng_agent.case_environment.subprocess.Popen", return_value=process) as popen:
            subprocess_argv_runner(
                (sys.executable, "-I", "-c", "print('probe')"),
                timeout=1,
            )

        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["USER"], "geng-case-runtime")
        self.assertEqual(environment["LOGNAME"], "geng-case-runtime")
        self.assertEqual(environment["USERNAME"], "geng-case-runtime")
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["close_fds"])


    def test_isolated_pip_never_executes_case_local_pip_module(self) -> None:
        with TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir).resolve()
            sentinel = case_dir / "fake-pip-executed.txt"
            (case_dir / "pip.py").write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('fake-pip-executed.txt').write_text('executed')\n",
                encoding="utf-8",
            )

            result = subprocess_argv_runner(
                (sys.executable, "-I", "-m", "pip", "--version"),
                cwd=case_dir,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())

    @unittest.skipIf(os.name == "nt", "POSIX process groups are required")
    def test_subprocess_runner_kills_probe_process_group_on_timeout(self) -> None:
        process = Mock(pid=4321, returncode=-9)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(("/case/python",), 0.01),
            ("partial-output", "timed-out"),
        ]
        with patch(
            "geng_agent.case_environment.subprocess.Popen",
            return_value=process,
        ), patch("geng_agent.case_environment.os.killpg") as kill_group:
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess_argv_runner(
                    ("/case/python", "-I", "-c", "while True: pass"),
                    timeout=0.01,
                )

        kill_group.assert_called_once_with(4321, signal.SIGKILL)

    @unittest.skipIf(os.name == "nt", "POSIX traversal permissions are required")
    def test_unprivileged_launcher_check_follows_symlink_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            root.chmod(0o755)
            private = root / "private"
            private.mkdir(mode=0o700)
            target = private / "python"
            target.write_bytes(b"synthetic executable")
            target.chmod(0o755)
            launcher = root / "case-python"
            launcher.symlink_to(target)

            self.assertFalse(_unprivileged_executable_path(launcher))

    def test_pip_report_requires_https_trusted_sha256_artifact_evidence(self) -> None:
        trusted = TrustedIndex(
            identity="research",
            url="https://packages.example.test/simple/",
            artifact_hosts=("wheels.example.test",),
        )
        valid = {
            "install": [
                {
                    "metadata": {"name": "Paper_Lib", "version": "1.2.3"},
                    "download_info": {
                        "url": "https://wheels.example.test/files/paper_lib-1.2.3.whl",
                        "archive_info": {"hashes": {"sha256": "a" * 64}},
                    },
                }
            ]
        }
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pip-report.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            evidence = validate_pip_report(path, trusted)
            self.assertEqual(evidence["artifacts"][0]["distribution"], "paper-lib")
            self.assertEqual(evidence["artifacts"][0]["version"], "1.2.3")
            self.assertRegex(evidence["report_sha256"], r"^[0-9a-f]{64}$")

            invalid_reports = []
            missing_download = deepcopy(valid)
            del missing_download["install"][0]["download_info"]
            invalid_reports.append(missing_download)
            insecure_url = deepcopy(valid)
            insecure_url["install"][0]["download_info"]["url"] = (
                "http://wheels.example.test/paper.whl"
            )
            invalid_reports.append(insecure_url)
            untrusted_host = deepcopy(valid)
            untrusted_host["install"][0]["download_info"]["url"] = (
                "https://evil.example/paper.whl"
            )
            invalid_reports.append(untrusted_host)
            missing_hash = deepcopy(valid)
            missing_hash["install"][0]["download_info"]["archive_info"] = {"hashes": {}}
            invalid_reports.append(missing_hash)
            missing_metadata = deepcopy(valid)
            missing_metadata["install"][0]["metadata"] = {}
            invalid_reports.append(missing_metadata)

            for index, payload in enumerate(invalid_reports):
                with self.subTest(index=index):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(EnvironmentPolicyError):
                        validate_pip_report(path, trusted)


class CaseEnvironmentResolutionTests(unittest.TestCase):
    def test_dry_run_writes_plan_without_invoking_runner(self) -> None:
        def forbidden_runner(argv, *, cwd=None, timeout=None):
            raise AssertionError("dry-run must not invoke the command runner")

        with TemporaryDirectory() as temp_dir:
            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-dry",
                target_interpreter=sys.executable,
                requirements=("brand-new-library>=1",),
                dry_run=True,
                run_argv=forbidden_runner,
                now=lambda: _FIXED_TIME,
            )

            self.assertFalse(resolution.ready)
            self.assertEqual(resolution.report["status"], "planned")
            self.assertFalse(resolution.report["install"]["attempted"])
            self.assertTrue(resolution.paths.request.is_file())
            self.assertTrue(resolution.paths.report.is_file())
            self.assertFalse(resolution.paths.lock.exists())

    def test_failed_reinstall_retires_stale_ready_lock(self) -> None:
        def failing_runner(argv, *, cwd=None, timeout=None):
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                return subprocess.CompletedProcess(
                    args=normalized,
                    returncode=1,
                    stdout="",
                    stderr="resolver failed",
                )
            if "marker_environment" in normalized[3]:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {"packages": [], "installed_distributions": []},
            )

        with TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "03a_environment.lock.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "kind": "geng.case_environment.lock",
                        "ready": True,
                        "request_hash": "stale",
                    }
                ),
                encoding="utf-8",
            )

            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-failed-reinstall",
                target_interpreter=sys.executable,
                requirements=("brand-new-library>=1",),
                force=True,
                run_argv=failing_runner,
                now=lambda: _FIXED_TIME,
            )

            self.assertEqual(resolution.report["status"], "install_failed")
            self.assertFalse(resolution.ready)
            self.assertFalse(lock_path.exists())

    def test_invalid_install_report_cannot_be_laundered_by_empty_retry(self) -> None:
        install_round = 0

        def runner(argv, *, cwd=None, timeout=None):
            nonlocal install_round
            normalized = tuple(str(part) for part in argv)
            if not _is_pip_action(normalized, "install"):
                if "marker_environment" in normalized[3]:
                    return _result(normalized, _identity_payload())
                return _result(
                    normalized,
                    {"packages": [], "installed_distributions": []},
                )
            report_path = Path(normalized[normalized.index("--report") + 1])
            is_plan = "--dry-run" in normalized
            if is_plan:
                install_round += 1
            if install_round == 1 and is_plan:
                payload = {
                    "install": [
                        {
                            "metadata": {"name": "paper-lib", "version": "1.2"},
                            "download_info": {
                                "url": "https://files.pythonhosted.org/paper-lib-1.2.whl",
                                "archive_info": {"hashes": {"sha256": "a" * 64}},
                            },
                        }
                    ],
                    "environment": _identity_payload()["marker_environment"],
                }
            elif install_round == 1:
                payload = {
                    "install": [
                        {
                            "metadata": {"name": "paper-lib", "version": "1.2"},
                            "download_info": {
                                "url": "https://files.pythonhosted.org/paper-lib-1.2.whl",
                                "archive_info": {"hashes": {}},
                            },
                        }
                    ],
                    "environment": _identity_payload()["marker_environment"],
                }
            else:
                payload = {
                    "install": [],
                    "environment": _identity_payload()["marker_environment"],
                }
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(normalized, 0, "", "")

        with TemporaryDirectory() as temp_dir:
            arguments = {
                "case_dir": temp_dir,
                "case_id": "case-tainted-report",
                "target_interpreter": "/case/python",
                "requirements": ("paper-lib>=1",),
                "run_argv": runner,
                "verify_artifacts": True,
                "now": lambda: _FIXED_TIME,
            }
            first = resolve_case_environment(**arguments)
            second = resolve_case_environment(**arguments)

            self.assertFalse(first.ready)
            self.assertFalse(second.ready)
            self.assertIsNone(second.lock)
            self.assertFalse(second.paths.lock.exists())
            self.assertIn("empty pip resolution report", second.report["install"]["stderr"])

    def test_probes_the_target_interpreter_for_import_and_version(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv, *, cwd=None, timeout=None):
            normalized = tuple(str(part) for part in argv)
            calls.append(normalized)
            if _is_pip_action(normalized, "install"):
                return subprocess.CompletedProcess(
                    args="pip install was host mediated",
                    returncode=0,
                    stdout="installed",
                    stderr="",
                )
            return subprocess_argv_runner(normalized, cwd=cwd, timeout=timeout)

        with TemporaryDirectory() as temp_dir:
            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-real-probe",
                target_interpreter=sys.executable,
                requirements=(
                    RequirementRequest(
                        requirement="packaging>=20",
                        import_names=("packaging",),
                    ),
                ),
                run_argv=runner,
                now=lambda: _FIXED_TIME,
            )

        self.assertTrue(resolution.ready)
        self.assertEqual(resolution.lock["interpreter"]["executable"], sys.executable)
        requirement = resolution.lock["requirements"][0]
        self.assertIsNotNone(requirement["installed_version"])
        self.assertTrue(requirement["version_satisfied"])
        self.assertTrue(requirement["imports"]["packaging"]["ok"])
        self.assertEqual(locked_distributions(resolution.lock), frozenset({"packaging"}))
        self.assertEqual(len(calls), 2)
        self.assertFalse(resolution.report["install"]["attempted"])
        self.assertEqual(requirement["resolution_source"], "host_runtime")
        self.assertTrue(resolution.lock["source_policy"]["host_runtime_verified"])

    def test_mixed_runtime_installs_only_missing_requirements_with_shared_cache(self) -> None:
        installed_new = False
        pip_calls: list[tuple[str, ...]] = []
        pip_cwds: list[Path | None] = []
        cache_dirs: list[str] = []

        def runner(argv, *, cwd=None, timeout=None):
            nonlocal installed_new
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                pip_calls.append(normalized)
                pip_cwds.append(cwd)
                cache_dirs.append(normalized[normalized.index("--cache-dir") + 1])
                report_path = Path(normalized[normalized.index("--report") + 1])
                report_path.write_text(
                    json.dumps(
                        {
                            "install": [
                                {
                                    "metadata": {"name": "new-lib", "version": "2.1"},
                                    "download_info": {
                                        "url": "https://files.pythonhosted.org/new_lib-2.1.whl",
                                        "archive_info": {
                                            "hashes": {"sha256": "a" * 64}
                                        },
                                    },
                                }
                            ],
                            "environment": _identity_payload()["marker_environment"],
                        }
                    ),
                    encoding="utf-8",
                )
                if "--dry-run" not in normalized:
                    installed_new = True
                return subprocess.CompletedProcess(normalized, 0, "ok", "")
            if "marker_environment" in normalized[3]:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "host-lib",
                            "installed_version": "1.5",
                            "version_error": None,
                            "imports": {"host_lib": {"ok": True, "error": None}},
                            "successful_import_names": ["host_lib"],
                        },
                        {
                            "distribution": "new-lib",
                            "installed_version": "2.1" if installed_new else None,
                            "version_error": None if installed_new else "PackageNotFoundError",
                            "imports": {
                                "new_lib": {
                                    "ok": installed_new,
                                    "error": None if installed_new else "ModuleNotFoundError",
                                }
                            },
                            "successful_import_names": ["new_lib"] if installed_new else [],
                        },
                    ],
                    "installed_distributions": [
                        {"distribution": "host-lib", "version": "1.5"},
                        *(
                            [{"distribution": "new-lib", "version": "2.1"}]
                            if installed_new
                            else []
                        ),
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-mixed-host-runtime",
                target_interpreter="/case/python",
                requirements=(
                    RequirementRequest(
                        requirement="host-lib>=1",
                        import_names=("host_lib",),
                    ),
                    RequirementRequest(
                        requirement="new-lib>=2",
                        import_names=("new_lib",),
                    ),
                ),
                run_argv=runner,
                verify_artifacts=True,
                now=lambda: _FIXED_TIME,
            )

        self.assertTrue(resolution.ready)
        self.assertEqual(len(pip_calls), 2)
        self.assertEqual(pip_cwds, [None, None])
        self.assertEqual(len(set(cache_dirs)), 1)
        for argv in pip_calls:
            self.assertEqual(argv[1:5], ("-I", "-m", "pip", "install"))
            self.assertTrue(Path(argv[argv.index("--report") + 1]).is_absolute())
            self.assertTrue(Path(argv[argv.index("--cache-dir") + 1]).is_absolute())
            self.assertIn("new-lib>=2", argv)
            self.assertNotIn("host-lib>=1", argv)
        sources = {
            item["distribution"]: item["resolution_source"]
            for item in resolution.lock["requirements"]
        }
        self.assertEqual(
            sources,
            {"host-lib": "host_runtime", "new-lib": "trusted_index"},
        )
        self.assertTrue(resolution.lock["source_policy"]["host_runtime_verified"])
        self.assertTrue(resolution.lock["source_policy"]["trusted_index_installed"])
        self.assertEqual(
            [
                item["distribution"]
                for item in resolution.lock["source_policy"]["artifact_evidence"]["artifacts"]
            ],
            ["new-lib"],
        )

    def test_partial_lock_records_unresolved_dependency_without_granting_permission(self) -> None:
        def runner(argv, *, cwd=None, timeout=None):
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                return _result(normalized, {})
            if normalized[3].find("marker_environment") >= 0:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "missing-science-lib",
                            "installed_version": None,
                            "version_error": "PackageNotFoundError",
                            "imports": {
                                "missing_science_lib": {
                                    "ok": False,
                                    "error": "ModuleNotFoundError",
                                }
                            },
                        }
                    ],
                    "installed_distributions": [],
                },
            )

        with TemporaryDirectory() as temp_dir:
            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-partial",
                target_interpreter="/case/python",
                requirements=("missing-science-lib>=1",),
                run_argv=runner,
                now=lambda: _FIXED_TIME,
            )

            persisted_lock = json.loads(resolution.paths.lock.read_text(encoding="utf-8"))

        self.assertFalse(resolution.ready)
        self.assertEqual(resolution.report["status"], "probe_failed")
        self.assertFalse(persisted_lock["ready"])
        self.assertEqual(persisted_lock["requirements"][0]["state"], "unresolved")
        self.assertEqual(locked_distributions(persisted_lock), frozenset())
        self.assertEqual(
            resolution.report["probe"]["unresolved"][0]["distribution"],
            "missing-science-lib",
        )

    def test_environment_hash_uses_semantic_runtime_state_not_timestamps(self) -> None:
        base = {
            "request_hash": "request",
            "index": {"fingerprint": "index"},
            "interpreter": _identity_payload(),
            "requirements": [
                {
                    "requirement": "paper-lib>=1",
                    "applicable": True,
                    "installed_version": "1.2",
                    "version_satisfied": True,
                    "imports_ok": True,
                    "satisfied": True,
                }
            ],
            "installed_distributions": [
                {"distribution": "paper-lib", "version": "1.2"}
            ],
            "created_at": "first",
        }
        later = deepcopy(base)
        later["created_at"] = "later"
        changed = deepcopy(base)
        changed["requirements"][0]["installed_version"] = "2.0"

        self.assertEqual(_environment_hash(base), _environment_hash(later))
        self.assertNotEqual(_environment_hash(base), _environment_hash(changed))

        artifact_changed = deepcopy(base)
        base["source_policy"] = {
            "artifact_evidence": {
                "artifacts": [
                    {
                        "distribution": "paper-lib",
                        "version": "1.2",
                        "url": "https://files.pythonhosted.org/paper-lib.whl",
                        "sha256": "a" * 64,
                    }
                ]
            }
        }
        artifact_changed["source_policy"] = deepcopy(base["source_policy"])
        artifact_changed["source_policy"]["artifact_evidence"]["artifacts"][0][
            "sha256"
        ] = "b" * 64
        self.assertNotEqual(_environment_hash(base), _environment_hash(artifact_changed))

    def test_matching_lock_is_reprobed_without_reinstalling(self) -> None:
        install_calls = 0

        def runner(argv, *, cwd=None, timeout=None):
            nonlocal install_calls
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                install_calls += 1
                return _result(normalized, {})
            if normalized[3].find("marker_environment") >= 0:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "paper-lib",
                            "installed_version": "1.5",
                            "version_error": None,
                            "imports": {"paper_lib": {"ok": True, "error": None}},
                        }
                    ],
                    "installed_distributions": [
                        {"distribution": "paper-lib", "version": "1.5"}
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            arguments = {
                "case_dir": temp_dir,
                "case_id": "case-cache",
                "target_interpreter": "/case/python",
                "requirements": ("paper-lib>=1",),
                "run_argv": runner,
                "now": lambda: _FIXED_TIME,
            }
            first = resolve_case_environment(**arguments)
            second = resolve_case_environment(**arguments)

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertTrue(second.cache_hit)
        self.assertEqual(install_calls, 0)
        self.assertFalse(second.report["install"]["attempted"])

    def test_capability_enriched_lock_remains_a_dependency_cache_hit(self) -> None:
        install_calls = 0

        def runner(argv, *, cwd=None, timeout=None):
            nonlocal install_calls
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                install_calls += 1
                return _result(normalized, {})
            if "marker_environment" in normalized[3]:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "paper-lib",
                            "installed_version": "1.5",
                            "version_error": None,
                            "imports": {"paper_lib": {"ok": True, "error": None}},
                            "successful_import_names": ["paper_lib"],
                        }
                    ],
                    "installed_distributions": [
                        {"distribution": "paper-lib", "version": "1.5"}
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            arguments = {
                "case_dir": temp_dir,
                "case_id": "case-capability-cache",
                "target_interpreter": "/case/python",
                "requirements": ("paper-lib>=1",),
                "run_argv": runner,
                "now": lambda: _FIXED_TIME,
            }
            first = resolve_case_environment(**arguments)
            enriched = json.loads(first.paths.lock.read_text(encoding="utf-8"))
            enriched["capabilities"] = [
                {"framework": "paper-lib", "device_policy": "cpu", "ok": True}
            ]
            enriched["capabilities_ok"] = True
            enriched["environment_hash"] = _environment_hash(enriched)
            first.paths.lock.write_text(json.dumps(enriched), encoding="utf-8")
            second = resolve_case_environment(**arguments)

        self.assertTrue(second.cache_hit)
        self.assertEqual(install_calls, 0)
        self.assertNotIn("capabilities", second.lock)
        self.assertNotEqual(second.lock["environment_hash"], enriched["environment_hash"])

    def test_cache_rejects_forged_lock_fields_with_stale_hash(self) -> None:
        install_calls = 0

        def runner(argv, *, cwd=None, timeout=None):
            nonlocal install_calls
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                install_calls += 1
                return _result(normalized, {})
            if "marker_environment" in normalized[3]:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "paper-lib",
                            "installed_version": "1.5",
                            "version_error": None,
                            "imports": {"paper_lib": {"ok": True, "error": None}},
                            "successful_import_names": ["paper_lib"],
                        }
                    ],
                    "installed_distributions": [
                        {"distribution": "paper-lib", "version": "1.5"}
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            arguments = {
                "case_dir": temp_dir,
                "case_id": "case-forged-cache",
                "target_interpreter": "/case/python",
                "requirements": ("paper-lib>=1",),
                "run_argv": runner,
                "now": lambda: _FIXED_TIME,
            }
            first = resolve_case_environment(**arguments)
            forged = json.loads(first.paths.lock.read_text(encoding="utf-8"))
            forged["requirements"].append(
                {
                    "requirement": "forged-lib",
                    "distribution": "forged-lib",
                    "applicable": True,
                    "installed_version": "9.9",
                    "version_satisfied": True,
                    "imports_ok": True,
                    "satisfied": True,
                }
            )
            first.paths.lock.write_text(json.dumps(forged), encoding="utf-8")
            second = resolve_case_environment(**arguments)

        self.assertFalse(second.cache_hit)
        self.assertEqual(install_calls, 0)
        self.assertEqual(
            [item["distribution"] for item in second.lock["requirements"]],
            ["paper-lib"],
        )

    def test_unknown_distribution_discovers_its_actual_import_name(self) -> None:
        def runner(argv, *, cwd=None, timeout=None):
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                return _result(normalized, {})
            if "marker_environment" in normalized[3]:
                return _result(normalized, _identity_payload())
            payload = json.loads(normalized[4])
            self.assertTrue(payload["packages"][0]["allow_discovery"])
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "beautifulsoup4",
                            "installed_version": "4.13.0",
                            "version_error": None,
                            "imports": {
                                "beautifulsoup4": {"ok": False, "error": "ModuleNotFoundError"},
                                "bs4": {"ok": True, "error": None},
                            },
                            "successful_import_names": ["bs4"],
                        }
                    ],
                    "installed_distributions": [
                        {"distribution": "beautifulsoup4", "version": "4.13.0"}
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            resolution = resolve_case_environment(
                case_dir=temp_dir,
                case_id="case-import-discovery",
                target_interpreter="/case/python",
                requirements=("beautifulsoup4",),
                run_argv=runner,
                now=lambda: _FIXED_TIME,
            )

        self.assertTrue(resolution.ready)
        self.assertEqual(resolution.lock["requirements"][0]["import_names"], ["bs4"])

    def test_request_lock_and_report_are_atomically_committed_json(self) -> None:
        replace_destinations: list[Path] = []
        real_replace = os.replace

        def recording_replace(source, destination):
            replace_destinations.append(Path(destination))
            real_replace(source, destination)

        def runner(argv, *, cwd=None, timeout=None):
            normalized = tuple(str(part) for part in argv)
            if _is_pip_action(normalized, "install"):
                return _result(normalized, {})
            if normalized[3].find("marker_environment") >= 0:
                return _result(normalized, _identity_payload())
            return _result(
                normalized,
                {
                    "packages": [
                        {
                            "distribution": "paper-lib",
                            "installed_version": "1.0",
                            "version_error": None,
                            "imports": {"paper_lib": {"ok": True, "error": None}},
                        }
                    ],
                    "installed_distributions": [
                        {"distribution": "paper-lib", "version": "1.0"}
                    ],
                },
            )

        with TemporaryDirectory() as temp_dir:
            with patch("geng_agent.outputs.os.replace", side_effect=recording_replace):
                resolution = resolve_case_environment(
                    case_dir=temp_dir,
                    case_id="case-atomic",
                    target_interpreter="/case/python",
                    requirements=("paper-lib>=1",),
                    run_argv=runner,
                    now=lambda: _FIXED_TIME,
                )

            expected = {
                resolution.paths.request,
                resolution.paths.lock,
                resolution.paths.report,
            }
            self.assertEqual(set(replace_destinations), expected)
            for path in expected:
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
