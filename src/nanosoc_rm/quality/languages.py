"""Which language a file is, and the default comment-density thresholds (Q1).

Thresholds come from the registry's optional `quality.yaml` (eligibility.schema.json `$defs/quality_file`),
merged over these defaults; a component entry there overrides the top level for that component.
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

__all__ = ["language_of", "CODE_LANGUAGES", "DOC_LANGUAGES", "DEFAULTS", "is_excluded"]

_EXT = {
    ".py": "python", ".pyi": "python",
    ".v": "verilog", ".sv": "verilog", ".svh": "verilog", ".vh": "verilog", ".svi": "verilog",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c", ".hxx": "c",
    ".scala": "c", ".java": "c", ".js": "c", ".ts": "c", ".rs": "c", ".go": "c",
    ".tcl": "tcl", ".sdc": "tcl", ".xdc": "tcl", ".do": "tcl",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell",
    ".mk": "make", ".mak": "make",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown", ".markdown": "markdown",
}
_NAMES = {"Makefile": "make", "makefile": "make", "GNUmakefile": "make", "Makefile.inc": "make",
          "Dockerfile": "shell", ".bashrc": "shell", ".gitlab-ci.yml": "yaml"}
_SHEBANG = (("python", "python"), ("bash", "shell"), ("/sh", "shell"), ("env sh", "shell"), ("zsh", "shell"),
            ("tclsh", "tcl"), ("wish", "tcl"), ("make -f", "make"))

CODE_LANGUAGES = ("python", "verilog", "c", "tcl", "shell", "make", "yaml", "toml")
DOC_LANGUAGES = ("markdown",)

# Comment ratio = (comment + docstring lines) / (code + comment + docstring lines).
# A file is an outlier when its ratio exceeds max_ratio and it has at least min_lines non-blank lines.
DEFAULTS: dict = {
    "defaults": {"max_ratio": 0.35, "min_lines": 20},
    "languages": {
        "python": {"max_ratio": 0.40},
        "verilog": {"max_ratio": 0.40},
        "c": {"max_ratio": 0.40},
        "tcl": {"max_ratio": 0.35},
        "shell": {"max_ratio": 0.35},
        "make": {"max_ratio": 0.35},
        "yaml": {"max_ratio": 0.30},
        "toml": {"max_ratio": 0.30},
    },
    "exclude": [],
    "max_file_bytes": 1048576,
    "stale_todo_days": 180,
}


def language_of(path: str, head: bytes | None = None) -> str | None:
    """The language key of a file, from its name, extension, or (for extensionless files) its #! line."""
    p = PurePosixPath(path)
    if p.name in _NAMES:
        return _NAMES[p.name]
    if p.name.startswith("Makefile") or p.name.endswith(".make"):
        return "make"
    ext = p.suffix.lower()
    if ext in _EXT:
        return _EXT[ext]
    if not ext and head and head.startswith(b"#!"):
        first = head.split(b"\n", 1)[0].decode("utf-8", "replace")
        for needle, lang in _SHEBANG:
            if needle in first:
                return lang
    return None


def is_excluded(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(path, pat.rstrip("/") + "/*"):
            return True
        if pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:]):
            return True
    return False
