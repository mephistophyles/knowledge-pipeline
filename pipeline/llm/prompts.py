"""Versioned prompt loading. Prompts live at ``config/prompts/<stage>_<version>.md``
so `prompt_version` in a stage's config selects the exact text, and that same
version string rides into the note's frontmatter (plan §6.5, §7)."""
from __future__ import annotations

from pipeline.config import Settings


def load_prompt(settings: Settings, stage: str, version: str) -> str:
    path = settings.prompts_dir / f"{stage}_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path} (stage={stage}, version={version})")
    return path.read_text(encoding="utf-8")
