"""Validate that generated task entrypoints use their assigned scientific components."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .task_writer_files import _read_optional_json_object, _task_source_files
from .task_writer_support import PAPER_EVIDENCE_DIR
from .foundation_scope import derive_foundation_scope


def _load_task_execution_binding(sandbox: Path, task_id: str) -> dict[str, Any] | None:
    '''Load the task-scoped scientific execution contract from its sandbox copy.'''

    architecture = _read_optional_json_object(
        sandbox
        / PAPER_EVIDENCE_DIR
        / 'analysis_artifacts'
        / 'scientific_architecture.json'
    )
    execution_plan = _read_optional_json_object(
        sandbox / PAPER_EVIDENCE_DIR / 'analysis_artifacts' / 'execution_plan.json'
    )
    return _task_execution_binding_from_architecture(architecture, task_id, execution_plan)

def _task_execution_binding_from_architecture(
    architecture: Any,
    task_id: str,
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    '''Resolve a 1.1 binding to concrete component execution records.

    Architecture 1.0 deliberately returns ``None`` so existing cases retain
    their legacy writer prompt and delivery behavior.
    '''

    if not isinstance(architecture, dict) or str(architecture.get('schema_version') or '') != '1.1':
        return None
    raw_components = architecture.get('components')
    components_by_id = {
        str(item.get('id')): item
        for item in raw_components
        if isinstance(item, dict) and str(item.get('id') or '')
    } if isinstance(raw_components, list) else {}
    raw_bindings = architecture.get('bindings')
    bindings = [
        item for item in raw_bindings
        if isinstance(item, dict) and str(item.get('task_id') or '') == str(task_id)
    ] if isinstance(raw_bindings, list) else []
    binding = bindings[0] if bindings else None
    scope = derive_foundation_scope(architecture, execution_plan)
    shared_ids = set(scope['component_ids'])
    configuration_issues: list[str] = []
    bound_components: list[dict[str, Any]] = []
    raw_groups = architecture.get('consistency_groups')
    consistency_groups = [
        str(group.get('id') or '')
        for group in (raw_groups if isinstance(raw_groups, list) else [])
        if isinstance(group, dict)
        and str(group.get('id') or '')
        and str(task_id) in {
            str(item)
            for item in group.get('task_ids', [])
        }
    ]
    if not isinstance(binding, dict):
        configuration_issues.append(f'no scientific_architecture/1.1 binding exists for task {task_id}')
    else:
        component_ids: list[str] = []
        for item in bindings:
            if not isinstance(item.get('components'), list):
                configuration_issues.append('binding.components must be a list of component IDs')
                continue
            component_ids.extend(str(value) for value in item['components'])
        component_ids = list(dict.fromkeys([
            *component_ids,
            *scope['task_component_ids'].get(str(task_id), []),
        ]))
        for raw_component_id in component_ids:
            component_id = str(raw_component_id or '')
            component = components_by_id.get(component_id)
            if not isinstance(component, dict):
                label = component_id or '<empty>'
                configuration_issues.append(f'binding refers to unknown component {label}')
                continue
            execution = component.get('execution')
            bound_components.append(
                {
                    'component_id': component_id,
                    'module': str(component.get('module') or ''),
                    'callable': str(component.get('callable') or ''),
                    'execution': dict(execution) if isinstance(execution, dict) else {},
                    'ownership': 'foundation' if component_id in shared_ids else 'execution_unit',
                }
            )
    return {
        'schema_version': '1.1',
        'task_id': str(task_id),
        'experiment_id': str(binding.get('experiment_id') or '') if isinstance(binding, dict) else '',
        'experiment_ids': list(dict.fromkeys(str(item.get('experiment_id') or '') for item in bindings)),
        'bindings': [dict(item) for item in bindings],
        'consistency_group': str(binding.get('consistency_group') or '') if isinstance(binding, dict) else '',
        'consistency_groups': consistency_groups,
        'components': bound_components,
        'configuration_issues': configuration_issues,
    }

def _task_execution_binding_issues(
    *,
    sandbox: Path,
    task_id: str,
    result_doc: Any,
    execution_binding: dict[str, Any] | None = None,
) -> list[str]:
    '''Apply the low-false-positive static gate for architecture 1.1.'''

    contract = execution_binding or _load_task_execution_binding(sandbox, task_id)
    if not isinstance(contract, dict) or str(contract.get('schema_version') or '') != '1.1':
        return []
    issues = [str(item) for item in contract.get('configuration_issues', []) if str(item)]
    components = [item for item in contract.get('components', []) if isinstance(item, dict)]
    usage_items = result_doc.get('component_usage') if isinstance(result_doc, dict) else None
    usage_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(usage_items, list):
        issues.append('task_agent_result.json must contain component_usage for every bound component')
        usage_items = []
    for index, item in enumerate(usage_items):
        if not isinstance(item, dict):
            issues.append(f'component_usage[{index}] must be an object')
            continue
        component_id = str(item.get('component_id') or '')
        if not component_id:
            issues.append(f'component_usage[{index}].component_id is empty')
        elif component_id in usage_by_id:
            issues.append(f'component_usage contains duplicate component {component_id}')
        else:
            usage_by_id[component_id] = item

    expected_ids = {str(item.get('component_id') or '') for item in components}
    for unexpected in sorted(set(usage_by_id) - expected_ids):
        issues.append(f'component_usage declares unbound component {unexpected}')

    source_facts = _inspect_task_execution_source(sandbox, task_id)
    imported_modules = source_facts['imported_modules']
    reachable_task_files = source_facts['reachable_task_files']
    for component in components:
        component_id = str(component.get('component_id') or '')
        module = str(component.get('module') or '')
        callable_name = str(component.get('callable') or '')
        execution = component.get('execution') if isinstance(component.get('execution'), dict) else {}
        usage = usage_by_id.get(component_id)
        if not isinstance(usage, dict):
            issues.append(f'component_usage is missing bound component {component_id}')
        else:
            if str(usage.get('module') or '') != module:
                issues.append(f'{component_id}: component_usage.module must equal declared module {module}')
            if str(usage.get('callable') or '') != callable_name:
                issues.append(f'{component_id}: component_usage.callable must equal declared callable {callable_name}')
            usage_kind = str(usage.get('usage') or '')
            if usage_kind not in {'in_scientific_path', 'reference_only', 'not_used'}:
                issues.append(
                    f'{component_id}: usage must be in_scientific_path, reference_only, or not_used'
                )
            evidence = usage.get('evidence_files')
            evidence_items = (
                [str(item).strip() for item in evidence if str(item).strip()]
                if isinstance(evidence, list)
                else []
            )
            if not evidence_items:
                issues.append(f'{component_id}: evidence_files must identify the task scientific path')
            for raw_evidence in evidence_items:
                relative = _sandbox_evidence_source(sandbox, raw_evidence)
                if relative is None:
                    issues.append(
                        f'{component_id}: evidence file {raw_evidence!r} must exist inside the sandbox'
                    )
                elif relative.casefold() not in reachable_task_files:
                    issues.append(
                        f'{component_id}: evidence file {raw_evidence!r} is not in the assigned task import closure'
                    )
            if execution.get('shared_implementation') is True and usage_kind != 'in_scientific_path':
                declared = usage_kind or 'undeclared'
                issues.append(
                    f'{component_id}: shared_implementation must be used in_scientific_path, not {declared}'
                )

        expected_import = _normalize_python_module(module)
        if not expected_import:
            issues.append(f'{component_id}: declared component module is empty')
        elif expected_import not in imported_modules:
            issues.append(
                f'{component_id}: expected module {expected_import} is not reachable from the assigned task entry through task-local and src import graphs'
            )
        elif callable_name and not _declared_callable_is_called(
            source_facts,
            module=expected_import,
            callable_name=callable_name,
        ):
            issues.append(
                f'{component_id}: declared callable {expected_import}.{callable_name} is imported but not called from the assigned task scientific path'
            )
    return _dedupe_strings(issues)

def _normalize_python_module(module: str) -> str:
    value = str(module or '').strip().replace('\\', '/').lstrip('./')
    if value.endswith('/__init__.py'):
        value = value[:-12]
    elif value.endswith('.py'):
        value = value[:-3]
    return value.strip('/').replace('/', '.')

def _inspect_task_execution_source(sandbox: Path, task_id: str) -> dict[str, Any]:
    '''Inspect only the source closure rooted at the assigned task entrypoint.'''

    module_paths, module_names = _task_module_index(sandbox)
    entry = _assigned_task_entrypoint(sandbox, task_id)
    imported_modules: set[str] = set()
    reachable_task_files: set[str] = set()
    pending = [entry.resolve()] if entry is not None else []
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        module_name = module_names.get(path)
        if not module_name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        reachable_task_files.add(path.relative_to(sandbox.resolve()).as_posix().casefold())
        imported = _imports_from_local_module(
            tree,
            module_name=module_name,
            is_package=path.name == '__init__.py',
        )
        imported_modules.update(imported)
        for imported_name in imported:
            candidate = module_paths.get(imported_name)
            if candidate is not None and candidate not in visited:
                pending.append(candidate)
    reachable_src_modules = _reachable_local_src_modules(sandbox, imported_modules)
    callable_usage = _static_callable_usage(
        sandbox,
        entry=entry,
        reachable_task_paths=visited,
        reachable_src_modules=reachable_src_modules,
    )
    return {
        'imported_modules': reachable_src_modules,
        'direct_imported_modules': imported_modules,
        'reachable_task_files': reachable_task_files,
        'called_symbols': callable_usage,
    }

def _assigned_task_entrypoint(sandbox: Path, task_id: str) -> Path | None:
    '''Prefer the trusted manifest entry, then fall back to a task-id filename.'''

    manifest = _read_optional_json_object(sandbox / 'tasks_manifest.json')
    raw_entries = manifest.get('tasks') if isinstance(manifest, dict) else None
    for entry in raw_entries if isinstance(raw_entries, list) else []:
        if not isinstance(entry, dict) or str(entry.get('task_id') or '') != str(task_id):
            continue
        candidates: list[str] = []
        script = str(entry.get('script') or '').strip()
        module = str(entry.get('module') or '').strip()
        if script:
            candidates.append(script)
        if module:
            candidates.append(f"tasks/{module.replace('.', '/')}.py")
        for raw in candidates:
            path = _safe_task_source_path(sandbox, raw)
            if path is not None:
                return path

    raw_task_id = str(task_id or '').strip()
    fallback_names = [raw_task_id]
    slug = ''.join(
        character if character.isalnum() or character == '_' else '_'
        for character in raw_task_id
    ).strip('_').lower()
    if slug and slug[0].isdigit():
        slug = f't_{slug}'
    if slug and slug not in fallback_names:
        fallback_names.append(slug)
    for name in fallback_names:
        path = _safe_task_source_path(sandbox, f'tasks/{name}.py')
        if path is not None:
            return path
    return None

def _safe_task_source_path(sandbox: Path, raw: str) -> Path | None:
    value = str(raw or '').strip().replace('\\', '/')
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = sandbox / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to((sandbox / 'tasks').resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink() or resolved.suffix.lower() != '.py':
        return None
    return resolved

def _task_module_index(sandbox: Path) -> tuple[dict[str, Path], dict[Path, str]]:
    module_paths: dict[str, Path] = {}
    module_names: dict[Path, str] = {}
    for path in _task_source_files(sandbox):
        resolved = path.resolve()
        relative = path.relative_to(sandbox).with_suffix('')
        parts = list(relative.parts)
        if parts and parts[-1] == '__init__':
            parts.pop()
        module_name = '.'.join(parts)
        if not module_name:
            continue
        module_names[resolved] = module_name
        module_paths[module_name] = resolved
        if module_name.startswith('tasks.'):
            module_paths.setdefault(module_name[len('tasks.'):], resolved)
    return module_paths, module_names

def _sandbox_evidence_source(sandbox: Path, raw: str) -> str | None:
    '''Resolve an optional line suffix and require a real sandbox file.'''

    value = str(raw or '').strip().replace('\\', '/')
    prefix, separator, suffix = value.rpartition(':')
    compact_suffix = suffix.replace('-', '')
    if separator and (
        suffix.casefold() == 'line'
        or compact_suffix.isdigit()
        or (suffix[:1].casefold() == 'l' and suffix[1:].isdigit())
    ):
        value = prefix
    fragment_prefix, fragment, fragment_line = value.rpartition('#L')
    if fragment and fragment_line.isdigit():
        value = fragment_prefix
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = sandbox / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(sandbox.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return relative.as_posix()

def _declared_callable_is_called(
    source_facts: dict[str, Any],
    *,
    module: str,
    callable_name: str,
) -> bool:
    target = '.'.join(
        part
        for part in (
            _normalize_python_module(module),
            str(callable_name or '').strip().replace(':', '.').strip('.'),
        )
        if part
    )
    if not target:
        return False
    for raw_symbol in source_facts.get('called_symbols', set()):
        symbol = str(raw_symbol or '')
        if symbol == target or symbol.startswith(f'{target}.'):
            return True
        if target.endswith('.__call__') and symbol == target[:-9]:
            return True
    return False

def _static_callable_usage(
    sandbox: Path,
    *,
    entry: Path | None,
    reachable_task_paths: set[Path],
    reachable_src_modules: set[str],
) -> set[str]:
    '''Return call targets reachable from the assigned task entry symbols.'''

    _task_paths, task_names = _task_module_index(sandbox)
    src_paths = _src_module_index(sandbox)
    selected: dict[str, Path] = {}
    for path in reachable_task_paths:
        module_name = task_names.get(path.resolve())
        if module_name:
            selected[module_name] = path.resolve()
    for module_name, path in src_paths.items():
        if module_name in reachable_src_modules or any(
            value.startswith(f'{module_name}.')
            for value in reachable_src_modules
        ):
            selected[module_name] = path.resolve()

    graph: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for module_name, path in selected.items():
        analysis = _analyze_static_module(path, module_name)
        aliases.update(analysis['aliases'])
        for owner, targets in analysis['graph'].items():
            graph.setdefault(owner, set()).update(targets)

    if entry is None:
        return set()
    entry_module = task_names.get(entry.resolve())
    if not entry_module:
        return set()
    roots = {f'{entry_module}.__module__'}
    return _walk_static_calls(roots, graph=graph, aliases=aliases)

def _src_module_index(sandbox: Path) -> dict[str, Path]:
    module_paths: dict[str, Path] = {}
    src_root = sandbox / 'src'
    if not src_root.is_dir():
        return module_paths
    for path in src_root.rglob('*.py'):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(sandbox).with_suffix('')
        parts = list(relative.parts)
        if parts and parts[-1] == '__init__':
            parts.pop()
        module_name = '.'.join(parts)
        if module_name:
            module_paths[module_name] = path.resolve()
    return module_paths

def _analyze_static_module(path: Path, module_name: str) -> dict[str, Any]:
    graph: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    try:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return {
            'graph': graph,
            'aliases': aliases,
        }
    is_package = path.name == '__init__.py'
    module_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol = f'{module_name}.{node.name}'
            module_aliases[node.name] = symbol
        elif isinstance(node, ast.ClassDef):
            module_aliases[node.name] = f'{module_name}.{node.name}'

    module_owner = f'{module_name}.__module__'
    module_scanner = _StaticCallScanner(
        owner=module_owner,
        aliases=module_aliases,
        graph=graph,
        module_name=module_name,
        is_package=is_package,
    )
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + [
                item for item in node.args.kw_defaults if item is not None
            ]:
                module_scanner.visit(default)
            continue
        if isinstance(node, ast.ClassDef):
            continue
        module_scanner.visit(node)
    module_aliases = module_scanner.aliases

    for local_name, target in module_aliases.items():
        if not local_name or '.' in local_name:
            continue
        exported = f'{module_name}.{local_name}'
        if target and target != exported:
            aliases[exported] = target

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = f'{module_name}.{node.name}'
            _analyze_static_function(
                node,
                owner=owner,
                base_aliases=module_aliases,
                graph=graph,
                module_name=module_name,
                is_package=is_package,
            )
            continue
        if not isinstance(node, ast.ClassDef):
            continue
        class_symbol = f'{module_name}.{node.name}'
        method_symbols = {
            item.name: f'{class_symbol}.{item.name}'
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if '__init__' in method_symbols:
            graph.setdefault(class_symbol, set()).add(method_symbols['__init__'])
        class_aliases = dict(module_aliases)
        class_aliases.update(method_symbols)
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            owner = method_symbols[item.name]
            function_aliases = dict(class_aliases)
            positional = list(item.args.posonlyargs) + list(item.args.args)
            if positional:
                function_aliases[positional[0].arg] = class_symbol
            _analyze_static_function(
                item,
                owner=owner,
                base_aliases=function_aliases,
                graph=graph,
                module_name=module_name,
                is_package=is_package,
            )
    return {
        'graph': graph,
        'aliases': aliases,
    }

def _analyze_static_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    owner: str,
    base_aliases: dict[str, str],
    graph: dict[str, set[str]],
    module_name: str,
    is_package: bool,
) -> None:
    aliases = dict(base_aliases)
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults = list(node.args.defaults)
    for argument, default in zip(positional[-len(defaults):], defaults):
        target = _static_reference(default, aliases)
        if target:
            aliases[argument.arg] = target
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is None:
            continue
        target = _static_reference(default, aliases)
        if target:
            aliases[argument.arg] = target
    scanner = _StaticCallScanner(
        owner=owner,
        aliases=aliases,
        graph=graph,
        module_name=module_name,
        is_package=is_package,
    )
    for statement in node.body:
        scanner.visit(statement)

class _StaticCallScanner(ast.NodeVisitor):
    def __init__(
        self,
        *,
        owner: str,
        aliases: dict[str, str],
        graph: dict[str, set[str]],
        module_name: str,
        is_package: bool,
    ) -> None:
        self.owner = owner
        self.aliases = dict(aliases)
        self.graph = graph
        self.module_name = module_name
        self.is_package = is_package

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split('.')[0]
            self.aliases[bound] = alias.name if alias.asname else alias.name.split('.')[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _static_import_base(
            node,
            module_name=self.module_name,
            is_package=self.is_package,
        )
        for alias in node.names:
            if alias.name == '*':
                continue
            target = '.'.join(part for part in (base, alias.name) if part)
            self.aliases[alias.asname or alias.name] = target

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = _static_reference(node.value, self.aliases)
        if not value:
            return
        for target in node.targets:
            name = _static_reference(target, self.aliases)
            if name:
                self.aliases[name] = value
            if isinstance(target, ast.Name):
                self.aliases[target.id] = value

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        value = _static_reference(node.value, self.aliases)
        if not value:
            return
        name = _static_reference(node.target, self.aliases)
        if name:
            self.aliases[name] = value
        if isinstance(node.target, ast.Name):
            self.aliases[node.target.id] = value

    def visit_Call(self, node: ast.Call) -> None:
        target = _static_reference(node.func, self.aliases)
        if target:
            self.graph.setdefault(self.owner, set()).add(target)
        self.generic_visit(node)

def _static_reference(node: ast.AST | None, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _static_reference(node.value, aliases)
        value = f'{parent}.{node.attr}' if parent else node.attr
        return aliases.get(value, value)
    if isinstance(node, ast.Call):
        return _static_reference(node.func, aliases)
    if isinstance(node, ast.Subscript):
        return _static_reference(node.value, aliases)
    return ''

def _static_import_base(
    node: ast.ImportFrom,
    *,
    module_name: str,
    is_package: bool,
) -> str:
    base = str(node.module or '')
    if not node.level:
        return base
    package = module_name if is_package else module_name.rpartition('.')[0]
    package_parts = package.split('.') if package else []
    trim = max(0, node.level - 1)
    if trim:
        package_parts = package_parts[:-trim] if trim <= len(package_parts) else []
    prefix = '.'.join(package_parts)
    return '.'.join(part for part in (prefix, base) if part)

def _canonical_static_symbol(symbol: str, aliases: dict[str, str]) -> str:
    current = str(symbol or '')
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        parts = current.split('.')
        replacement = ''
        suffix: list[str] = []
        for length in range(len(parts), 0, -1):
            prefix = '.'.join(parts[:length])
            target = aliases.get(prefix)
            if target:
                replacement = target
                suffix = parts[length:]
                break
        if not replacement:
            break
        current = '.'.join([replacement, *suffix])
    return current

def _walk_static_calls(
    roots: set[str],
    *,
    graph: dict[str, set[str]],
    aliases: dict[str, str],
) -> set[str]:
    called: set[str] = set()
    pending = list(roots)
    visited: set[str] = set()
    while pending:
        raw_owner = pending.pop()
        owner = _canonical_static_symbol(raw_owner, aliases)
        if owner in visited:
            continue
        visited.add(owner)
        targets = set(graph.get(owner, set()))
        if owner != raw_owner:
            targets.update(graph.get(raw_owner, set()))
        for raw_target in targets:
            target = _canonical_static_symbol(raw_target, aliases)
            if not target:
                continue
            called.add(target)
            if target not in visited:
                pending.append(target)
    return called

def _reachable_local_src_modules(sandbox: Path, roots: set[str]) -> set[str]:
    '''Follow static imports through local Foundation modules under src/.'''

    src_root = sandbox / 'src'
    module_paths: dict[str, Path] = {}
    if src_root.is_dir():
        for path in src_root.rglob('*.py'):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(sandbox).with_suffix('')
            parts = list(relative.parts)
            if parts and parts[-1] == '__init__':
                parts.pop()
            module_name = '.'.join(parts)
            if module_name:
                module_paths[module_name] = path

    reachable = set(roots)
    pending = list(roots)
    visited: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        path = module_paths.get(module_name)
        if path is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        imported = _imports_from_local_module(
            tree,
            module_name=module_name,
            is_package=path.name == '__init__.py',
        )
        for candidate in imported:
            if candidate not in reachable:
                reachable.add(candidate)
                pending.append(candidate)
    return reachable

def _imports_from_local_module(
    tree: ast.AST,
    *,
    module_name: str,
    is_package: bool,
) -> set[str]:
    imported: set[str] = set()
    package = module_name if is_package else module_name.rpartition('.')[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = str(node.module or '')
        if node.level:
            package_parts = package.split('.') if package else []
            trim = max(0, node.level - 1)
            if trim:
                package_parts = package_parts[:-trim] if trim <= len(package_parts) else []
            prefix = '.'.join(package_parts)
            base = '.'.join(value for value in (prefix, base) if value)
        if base:
            imported.add(base)
        for alias in node.names:
            if alias.name != '*':
                imported.add('.'.join(value for value in (base, alias.name) if value))
    return imported

def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
