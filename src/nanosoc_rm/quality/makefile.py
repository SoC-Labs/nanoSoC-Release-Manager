"""A best-effort Makefile reader for the README checks (Q3): targets, pattern rules, recipes and includes.

It expands simple variables (`=`, `:=`, `?=`, `+=`) in target names, recipes and include lines. Anything it
cannot resolve (an include naming a function, a target made by `$(eval ...)`) is recorded in `unresolved`, and
a target it cannot find is then reported as unverified rather than missing.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field

__all__ = ["Makefile", "read_makefile", "expand"]

_ASSIGN = re.compile(r"^(?:override\s+|export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*(:::=|::=|:=|\?=|\+=|!=|=)\s*(.*)$")
_RULE = re.compile(r"^([^\t#=][^=]*?[^:=\s]|[^\t#:=\s])\s*(::?)(?!=)(.*)$")
_INCLUDE = re.compile(r"^\s*(-?include|sinclude)\s+(.+)$")
_VAR = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_.-]*)\)|\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


@dataclass
class Makefile:
    path: str
    targets: dict[str, list[str]] = field(default_factory=dict)      # target -> recipe lines
    patterns: list[str] = field(default_factory=list)                 # `%.o` rules
    variables: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)                    # every makefile read (path, includes)
    unresolved: list[str] = field(default_factory=list)               # includes or rules it could not resolve

    def has(self, target: str) -> bool:
        if target in self.targets:
            return True
        return any(fnmatch.fnmatchcase(target, p.replace("%", "*")) for p in self.patterns)


def expand(text: str, variables: dict[str, str], depth: int = 0) -> str:
    if depth > 8 or "$" not in text:
        return text

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name in variables:
            return expand(variables[name], variables, depth + 1)
        if name == "CURDIR":
            return "."
        return m.group(0)
    return _VAR.sub(sub, text)


def _logical_lines(text: str) -> list[str]:
    out, buf = [], ""
    for line in text.split("\n"):
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def read_makefile(source, path: str, mk: Makefile | None = None, depth: int = 0) -> Makefile:
    """Read `path` (and what it includes, relative to the top makefile's directory) from `source`."""
    top = mk is None
    mk = mk or Makefile(path)
    text = source.text(path)
    if text is None:
        mk.unresolved.append("%s: not found" % path)
        return mk
    mk.files.append(path)
    base = posixpath.dirname(mk.path)
    current: list[str] = []
    for line in _logical_lines(text):
        if line.startswith("\t"):
            if current:
                for t in current:
                    mk.targets.setdefault(t, []).append(line[1:])
            continue
        stripped = line.split("#", 1)[0].rstrip() if "#" in line and "\\#" not in line else line.rstrip()
        if not stripped.strip():
            continue
        m = _INCLUDE.match(stripped)
        if m:
            current = []
            for word in expand(m.group(2), mk.variables).split():
                if "$" in word:
                    mk.unresolved.append("include %s" % word)
                    continue
                inc = posixpath.normpath(posixpath.join(base, word))
                hits = source.glob(inc) if any(c in inc for c in "*?[") else [inc]
                for h in hits:
                    if source.entry(h) is None:
                        if m.group(1) == "include":
                            mk.unresolved.append("include %s: not in the tree" % word)
                        continue
                    if h not in mk.files and depth < 6:
                        read_makefile(source, h, mk, depth + 1)
            continue
        if re.match(r"^\s*(ifeq|ifneq|ifdef|ifndef|else|endif|define|endef|export|unexport|vpath)\b", stripped):
            if stripped.lstrip().startswith("define"):
                mk.unresolved.append("define block in %s" % path)
            continue
        if "$(eval" in stripped or ("$(call" in stripped and ":" not in stripped):
            mk.unresolved.append("generated rule in %s" % path)
            current = []
            continue
        a = _ASSIGN.match(stripped.strip())
        if a:
            name, op, value = a.group(1), a.group(2), a.group(3).strip()
            if op == "?=" and name in mk.variables:
                continue
            if op == "+=":
                mk.variables[name] = (mk.variables.get(name, "") + " " + value).strip()
            elif op != "!=":
                mk.variables[name] = value
            current = []
            continue
        r = _RULE.match(stripped.strip())
        if r:
            names = expand(r.group(1), mk.variables).split()
            current = []
            for n in names:
                if "$" in n:
                    mk.unresolved.append("target %s in %s" % (n, path))
                    continue
                if n == ".PHONY":
                    for p in expand(r.group(3), mk.variables).split():
                        mk.targets.setdefault(p, [])
                    continue
                if n.startswith(".") and n.isupper():
                    continue
                if "%" in n:
                    mk.patterns.append(n)
                else:
                    mk.targets.setdefault(n, [])
                current.append(n)
            rest = r.group(3)
            if ";" in rest:
                for t in current:
                    mk.targets.setdefault(t, []).append(rest.split(";", 1)[1].strip())
            continue
        current = []
    if top:
        mk.variables.setdefault("MAKE", "make")
    return mk
