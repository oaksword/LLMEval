"""Sandbox: temporary directories and task file setup."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Sandbox:
    """A temporary directory sandbox for a single task run."""

    root: Path
    _temp_dir: Any = field(repr=False, default=None)

    @classmethod
    def create(cls) -> "Sandbox":
        """Create a new temporary sandbox directory."""
        td = tempfile.TemporaryDirectory(prefix="llmeval-sandbox-")
        return cls(root=Path(td.name), _temp_dir=td)

    def setup_files(self, files: list[dict]) -> None:
        """Create files and directories from task setup spec.

        Each item: {path: "rel/path", content: "text", is_dir: bool, mode: "644"}
        All paths are validated to stay within the sandbox.
        """
        for f in files:
            rel = f["path"]
            # Validate containment and get the resolved path (strip trailing / for dirs)
            clean_rel = rel.rstrip("/") if rel.endswith("/") else rel
            p = self.resolve(clean_rel)
            if f.get("is_dir") or rel.endswith("/"):
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f.get("content", ""))
                if "mode" in f:
                    import stat
                    p.chmod(int(f["mode"], 8))
            # hidden files on macOS (dotfiles are already hidden)

    def cleanup(self) -> None:
        """Remove the sandbox."""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    def resolve(self, rel: str) -> Path:
        """Resolve a path relative to sandbox root (reject escapes)."""
        p = (self.root / rel).resolve()
        root_resolved = self.root.resolve()
        # Safe containment check — Path.relative_to() avoids prefix collisions
        # (e.g. sandbox-abc vs sandbox-abc2).
        try:
            p.relative_to(root_resolved)
        except ValueError:
            raise ValueError(f"Path escapes sandbox: {rel}")
        return p

    _SNAPSHOT_MAX_BYTES = 1_000_000  # skip files larger than 1 MB

    def snapshot(self) -> dict[str, str]:
        """Return a dict of {relative_path: content} for all files (skips large/binary)."""
        result = {}
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(self.root))
                try:
                    if p.stat().st_size > self._SNAPSHOT_MAX_BYTES:
                        result[rel] = "<too large>"
                    else:
                        result[rel] = p.read_text()
                except Exception:
                    result[rel] = "<binary>"
        return result
