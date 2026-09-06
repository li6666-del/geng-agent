from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from geng_agent.case_environment import (
    CaseEnvironmentPaths,
    CommandResult,
    EnvironmentPolicyError,
    EnvironmentResolution,
)
from geng_agent.case_runtime import (
    EnvironmentResolutionError,
    HOST_SHARED_RUNTIME_MODE,
    _case_python_path,
    _case_venv_is_trusted,
    _copy_runtime_inventory,
    _host_shared_runtime_lock_target,
    _open_or_create_host_root,
    _probe_runtime_capabilities,
    _run_checked,
    _runtime_inventory_digest,
    _runtime_inventory_manifest,
    _write_case_venv_marker,
    ensure_case_runtime,
    read_environment_request,
    requirements_from_scientific_architecture,
    requirements_missing_from_lock,
)


def _architecture(
    *,
    framework: str,
    device_policy: str = "cpu",
    trainable: bool = False,
    gradient_mode: str = "not_applicable",
    checkpoint_policy: str = "not_applicable",
    required_capabilities: list[str] | None = None,
) -> dict:
    return {
        "components": [
            {
                "id": "model",
                "execution": {
                    "primary_framework": framework,
                    "supporting_libraries": [],
                    "device_policy": device_policy,
                    "trainable": trainable,
                    "gradient_mode": gradient_mode,
                    "checkpoint_policy": checkpoint_policy,
                    "required_capabilities": required_capabilities or [],
                },
            }
        ]
    }


def _ready_host_resolution(
    *,
    output_dir: Path,
    launcher: Path,
    prefix: Path,
) -> EnvironmentResolution:
    paths = CaseEnvironmentPaths.under(output_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "kind": "geng.case_environment.request",
        "case_id": output_dir.name,
        "target_interpreter": str(launcher),
        "requirements": [],
        "request_hash": "request-hash",
    }
    paths.request.write_text(json.dumps(manifest), encoding="utf-8")
    lock = {
        "schema_version": 1,
        "kind": "geng.case_environment.lock",
        "case_id": output_dir.name,
        "request_hash": "request-hash",
        "index": {},
        "source_policy": {},
        "interpreter": {
            "executable": str(launcher),
            "prefix": str(prefix),
            "base_prefix": str(prefix),
            "python_full_version": "3.11.0",
            "implementation": "cpython",
            "marker_environment": {},
            "sys_path": [str(prefix)],
        },
        "requirements": [],
        "ready": True,
        "resolution_hash": "resolution-hash",
        "environment_hash": "environment-hash",
    }
    report = {"ready": True, "status": "ready", "probe": {}}
    return EnvironmentResolution(paths=paths, manifest=manifest, lock=lock, report=report)


