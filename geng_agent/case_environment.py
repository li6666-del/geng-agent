"""Compatibility facade for case-scoped environment planning and resolution."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .outputs import write_json
from .environment_policy import (
    ArgvRunner,
    CaseEnvironmentPaths,
    CommandResult,
    DEFAULT_TRUSTED_INDEXES,
    ENVIRONMENT_LOCK_FILENAME,
    ENVIRONMENT_PIP_PLAN_FILENAME,
    ENVIRONMENT_PIP_REPORT_FILENAME,
    ENVIRONMENT_REPORT_FILENAME,
    ENVIRONMENT_REQUEST_FILENAME,
    ENVIRONMENT_SCHEMA_VERSION,
    EnvironmentPolicyError,
    EnvironmentProbeError,
    EnvironmentResolution,
    KNOWN_IMPORT_NAME_PROFILES,
    NormalizedRequirement,
    PYPI_INDEX,
    RequirementRequest,
    TrustedIndex,
    _sha256_json,
    build_environment_manifest,
    build_pip_install_argv,
    normalize_requirement,
    resolve_trusted_index,
)
from .environment_probe import (
    _environment_hash,
    _probe_environment,
    _resolution_hash,
    _unprivileged_executable_path,
)
from .environment_reports import (
    _base_report,
    _bounded_error,
    _bounded_output,
    _combine_artifact_evidence,
    _manifest_has_applicable_requirements,
    _probe_report,
    _read_regular_report_nofollow,
    _timestamp,
    load_environment_lock,
    locked_distributions,
    validate_pip_report,
)
from .environment_resolution import (
    _RESOLUTION_SOURCES,
    _apply_resolution_sources,
    _requirement_record_key,
    _resolution_sources_from_lock,
    _run,
    _unresolved_manifest,
    resolve_case_environment,
    subprocess_argv_runner,
)
