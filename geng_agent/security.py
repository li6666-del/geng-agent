"""Compatibility facade for generated-project security and dependency policy."""

from __future__ import annotations

# Keep these module objects importable from the historical facade. Existing
# integrations patching geng_agent.security.importlib.util.find_spec continue
# to affect the shared stdlib module object used by the owner module.
import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .dependency_import_policy import (
    catches_broad_exception,
    collect_imports_from_node,
    collect_local_module_roots,
    collect_third_party_imports,
    is_third_party_import,
    iter_project_python_files,
    reconcile_runtime_requirements,
    uses_trusted_torch_backend,
    validate_import_requirements,
    validate_requirements,
)
from .dependency_policy import (
    DEFAULT_REPRO_PACKAGE_PROFILES,
    IMPORT_REQUIREMENT_NAMES,
    KNOWN_PACKAGE_PROFILES,
    REQUIREMENT_IMPORT_NAMES,
    RuntimeDocument,
    _DISTRIBUTION_ALIASES,
    _UNDECLARED_IMPORT_RE,
    _canonical_distribution_name,
    _lock_requirement_matches,
    _matching_lock_record,
    _parse_safe_requirement,
    _record_distribution,
    _requirement_imports_available,
    _runtime_dependency_state,
    _runtime_document,
    _runtime_import_names,
    _runtime_lock_binding_issues,
    _runtime_lock_is_trusted,
    _runtime_requirement_records,
    dependency_policy_prompt_text,
    import_names_for_requirement,
    is_nonblocking_requirement_issue,
    requirement_name_for_import,
    split_requirement_issues,
)
from .security_env import (
    SECRET_PATTERNS,
    SENSITIVE_ENV_KEYS,
    _default_geng_python,
    _prefer_geng_python_for_codex,
    _select_geng_python,
    _valid_python_path,
    build_safe_env,
    codex_safe_env,
    redact_data,
    redact_text,
)
from .security_static_policy import (
    DANGEROUS_DYNAMIC_IMPORT_ROOTS,
    DANGEROUS_REFLECTION_ATTRIBUTES,
    FORBIDDEN_BUILTINS,
    FORBIDDEN_CALLS,
    FORBIDDEN_DUNDER_ATTRS,
    FORBIDDEN_IMPORTS,
    FOUNDATION_STATIC_SECURITY_ADVISORY_CATEGORIES,
    ORDINARY_REFLECTION_BUILTINS,
    TRUSTED_RUNTIME_FILES,
    _call_name,
)
from .security_static_scan import (
    _add_one_level_callable_aliases,
    _call_argument,
    _check_absolute_path_literal,
    _dangerous_dynamic_import_message,
    _dangerous_environment_call_message,
    _dangerous_environment_subscript_message,
    _dangerous_reflection_message,
    _dangerous_reflection_subscript_message,
    _environment_key_issue,
    _importlib_package_root,
    _is_dangerous_loader_reflection,
    _is_sensitive_callable_name,
    _resolve_security_name,
    _security_import_aliases,
    _static_string_value,
    classify_static_security_issue,
    split_static_security_issues,
    static_scan_repro_project,
)
