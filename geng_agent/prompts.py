from __future__ import annotations

import re
from pathlib import Path

from .security import dependency_policy_prompt_text


PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


class PromptBook:
    def __init__(self, prompt_dir: Path | None = None) -> None:
        self.prompt_dir = prompt_dir or Path(__file__).with_name("prompts")

    def load(self, name: str) -> str:
        path = self.prompt_dir / name
        if not path.exists():
            raise FileNotFoundError(f"提示词模板不存在：{path}")
        return path.read_text(encoding="utf-8")

    def render(self, name: str, **values: str) -> str:
        template = self.load(name)
        values.setdefault("dependency_policy", dependency_policy_prompt_text())

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise KeyError(f"提示词模板缺少变量：{key}")
            return values[key]

        return PLACEHOLDER_RE.sub(replace, template)
