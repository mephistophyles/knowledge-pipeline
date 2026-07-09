"""Vault writer — the vault is a git repo; deltas are commits (plan §5, §8).

Writes frontmatter+markdown notes under the corpus/personal/hubs tree and
commits per batch. Generated notes are never hand-edited; human commentary lives
in sibling notes under personal/ (plan invariant 4).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import frontmatter

# Directory layout inside the vault (plan §2).
SUBDIRS = [
    "corpus/sources",
    "corpus/claims",
    "corpus/entities",
    "personal/commentary",
    "personal/experiences",
    "hubs",
]


class VaultWriter:
    def __init__(self, vault_dir: str | Path):
        self.root = Path(vault_dir)

    def ensure_layout(self) -> None:
        """Create the vault dir tree and initialise it as a git repo if needed."""
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
            gk = self.root / sub / ".gitkeep"
            if not gk.exists():
                gk.write_text("")
        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "[init] vault layout", _allow_empty=True)

    def write_note(self, relpath: str, metadata: dict, body: str) -> Path:
        """Write one note (frontmatter + markdown). Returns its path."""
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(body, **metadata)
        path.write_bytes(frontmatter.dumps(post).encode() + b"\n")
        return path

    def commit(self, message: str) -> bool:
        """Stage everything and commit. Returns True if a commit was made."""
        self._git("add", "-A")
        # Nothing staged → skip (avoids empty commits on no-op reruns).
        if self._git("diff", "--cached", "--quiet", _check=False).returncode == 0:
            return False
        self._git("commit", "-q", "-m", message)
        return True

    def _git(self, *args: str, _check: bool = True, _allow_empty: bool = False):
        cmd = ["git", "-C", str(self.root), *args]
        if _allow_empty and args and args[0] == "commit":
            cmd.insert(cmd.index("commit") + 1, "--allow-empty")
        return subprocess.run(cmd, check=_check, capture_output=True, text=True)


def read_note(path: str | Path) -> frontmatter.Post:
    return frontmatter.load(str(path))