class CaseRuntimeRequestTests(unittest.TestCase):
    def test_unknown_architecture_package_is_requested_without_static_whitelist(self) -> None:
        architecture = _architecture(framework="novel-research-framework")

        requests = requirements_from_scientific_architecture(architecture)
        request = next(
            item for item in requests if item.requirement == "novel-research-framework"
        )

        self.assertFalse(request.import_names_explicit)
        self.assertEqual(request.import_names, ("novel_research_framework",))

    def test_python_prefixed_distribution_is_not_mistaken_for_the_runtime(self) -> None:
        architecture = _architecture(framework="python-docx")

        requests = requirements_from_scientific_architecture(architecture)

        self.assertIn("python-docx", {item.requirement for item in requests})

    def test_writer_request_accepts_safe_pep508_and_rejects_install_controls(self) -> None:
        with TemporaryDirectory() as temp_dir:
            sandbox = Path(temp_dir)
            request_path = sandbox / "environment_request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "requirements": [
                            {
                                "requirement": "Novel_Lib[cuda]>=2",
                                "import_names": ["novel_lib"],
                                "reason": "paper method requires its kernel",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            requests = read_environment_request(sandbox=sandbox, source="task_writer:test")
            self.assertEqual(requests[0].requirement, "novel-lib[cuda]>=2")
            self.assertTrue(requests[0].import_names_explicit)
            self.assertEqual(requests[0].requested_by, "task_writer:test")

            request_path.write_text(
                json.dumps(
                    {
                        "requirements": [
                            {"requirement": "novel-lib @ https://evil.invalid/lib.whl"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(EnvironmentPolicyError):
                read_environment_request(sandbox=sandbox, source="task_writer:test")

    def test_satisfied_version_constraint_does_not_extend_lock_for_textual_difference(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_text("numpy>=2.4,<3\n", encoding="utf-8")
            lock = {
                "interpreter": {"marker_environment": {"python_version": "3.11"}},
                "requirements": [
                    {
                        "requirement": "numpy",
                        "distribution": "numpy",
                        "applicable": True,
                        "installed_version": "2.4.3",
                        "version_satisfied": True,
                        "imports_ok": True,
                        "satisfied": True,
                    }
                ],
            }

            missing = requirements_missing_from_lock(
                requirements,
                lock,
                source="foundation_writer:requirements.txt",
            )

        self.assertEqual(missing, ())

    def test_unsatisfied_version_constraint_still_requests_environment_extension(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_text("numpy>=3\n", encoding="utf-8")
            lock = {
                "requirements": [
                    {
                        "requirement": "numpy",
                        "distribution": "numpy",
                        "applicable": True,
                        "installed_version": "2.4.3",
                        "version_satisfied": True,
                        "imports_ok": True,
                        "satisfied": True,
                    }
                ]
            }

            missing = requirements_missing_from_lock(
                requirements,
                lock,
                source="foundation_writer:requirements.txt",
            )

        self.assertEqual([item.requirement for item in missing], ["numpy>=3"])

    def test_unlocked_extra_is_not_treated_as_satisfied_by_base_distribution(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_text("novel-lib[cuda]>=2\n", encoding="utf-8")
            lock = {
                "requirements": [
                    {
                        "requirement": "novel-lib>=2",
                        "distribution": "novel-lib",
                        "applicable": True,
                        "installed_version": "2.1",
                        "version_satisfied": True,
                        "imports_ok": True,
                        "satisfied": True,
                    }
                ]
            }

            missing = requirements_missing_from_lock(
                requirements,
                lock,
                source="foundation_writer:requirements.txt",
            )

        self.assertEqual([item.requirement for item in missing], ["novel-lib[cuda]>=2"])

    def test_unmarked_requirement_requires_complete_applicable_lock_evidence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_text("numpy\n", encoding="utf-8")
            valid_record = {
                "requirement": "numpy",
                "distribution": "numpy",
                "applicable": True,
                "installed_version": "2.4.3",
                "version_satisfied": True,
                "imports_ok": True,
                "satisfied": True,
            }
            invalid_evidence = {
                "applicable": False,
                "satisfied": False,
                "version_satisfied": False,
                "imports_ok": False,
                "installed_version": None,
            }

            for field, value in invalid_evidence.items():
                with self.subTest(field=field):
                    record = {**valid_record, field: value}
                    missing = requirements_missing_from_lock(
                        requirements,
                        {"requirements": [record]},
                        source="foundation_writer:requirements.txt",
                    )

                    self.assertEqual(
                        [item.requirement for item in missing],
                        ["numpy"],
                    )

    def test_requirement_with_inapplicable_marker_does_not_extend_lock(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_text(
                'numpy; python_version < "3.0"\n',
                encoding="utf-8",
            )
            lock = {
                "interpreter": {
                    "marker_environment": {"python_version": "3.11"},
                },
                "requirements": [],
            }

            missing = requirements_missing_from_lock(
                requirements,
                lock,
                source="foundation_writer:requirements.txt",
            )

        self.assertEqual(missing, ())

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows")
    def test_requirements_symlink_is_rejected_without_reading_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = root / "secret.txt"
            secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
            requirements = root / "requirements.txt"
            requirements.symlink_to(secret)

            with self.assertRaises(EnvironmentPolicyError) as caught:
                requirements_missing_from_lock(requirements, {}, source="task_writer:test")

        self.assertNotIn("TOP-SECRET-CONTENT", str(caught.exception))

    def test_requirements_hardlink_is_rejected_without_reading_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = root / "secret.txt"
            secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
            requirements = root / "requirements.txt"
            os.link(secret, requirements)

            with self.assertRaises(EnvironmentPolicyError) as caught:
                requirements_missing_from_lock(requirements, {}, source="task_writer:test")

        self.assertNotIn("TOP-SECRET-CONTENT", str(caught.exception))

    def test_oversized_requirements_is_rejected_before_parsing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements = Path(temp_dir) / "requirements.txt"
            requirements.write_bytes(b"x" * (1024 * 1024 + 1))

            with self.assertRaisesRegex(EnvironmentPolicyError, "too large"):
                requirements_missing_from_lock(requirements, {}, source="task_writer:test")


class CaseRuntimeHostSharedTests(unittest.TestCase):
    def test_default_runtime_uses_host_prefix_without_creating_case_venv(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "case"
            audit = root / "audit"
            prefix = root / "host-runtime"
            launcher = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"host launcher sentinel")
            resolution = _ready_host_resolution(
                output_dir=output,
                launcher=launcher,
                prefix=prefix,
            )
            commands: list[tuple[str, ...]] = []
            command_cwds: list[Path | None] = []

            def ready_command(argv, *, cwd=None, timeout=None):
                normalized = tuple(str(item) for item in argv)
                commands.append(normalized)
                command_cwds.append(cwd)
                return CommandResult(argv=normalized, returncode=0)

            with patch.dict(
                os.environ,
                {"GENG_HOST_RUNTIME_LOCK_ROOT": str(root / "runtime-locks")},
            ), patch(
                "geng_agent.case_runtime.resolve_case_environment",
                return_value=resolution,
            ), patch(
                "geng_agent.case_runtime._probe_runtime_capabilities",
                return_value=[{"component_id": "model", "ok": True}],
            ):
                runtime = ensure_case_runtime(
                    output_dir=output,
                    audit_dir=audit,
                    scientific_architecture=_architecture(framework="packaging"),
                    base_interpreter=launcher,
                    resume=False,
                    run_argv=ready_command,
                )

            self.assertEqual(runtime.venv_dir, prefix.absolute())
            self.assertEqual(runtime.python_executable, launcher.absolute())
            self.assertFalse((audit / "03a_case_environment" / "venv").exists())
            self.assertFalse((prefix / ".geng_host_venv.json").exists())
            self.assertFalse(
                any(command[1:3] == ("-m", "venv") for command in commands)
            )
            self.assertEqual(
                commands,
                [(str(launcher.absolute()), "-I", "-m", "pip", "check")],
            )
            self.assertEqual(command_cwds, [None])
            persisted_lock = json.loads(runtime.lock_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted_lock["runtime_mode"], HOST_SHARED_RUNTIME_MODE)
            self.assertEqual(
                persisted_lock["host_provenance"]["selected_launcher"],
                str(launcher.absolute()),
            )
            self.assertEqual(
                persisted_lock["host_provenance"]["prefix"],
                str(prefix.absolute()),
            )
            self.assertTrue(runtime.request_path.is_file())
            self.assertTrue(runtime.report_path.is_file())

    def test_host_failure_never_retires_shared_prefix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "case"
            audit = root / "audit"
            prefix = root / "host-runtime"
            launcher = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"host launcher sentinel")
            sentinel = prefix / "must-survive.txt"
            sentinel.write_text("shared host state", encoding="utf-8")
            resolution = _ready_host_resolution(
                output_dir=output,
                launcher=launcher,
                prefix=prefix,
            )

            def failed_pip_check(argv, *, cwd=None, timeout=None):
                normalized = tuple(str(item) for item in argv)
                return CommandResult(
                    argv=normalized,
                    returncode=1,
                    stderr="host dependency conflict",
                )

            with patch.dict(
                os.environ,
                {"GENG_HOST_RUNTIME_LOCK_ROOT": str(root / "runtime-locks")},
            ), patch(
                "geng_agent.case_runtime.resolve_case_environment",
                return_value=resolution,
            ), patch("geng_agent.case_runtime._retire_case_venv") as retire:
                with self.assertRaises(EnvironmentResolutionError) as caught:
                    ensure_case_runtime(
                        output_dir=output,
                        audit_dir=audit,
                        scientific_architecture=_architecture(framework="packaging"),
                        base_interpreter=launcher,
                        resume=False,
                        run_argv=failed_pip_check,
                    )

            self.assertEqual(caught.exception.category, "abi_conflict")
            self.assertTrue(launcher.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "shared host state")
            self.assertFalse((output / "03a_environment.lock.json").exists())
            retire.assert_not_called()

    def test_shared_lock_target_is_stable_for_same_real_host_interpreter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prefix = root / "host-runtime"
            launcher = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b"host launcher sentinel")
            alias = launcher.with_name("python-alias.exe" if os.name == "nt" else "python-alias")
            os.link(launcher, alias)

            with patch.dict(
                os.environ,
                {"GENG_HOST_RUNTIME_LOCK_ROOT": str(root / "runtime-locks")},
            ):
                first = _host_shared_runtime_lock_target(launcher)
                second = _host_shared_runtime_lock_target(alias)
                repeated = _host_shared_runtime_lock_target(launcher)

            self.assertEqual(first, second)
            self.assertEqual(first, repeated)
            self.assertEqual(first.parent, root / "runtime-locks")


class CaseRuntimeIsolationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0,
        "root-owned POSIX metadata is required",
    )
    def test_host_root_rejects_symlink_without_mutating_its_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            target = parent / "target"
            target.mkdir(mode=0o700)
            link = parent / "runtime-root"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises(EnvironmentResolutionError) as caught:
                _open_or_create_host_root(link)

            self.assertEqual(caught.exception.category, "unsafe_runtime_root")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(
        os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0,
        "root-owned POSIX metadata is required",
    )
    def test_host_root_repairs_restrictive_umask_after_nofollow_verification(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime-root"
            previous_umask = os.umask(0o077)
            try:
                _open_or_create_host_root(root)
            finally:
                os.umask(previous_umask)

            self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_runtime_mirror_uses_positive_inventory_and_matching_copy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prefix = root / "source"
            python = prefix / "bin" / "python3.11"
            stdlib = prefix / "lib" / "python3.11"
            (stdlib / "encodings").mkdir(parents=True)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"synthetic-python")
            python.chmod(0o755)
            (stdlib / "encodings" / "__init__.py").write_text("", encoding="utf-8")
            (stdlib / "os.py").write_text("name = 'posix'\n", encoding="utf-8")
            (stdlib / "site-packages").mkdir()
            (stdlib / "site-packages" / "untrusted.py").write_text(
                "SECRET = True\n",
                encoding="utf-8",
            )
            (prefix / "lib" / "libpython3.11.so.1.0").write_bytes(b"shared-library")
            (prefix / "lib" / "build-config.txt").write_text("excluded", encoding="utf-8")
            (prefix / "bin" / "pip").write_text("excluded", encoding="utf-8")
            (prefix / "etc").mkdir()
            (prefix / "etc" / "aau_token").write_text("never-copy", encoding="utf-8")
            (prefix / "share" / "doc").mkdir(parents=True)
            (prefix / "share" / "doc" / "readme").write_text("excluded", encoding="utf-8")

            manifest = _runtime_inventory_manifest(
                source_prefix=prefix,
                resolved_python=python,
                stdlib=stdlib,
            )
            selected = {str(entry["path"]) for entry in manifest}
            self.assertIn("bin/python3.11", selected)
            self.assertIn("lib/python3.11/os.py", selected)
            self.assertIn("lib/libpython3.11.so.1.0", selected)
            self.assertNotIn("bin/pip", selected)
            self.assertNotIn("etc/aau_token", selected)
            self.assertNotIn("share/doc/readme", selected)
            self.assertFalse(any("site-packages" in path for path in selected))

            mirror = root / "mirror"
            _copy_runtime_inventory(
                source_prefix=prefix,
                destination_prefix=mirror,
                manifest=manifest,
            )
            copied_manifest = _runtime_inventory_manifest(
                source_prefix=mirror,
                resolved_python=mirror / "bin" / "python3.11",
                stdlib=mirror / "lib" / "python3.11",
            )
            self.assertEqual(copied_manifest, manifest)
            self.assertFalse((mirror / "etc" / "aau_token").exists())
            self.assertFalse((mirror / "bin" / "pip").exists())

    def test_runtime_inventory_hashes_selected_content_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            prefix = Path(temp_dir) / "source"
            python = prefix / "bin" / "python3.11"
            stdlib = prefix / "lib" / "python3.11"
            (stdlib / "encodings").mkdir(parents=True)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
            python.chmod(0o755)
            selected = stdlib / "module.py"
            selected.write_text("VALUE = 1\n", encoding="utf-8")
            excluded = prefix / "etc" / "host-secret"
            excluded.parent.mkdir()
            excluded.write_text("first", encoding="utf-8")

            initial = _runtime_inventory_digest(
                source_prefix=prefix,
                resolved_python=python,
                stdlib=stdlib,
            )
            excluded.write_text("second", encoding="utf-8")
            self.assertEqual(
                initial,
                _runtime_inventory_digest(
                    source_prefix=prefix,
                    resolved_python=python,
                    stdlib=stdlib,
                ),
            )
            selected.write_text("VALUE = 2\n", encoding="utf-8")
            self.assertNotEqual(
                initial,
                _runtime_inventory_digest(
                    source_prefix=prefix,
                    resolved_python=python,
                    stdlib=stdlib,
                ),
            )

    @unittest.skipIf(os.name == "nt", "POSIX link semantics are required")
    def test_runtime_inventory_rejects_external_and_unselected_links(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prefix = root / "source"
            python = prefix / "bin" / "python3.11"
            stdlib = prefix / "lib" / "python3.11"
            (stdlib / "encodings").mkdir(parents=True)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
            python.chmod(0o755)
            external = root / "outside.py"
            external.write_text("outside", encoding="utf-8")
            bad_link = stdlib / "external.py"
            bad_link.symlink_to(external)

            with self.assertRaisesRegex(EnvironmentResolutionError, "escapes"):
                _runtime_inventory_manifest(
                    source_prefix=prefix,
                    resolved_python=python,
                    stdlib=stdlib,
                )

            bad_link.unlink()
            internal_unselected = prefix / "etc" / "config.py"
            internal_unselected.parent.mkdir()
            internal_unselected.write_text("config", encoding="utf-8")
            bad_link.symlink_to(internal_unselected)
            with self.assertRaisesRegex(EnvironmentResolutionError, "unselected target"):
                _runtime_inventory_manifest(
                    source_prefix=prefix,
                    resolved_python=python,
                    stdlib=stdlib,
                )

    def test_case_venv_reuse_requires_isolated_config_bound_to_host_marker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            venv = root / "venv"
            case_python = _case_python_path(venv)
            case_python.parent.mkdir(parents=True)
            if os.name == "nt":
                case_python.write_bytes(b"synthetic launcher")
            else:
                case_python.symlink_to(Path(sys.executable).resolve())
            site_packages = (
                venv / "Lib" / "site-packages"
                if os.name == "nt"
                else venv
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            site_packages.mkdir(parents=True)
            config = venv / "pyvenv.cfg"
            config.write_text(
                f"home = {Path(sys.executable).resolve().parent}\n"
                "include-system-site-packages = false\n",
                encoding="utf-8",
            )
            _write_case_venv_marker(
                venv_dir=venv,
                base_python=Path(sys.executable),
                output_dir=root,
            )

            with patch("geng_agent.case_runtime._trusted_host_path", return_value=True), patch(
                "geng_agent.case_runtime._unprivileged_executable_path",
                return_value=True,
            ):
                self.assertTrue(
                    _case_venv_is_trusted(
                        venv_dir=venv,
                        case_python=case_python,
                        base_python=Path(sys.executable),
                        output_dir=root,
                    )
                )
                marker_path = venv / ".geng_host_venv.json"
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker["schema_version"] = 999
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                self.assertFalse(
                    _case_venv_is_trusted(
                        venv_dir=venv,
                        case_python=case_python,
                        base_python=Path(sys.executable),
                        output_dir=root,
                    )
                )
                _write_case_venv_marker(
                    venv_dir=venv,
                    base_python=Path(sys.executable),
                    output_dir=root,
                )
                config.write_text(
                    f"home = {Path(sys.executable).resolve().parent}\n"
                    "include-system-site-packages = true\n",
                    encoding="utf-8",
                )
                _write_case_venv_marker(
                    venv_dir=venv,
                    base_python=Path(sys.executable),
                    output_dir=root,
                )
                self.assertFalse(
                    _case_venv_is_trusted(
                        venv_dir=venv,
                        case_python=case_python,
                        base_python=Path(sys.executable),
                        output_dir=root,
                    )
                )


class CaseRuntimeCapabilityTests(unittest.TestCase):
    def test_numpy_domain_capabilities_are_deferred_to_component_runtime_tests(self) -> None:
        architecture = _architecture(
            framework="numpy",
            device_policy="cpu",
            trainable=False,
            gradient_mode="not_required",
            checkpoint_policy="not_applicable",
            required_capabilities=[
                "huffman_coding",
                "reed_solomon",
                "qam_modulation",
            ],
        )
        with TemporaryDirectory() as temp_dir:
            capabilities = _probe_runtime_capabilities(
                case_python=Path(sys.executable),
                architecture=architecture,
                output_dir=Path(temp_dir),
                run_argv=lambda argv, *, cwd=None, timeout=None: subprocess.run(
                    argv,
                    cwd=cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                ),
            )

        self.assertEqual(len(capabilities), 1)
        self.assertTrue(capabilities[0]["ok"], capabilities[0])
        self.assertIn("linear_algebra", capabilities[0]["evidence"])
        self.assertEqual(
            capabilities[0]["evidence"]["declared_component_capabilities"],
            ["huffman_coding", "reed_solomon", "qam_modulation"],
        )
        self.assertEqual(
            capabilities[0]["evidence"][
                "declared_component_capabilities_verification"
            ],
            "deferred_to_foundation_or_task_runtime_tests",
        )

    def test_unknown_framework_cannot_fake_advanced_capabilities(self) -> None:
        architecture = _architecture(
            framework="packaging",
            device_policy="accelerator_required",
            trainable=True,
            gradient_mode="required",
            checkpoint_policy="required",
            required_capabilities=["autograd", "checkpoint_roundtrip"],
        )
        with TemporaryDirectory() as temp_dir:
            capabilities = _probe_runtime_capabilities(
                case_python=Path(sys.executable),
                architecture=architecture,
                output_dir=Path(temp_dir),
                run_argv=lambda argv, *, cwd=None, timeout=None: subprocess.run(
                    argv,
                    cwd=cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                ),
            )

        self.assertEqual(len(capabilities), 1)
        self.assertFalse(capabilities[0]["ok"])
        self.assertIn("no trusted generic probe", capabilities[0]["error"])

    def test_unknown_cpu_framework_can_use_prior_import_and_version_proof(self) -> None:
        architecture = _architecture(framework="packaging")
        with TemporaryDirectory() as temp_dir:
            capabilities = _probe_runtime_capabilities(
                case_python=Path(sys.executable),
                architecture=architecture,
                output_dir=Path(temp_dir),
                run_argv=lambda argv, *, cwd=None, timeout=None: subprocess.run(
                    argv,
                    cwd=cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                ),
            )

        self.assertTrue(capabilities[0]["ok"])
        self.assertEqual(
            capabilities[0]["evidence"]["mode"],
            "import_and_version_probe_from_case_lock",
        )

    def test_host_command_exception_is_normalized_as_environment_failure(self) -> None:
        def timed_out(argv, *, cwd=None, timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(EnvironmentResolutionError) as caught:
            _run_checked(
                timed_out,
                (sys.executable, "-m", "venv", "case"),
                cwd=None,
                timeout=1,
            )

        self.assertEqual(caught.exception.category, "environment_command_failed")
        self.assertNotIn(sys.executable, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
