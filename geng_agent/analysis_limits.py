from __future__ import annotations

DEFAULT_ANALYSIS_AGENT_WIDTH = 2
MAX_ANALYSIS_AGENT_WIDTH = 8


def normalize_analysis_agent_width(value: int | None) -> int:
    width = max(1, int(value or 1))
    if width > MAX_ANALYSIS_AGENT_WIDTH:
        raise ValueError(
            f"analysis_agent_width must be between 1 and {MAX_ANALYSIS_AGENT_WIDTH}, got {width}"
        )
    return width
