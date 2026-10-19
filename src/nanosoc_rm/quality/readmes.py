"""Q3: check each README against the tree at the analysed commit, and generate a "Repository layout" section.

What is checked, per README (every `README*.md`; `docs/*.md` too with include_docs):

- paths: inline code spans and link targets that look like paths (a `/`, a known file extension, a trailing
  `/`), resolved against the README's directory, then the repository root. Paths the repository's .gitignore
  matches are generated outputs and are skipped; so are placeholders (`<x>`), URLs, absolute and home paths.
- make targets: `make [-C DIR] [-f FILE] [VAR=V] TARGET...` in inline code or shell code blocks, against the
  Makefile (with its includes, best-effort) that make would read.
- scripts: a command's program (`./x.sh`, `scripts/x.py`) or the script an interpreter runs (`python3 x.py`,
  `bash x.sh`, `source x`): it must exist, and a `./x` must be executable.
- submodules: `git submodule update --init PATH` and names on a line that mentions "submodule" must be in
  .gitmodules (by name or path).
- flows: for each make target a README names, every script its recipe runs must exist.
- layout: entries of a tree drawing in a layout section must exist; top-level directories the section does not
  mention are reported as undocumented.

A mismatch the reader could not confirm (a Makefile include it could not resolve) is `unverified`, not missing.

Precision (CR-WD-Q4): in a shell block that shows prompts (`$ cmd`), only the prompted lines are commands (the rest
is output); an untagged block is read as commands only on lines that start with a known command or `./`; a
`console` or `shell-session` block only on prompted lines. Never a mismatch: URLs and host names
(`git.example.org/x.git`), branch names on a submodule line, home and absolute paths, virtual environments the
reader creates (`.venv*/`), build outputs (`*.bit`, `*.bin`, `*.xsa`, reports and logs: `ignored`), and link
targets inside a submodule, commit ids on a submodule line, and `NN_name.sh` placeholders. A bare HDL or IP file
name (`x.sv`, `x.flist`, `x.lib`) that is not in the repository's own files is `unverified` when the repository has
submodules (it may be in one, and their trees are not read); a bare name on a submodule line likewise.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex

from nanosoc_rm.quality.makefile import Makefile, expand, read_makefile
from nanosoc_rm.quality.source import Source

__all__ = ["check_readmes", "layout_section", "layout_patch", "readme_files", "MISMATCH_KINDS"]

MISMATCH_KINDS = ("path", "make_target", "script", "not_executable", "submodule", "flow", "layout",
                  "layout_undocumented")
_FILE_EXT = set("py sh bash v sv svh vh c h cc cpp hpp tcl sdc xdc md rst txt yaml yml toml json cfg ini mk f "
                "flist lst csv xml html ld hex bin elf pdf png svg jpg dot gds lef lib db spef sdf vcd fst "
                "do tar gz zip patch diff mem coe sv.j2 j2 ipynb pl rb js ts go rs bit xsa dcp mcs bif ltx rpt log "
                "fsdb xpr cfg ini conf service toml lock".split())
_LAYOUT_HEAD = re.compile(r"^(#{1,6})\s*(repository|repo|directory|folder|project|source)?\s*"
                          r"(layout|structure|tree|organi[sz]ation|contents)\b", re.I)
_SHELL_FENCES = {"", "sh", "bash", "shell", "console", "zsh", "terminal", "shell-session", "cmd"}
_INTERP = {"python", "python3", "python2", "bash", "sh", "zsh", "source", ".", "tclsh", "perl", "ruby", "node",
           "uv", "pytest"}
_PLACEHOLDER = re.compile(r"[<>{}…]|\.\.\.|X{3,}|N{3,}|\$|(^|[/_])(NN|XX)(_|\.|$)")
_TRANSCRIPT_FENCES = {"console", "shell-session", "terminal"}
_PROMPT = re.compile(r"^(\$|%|>|#)\s+")
_COMMAND_START = set("make gmake git cd python python3 python2 bash sh zsh source . pytest pip pip3 uv export env sudo "
                     "cmake ninja vivado vcs xrun verilator tclsh perl nanosoc-rm docker ssh scp rsync curl wget".split())
# A script run by its repo path (`scripts/run_all.sh --all`); a two-space gap or `->` makes it a description list
_REPO_SCRIPT = re.compile(r"^[\w.-]+(/[\w.-]+)+\.(sh|bash|py|tcl|pl|rb)$")
_URL_LIKE = re.compile(r"^[\w-]+(\.[\w-]+)*\.(com|org|net|io|uk|edu|gov|dev|eu|de|fr|ai|co)(/|:|$)", re.I)
_BRANCH_WORDS = {"main", "master", "develop", "dev", "trunk", "HEAD"}
_BUILD_EXT = set("bin bit xsa hwh elf rpt log vcd fst fsdb dcp mcs ltx spef sdf wdb vpd".split())
_SUBMODULE_EXT = set("v sv svh vh vhd vhdl flist f lib lef db".split())     # IP sources a submodule usually holds
_VENV = re.compile(r"^\.?venv([-.\w]*)?(/|$)")


def readme_files(source: Source, include_docs: bool = False) -> list[str]:
    out = []
    for e in source.files():
        name = e.path.rsplit("/", 1)[-1]
        if re.match(r"^readme[^/]*\.md$", name, re.I):
            out.append(e.path)
        elif include_docs and re.match(r"^docs/[^/]+\.md$", e.path):
            out.append(e.path)
    return sorted(out, key=lambda p: (p.count("/"), p))


# ---------------------------------------------------------------------------------------------------------
# .gitignore (generated outputs are not stale paths)
# ---------------------------------------------------------------------------------------------------------
def _gitignore(source: Source) -> list[tuple[str, str]]:
    pats = []
    for e in source.files():
        if e.path.rsplit("/", 1)[-1] != ".gitignore":
            continue
        base = posixpath.dirname(e.path)
        for line in (source.text(e.path) or "").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "!")):
                pats.append((base, line))
    return pats


def _ignored(path: str, pats: list[tuple[str, str]]) -> bool:
    for base, pat in pats:
        if base and not path.startswith(base + "/"):
            continue
        rel = path[len(base) + 1:] if base else path
        p = pat.rstrip("/")
        anchored = "/" in p.lstrip("/")
        p = p.lstrip("/")
        if anchored:
            if fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(rel, p + "/*") or rel.startswith(p + "/"):
                return True
        else:
            parts = rel.split("/")
            if any(fnmatch.fnmatchcase(x, p) for x in parts):
                return True
    return False


# ---------------------------------------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------------------------------------
def _segments(text: str):
    """Yield (line number, kind, payload): kind 'code' (inline span), 'link', 'cmd' (a shell block line),
    'tree' (a tree-drawing line), 'heading' (level, title), 'prose' (the raw line)."""
    fence, fence_lang, prompted = None, "", False
    all_lines = text.split("\n")
    for n, line in enumerate(all_lines, 1):
        st = line.strip()
        m = re.match(r"^(```+|~~~+)\s*([\w+-]*)", st)
        if fence:
            if st.startswith(fence):
                fence = None
                continue
            if re.search(r"[├└│]──|^\s*[|`]--|^\s*\+--", line):
                yield n, "tree", line
            elif fence_lang in _SHELL_FENCES and st:
                cmd = _fence_command(st, fence_lang, prompted)
                if cmd is not None:
                    yield n, "cmd", cmd
            continue
        if m:
            fence, fence_lang = m.group(1), m.group(2).lower()
            prompted = _has_prompts(all_lines, n, fence)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            yield n, "heading", (len(h.group(1)), h.group(2).strip())
        yield n, "prose", line
        for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", line):
            yield n, "code", span.strip()
        for target in re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", line):
            yield n, "link", target


def _has_prompts(lines: list[str], opened_at: int, fence: str) -> bool:
    """The code block opened at line `opened_at` (1-based) shows a prompt (`$ cmd`, `% cmd`) on some line."""
    for line in lines[opened_at:]:
        st = line.strip()
        if st.startswith(fence):
            return False
        if re.match(r"^(\$|%)\s+\S", st):
            return True
    return False


def _fence_command(st: str, lang: str, prompted: bool) -> str | None:
    """The command on a line of a shell code block, or None when the line is output, a comment or prose
    (CR-WD-Q4): with prompts in the block only prompted lines are commands; a transcript block (`console`)
    needs a prompt; an untagged block needs a known command (or `./x`) first."""
    has_prompt = bool(re.match(r"^(\$|%)\s+\S", st))
    if prompted or lang in _TRANSCRIPT_FENCES:
        return _PROMPT.sub("", st, count=1) if has_prompt else None
    if st.startswith("#"):
        return None
    cmd = _PROMPT.sub("", st, count=1)
    if lang == "":
        first = cmd.split(None, 1)[0] if cmd.split() else ""
        script = bool(_REPO_SCRIPT.match(first)) and not re.search(r"\s{2,}|->", cmd)
        if not (first in _COMMAND_START or first.startswith("./") or script or
                re.match(r"^[A-Za-z_]\w*=\S*$", first)):
            return None
    return cmd


# ---------------------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------------------
class _Ctx:
    def __init__(self, source: Source):
        self.source = source
        self.ignore = _gitignore(source)
        self.subs = source.submodules()
        self.gitlinks = source.gitlinks()
        self.makefiles: dict[str, Makefile] = {}
        self.basenames: dict[str, list[str]] = {}
        for e in source.files():
            self.basenames.setdefault(e.path.rsplit("/", 1)[-1], []).append(e.path)

    def makefile(self, path: str) -> Makefile | None:
        if path not in self.makefiles:
            self.makefiles[path] = read_makefile(self.source, path) if self.source.entry(path) else None
        return self.makefiles[path]

    def target_elsewhere(self, target: str, but: str) -> str | None:
        """Another makefile of the repository that defines `target` (README run-directory ambiguity)."""
        for e in self.source.files():
            name = e.path.rsplit("/", 1)[-1]
            if e.path == but or not (name in ("Makefile", "makefile", "GNUmakefile") or name.endswith(".mk")):
                continue
            mk = self.makefile(e.path)
            if mk is not None and target in mk.targets:
                return e.path
        return None

    def resolve(self, token: str, base: str) -> str | None:
        """The tree path `token` names, relative to `base` then the root; None when absent."""
        t = token.split("#", 1)[0].split("?", 1)[0]
        t = t[2:] if t.startswith("./") else t
        for cand in ([posixpath.normpath(posixpath.join(base, t))] if base else []) + [posixpath.normpath(t)]:
            c = cand.rstrip("/")
            if c.startswith(".."):
                continue
            if any(ch in c for ch in "*?["):
                if self.source.glob(c):
                    return c
                continue
            if self.source.exists(c):
                return c
        return None


def _dotslash(p: str) -> str:
    """`p` without leading `./` segments (str.lstrip would also eat the dot of `.venv`)."""
    while p.startswith("./"):
        p = p[2:]
    return p


def _path_like(tok: str) -> bool:
    if not tok or " " in tok or tok.startswith(("-", "/", "~", "http:", "https:", "git@", "mailto:", "#")):
        return False
    if _PLACEHOLDER.search(tok) or "://" in tok or "@" in tok or "=" in tok or tok.count(":") or "," in tok:
        return False
    if _URL_LIKE.match(tok) or _VENV.match(_dotslash(tok)):
        return False                                     # a host name or URL; a venv the reader creates
    if "*" in tok or (tok.startswith(".") and "/" not in tok and tok.count(".") >= 1 and not tok.startswith("./")
                      and len(tok.split(".")) <= 3 and tok.split(".")[1] in _FILE_EXT):
        return False                                     # a glob or a bare extension (`*.lib`, `.pins.xdc`)
    if tok.endswith("/"):
        return True
    name = tok.rstrip("/").rsplit("/", 1)[-1]
    if "." in name and name.rsplit(".", 1)[1] in _FILE_EXT and not re.match(r"^v?\d+(\.\d+)+$", name):
        return True
    return name in ("Makefile", "Dockerfile", "LICENSE", "LICENCE") and "/" in tok


def _first_component_exists(ctx: _Ctx, tok: str, base: str) -> bool:
    first = _dotslash(tok).split("/", 1)[0]
    return bool(first) and (ctx.source.exists(first) or (base and ctx.source.exists(posixpath.join(base, first))))


def _row(readme: str, line: int, kind: str, token: str, status: str, detail: str) -> dict:
    return {"readme": readme, "line": line, "kind": kind, "token": token, "status": status, "detail": detail}


def _check_path(ctx: _Ctx, readme: str, base: str, n: int, tok: str, rows: list[dict], kind: str = "path") -> None:
    hit = ctx.resolve(tok, base)
    if hit is not None:
        rows.append(_row(readme, n, kind, tok, "ok", hit))
        return
    if "/" not in tok.rstrip("/") and ctx.basenames.get(tok):
        rows.append(_row(readme, n, kind, tok, "ok", ctx.basenames[tok][0]))
        return
    norm = posixpath.normpath(posixpath.join(base, _dotslash(tok))) if base else _dotslash(tok)
    if _ignored(norm, ctx.ignore) or _ignored(_dotslash(tok), ctx.ignore):
        rows.append(_row(readme, n, kind, tok, "ignored", "matches .gitignore: a generated output"))
        return
    under_sub = next((g for g in ctx.gitlinks if _dotslash(tok).startswith(g + "/") or
                      norm.startswith(g + "/")), None)
    if under_sub:
        rows.append(_row(readme, n, kind, tok, "ignored", "inside submodule %s (not read)" % under_sub))
        return
    ext = tok.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() if "." in tok.rsplit("/", 1)[-1] else ""
    if ext in _BUILD_EXT:
        rows.append(_row(readme, n, kind, tok, "ignored", "a build output (.%s): built, not committed" % ext))
        return
    if ctx.gitlinks and "/" not in tok and ext in _SUBMODULE_EXT:
        rows.append(_row(readme, n, kind, tok, "unverified",
                         "not in this repository's own files; it may be in a submodule (not read)"))
        return
    rows.append(_row(readme, n, kind, tok, "missing", "no such file or directory at this commit"))


def _split_commands(cmd: str) -> list[list[str]]:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="|&;")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return []
    out, cur = [], []
    for t in toks:
        if t in ("|", "||", "&&", ";", "&", "|&"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _check_command(ctx: _Ctx, readme: str, base: str, n: int, cmd: str, rows: list[dict],
                   named_targets: list[tuple[str, str, Makefile]]) -> None:
    cwd = base
    for argv in _split_commands(cmd):
        while argv and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]) or argv[0] in ("sudo", "env", "time",
                                                                                        "exec", "nohup")):
            argv = argv[1:]
        if not argv:
            continue
        prog = argv[0]
        if prog == "cd" and len(argv) > 1:
            d = ctx.resolve(argv[1], cwd)
            if d is not None and ctx.source.is_dir(d):
                cwd = d
            continue
        if prog == "make" or prog.endswith("/make") or prog == "gmake":
            _check_make(ctx, readme, cwd, n, argv, rows, named_targets)
            continue
        if prog == "git" and argv[1:3] == ["submodule", "update"]:
            for a in argv[3:]:
                if not a.startswith("-") and not _PLACEHOLDER.search(a):
                    _check_submodule(ctx, readme, n, a, rows, claimed=True)
            continue
        if prog in _INTERP:
            script = next((a for a in argv[1:] if not a.startswith("-")), None)
            if prog in ("python", "python3", "python2") and "-m" in argv:
                continue
            if prog in ("uv", "pytest"):
                continue
            if script and not _PLACEHOLDER.search(script) and (("/" in script) or "." in script) \
                    and not script.startswith(("/", "~")) and not _VENV.match(_dotslash(script)):
                _check_script(ctx, readme, cwd, n, script, rows, need_exec=False)
            continue
        if _NOT_PROGRAM.search(prog) and "/" in prog and not _PLACEHOLDER.search(prog):
            _check_path(ctx, readme, cwd, n, prog, rows)  # `cocotb/Makefile regression`: a file, not a program
            continue
        if _looks_like_script(prog) and not _PLACEHOLDER.search(prog) and not prog.startswith(("/", "~")) \
                and not _VENV.match(_dotslash(prog)) and not _URL_LIKE.match(prog):
            _check_script(ctx, readme, cwd, n, prog, rows, need_exec=prog.startswith("./") or "/" in prog)


_NOT_PROGRAM = re.compile(r"(^|/)(GNUmakefile|[Mm]akefile|Dockerfile|CMakeLists\.txt|[^/]+\.mk)$")


def _looks_like_script(prog: str) -> bool:
    name = prog.rsplit("/", 1)[-1]
    if not _plausible_prog(prog):
        return False                                     # `0/0 pads survived`: prose in a code span
    return prog.startswith("./") or bool(re.search(r"\.(sh|bash|py|tcl|pl)$", prog)) or \
        ("/" in prog and "." not in name and bool(name))


def _check_script(ctx: _Ctx, readme: str, base: str, n: int, script: str, rows: list[dict], need_exec: bool) -> None:
    hit = ctx.resolve(script, base)
    if hit is None and "/" not in script and ctx.basenames.get(script):
        hit = ctx.basenames[script][0]
    if hit is None:
        norm = posixpath.normpath(posixpath.join(base, _dotslash(script)))
        if _ignored(norm, ctx.ignore):
            rows.append(_row(readme, n, "script", script, "ignored", "matches .gitignore"))
            return
        if any(norm.startswith(g + "/") for g in ctx.gitlinks):
            rows.append(_row(readme, n, "script", script, "ignored", "inside a submodule (not read)"))
            return
        stem = posixpath.basename(_dotslash(script))
        src = next((c for c in ctx.basenames.get(stem + ".c", []) + ctx.basenames.get(stem + ".cpp", [])
                    if posixpath.dirname(c) == posixpath.dirname(norm)), None)
        if src:
            rows.append(_row(readme, n, "script", script, "unverified",
                             "not in the tree, but %s is: a program the reader builds first" % src))
            return
        d = base
        while d:
            d = posixpath.dirname(d)
            hit = ctx.resolve(script, d) if d else None
            if hit is not None:
                rows.append(_row(readme, n, "script", script, "unverified",
                                 "found as %s, relative to %s/: say where to run it" % (hit, d)))
                return
        rows.append(_row(readme, n, "script", script, "missing", "script not in the tree at this commit"))
        return
    e = ctx.source.entry(hit)
    if need_exec and e is not None and not e.executable:
        rows.append(_row(readme, n, "not_executable", script, "missing",
                         "%s exists but is not executable (mode %s): run it with its interpreter or `git "
                         "update-index --chmod=+x`" % (hit, e.mode)))
        return
    rows.append(_row(readme, n, "script", script, "ok", hit))


def _check_make(ctx: _Ctx, readme: str, cwd: str, n: int, argv: list[str], rows: list[dict],
                named_targets: list[tuple[str, str, Makefile]]) -> None:
    mdir, mfile, targets, i = cwd, None, [], 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--directory") and i + 1 < len(argv):
            d = ctx.resolve(argv[i + 1], cwd)
            mdir = d if d is not None else posixpath.normpath(posixpath.join(cwd, argv[i + 1]))
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            mdir = posixpath.normpath(posixpath.join(cwd, a[2:]))
        elif a in ("-f", "--file", "--makefile") and i + 1 < len(argv):
            mfile = argv[i + 1]
            i += 2
            continue
        elif a in ("-j", "-l") and i + 1 < len(argv) and argv[i + 1].isdigit():
            i += 2
            continue
        elif a.startswith("-") or "=" in a:
            pass
        elif _PLACEHOLDER.search(a):
            pass
        else:
            targets.append(a)
        i += 1
    if not targets:
        return
    path = None
    if mfile:
        path = ctx.resolve(mfile, mdir)
    else:
        for name in ("GNUmakefile", "makefile", "Makefile"):
            c = posixpath.join(mdir, name) if mdir else name
            if ctx.source.entry(c):
                path = c
                break
        d = mdir
        while path is None and d and mdir == cwd:             # the nearest Makefile above the README's directory
            d = posixpath.dirname(d)
            for name in ("GNUmakefile", "makefile", "Makefile"):
                c = posixpath.join(d, name) if d else name
                if ctx.source.entry(c):
                    path = c
                    break
    for t in targets:
        if path is None:
            under = any((mdir or "").startswith(g) for g in ctx.gitlinks)
            rows.append(_row(readme, n, "make_target", t, "ignored" if under else "missing",
                             "inside a submodule (not read)" if under else
                             "no Makefile in %s" % (mdir or "the repository root")))
            continue
        mk = ctx.makefile(path)
        if mk is not None and mk.has(t):
            rows.append(_row(readme, n, "make_target", t, "ok", path))
            named_targets.append((t, path, mk))
        elif mk is not None and mk.unresolved:
            rows.append(_row(readme, n, "make_target", t, "unverified",
                             "not found in %s; %d include(s) or rule(s) could not be read: %s"
                             % (path, len(mk.unresolved), "; ".join(mk.unresolved[:2]))))
        else:
            elsewhere = ctx.target_elsewhere(t, path)
            if elsewhere:
                rows.append(_row(readme, n, "make_target", t, "unverified",
                                 "not in %s, but %s defines it: say where to run it" % (path, elsewhere)))
            else:
                rows.append(_row(readme, n, "make_target", t, "missing", "no rule for %r in %s" % (t, path)))


def _check_submodule(ctx: _Ctx, readme: str, n: int, tok: str, rows: list[dict], claimed: bool = False) -> None:
    """`claimed`: the README says it is a submodule (`git submodule update --init PATH`), not just mentions it
    on a line about submodules."""
    t = _dotslash(tok.strip("/"))
    if _URL_LIKE.match(t) or t in _BRANCH_WORDS or re.fullmatch(r"[0-9a-f]{7,40}", t):
        return                                           # a URL, a branch name or a commit in a submodule row
    if t in ctx.subs or t in ctx.subs.values() or t in ctx.gitlinks or \
            any(p.rsplit("/", 1)[-1] == t for p in list(ctx.subs.values()) + list(ctx.gitlinks)):
        rows.append(_row(readme, n, "submodule", tok, "ok", "in .gitmodules"))
    elif ctx.source.is_dir(t) and claimed:
        rows.append(_row(readme, n, "submodule", tok, "missing", "%s is a plain directory, not a submodule" % t))
    elif ctx.source.is_dir(t):
        # A directory named on a line that mentions submodules ("`a/` is driven by the `b` submodule") is not
        # a claim that it is one: check it as a path.
        rows.append(_row(readme, n, "path", tok, "ok", t))
    elif any(t.startswith(g + "/") for g in ctx.gitlinks):
        rows.append(_row(readme, n, "submodule", tok, "ignored", "inside a submodule (not read)"))
    elif not claimed and ctx.gitlinks and "/" not in t:
        rows.append(_row(readme, n, "submodule", tok, "unverified",
                         "not in .gitmodules; it may be a nested submodule (submodule trees are not read)"))
    else:
        rows.append(_row(readme, n, "submodule", tok, "missing", "not in .gitmodules"))


_PROG = re.compile(r"^[\w.~+-]*[A-Za-z][\w.~+-]*(/[\w.+-]+)*/?$|^(\.{1,2}/)+[\w.+-]+(/[\w.+-]+)*$")


def _plausible_prog(a: str) -> bool:
    """A word that can name a program file: no awk/sed text (`sub(/^[0-9]+[`), no prose (`0/0`)."""
    return bool(_PROG.match(a)) and any(ch.isalpha() for ch in a.rsplit("/", 1)[-1])


def _cd_target(ctx: _Ctx, arg: str, cwd: str | None) -> str | None:
    """The directory a recipe's `cd ARG` enters, or None when it cannot be told (an unexpanded variable that
    is not a prefix of an in-tree path)."""
    if cwd is None:
        return None
    if "$" in arg:
        rest = re.sub(r"^(\$\([^)]*\)|\$\{[^}]*\})+/?", "", arg)      # $(CHIPLET_HOME)/verif/bscan
        if not rest or "$" in rest:
            return None
        for cand in (posixpath.normpath(posixpath.join(cwd, rest)) if cwd else None, posixpath.normpath(rest)):
            if cand and ctx.source.is_dir(cand):
                return cand
        return None
    d = ctx.resolve(arg, cwd)
    return d if d is not None and ctx.source.is_dir(d) else None


def _built_by(mk: Makefile, recipes: list[str]) -> set[str]:
    """Files the makefile makes: every rule target, and `-o NAME` outputs of the recipe lines."""
    out = set(mk.targets)
    for r in recipes:
        out.update(re.findall(r"(?:^|\s)-o\s*([^\s;&|]+)", r))
    return {posixpath.basename(_dotslash(x)) for x in out} | out


def _check_flows(ctx: _Ctx, readme: str, n_of: dict, named: list[tuple[str, str, Makefile]],
                 rows: list[dict]) -> None:
    """For each make target the README names, every program its recipe runs must be in the tree. A recipe's
    `cd DIR` moves the base (for the rest of the line, or the recipe with .ONESHELL); files the makefile builds
    (rule targets, `-o NAME`) and .gitignore'd outputs are not checked."""
    seen = set()
    for target, mpath, mk in named:
        if (target, mpath) in seen:
            continue
        seen.add((target, mpath))
        top = posixpath.dirname(mpath)
        recipes = [expand(r, mk.variables) for r in mk.targets.get(target, [])]
        built = _built_by(mk, recipes)
        cwd: str | None = top
        for line in recipes:
            if not mk.oneshell:
                cwd = top
            for argv in _split_commands(line.lstrip("@-+ ")):
                while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
                    argv = argv[1:]
                if not argv:
                    continue
                if argv[0] == "cd":
                    cwd = _cd_target(ctx, argv[1], cwd) if len(argv) > 1 else top
                    continue
                if argv[0] in _INTERP:
                    a = next((x for x in argv[1:] if not x.startswith("-")), None)
                else:
                    a = argv[0]
                if a is None or "$" in a or _PLACEHOLDER.search(a) or a.startswith(("/", "~")):
                    continue
                if not ("/" in a or re.search(r"\.(sh|py|tcl|pl)$", a)) or not _plausible_prog(a):
                    continue
                if cwd is None:
                    continue                                  # after an unresolvable `cd`: cannot tell
                if a in built or posixpath.basename(_dotslash(a)) in built:
                    continue                                  # the makefile builds it
                hit = ctx.resolve(a, cwd)
                norm = posixpath.normpath(posixpath.join(cwd, _dotslash(a)))
                if hit is None and (_ignored(norm, ctx.ignore) or
                                    any(norm.startswith(g + "/") for g in ctx.gitlinks)):
                    continue                                  # a generated output, or inside a submodule
                if hit is None:
                    rows.append(_row(readme, n_of.get(target, 0), "flow", "make %s" % target, "missing",
                                     "its recipe in %s runs %s, which is not in the tree" % (mpath, a)))
                else:
                    rows.append(_row(readme, n_of.get(target, 0), "flow", "make %s" % target, "ok",
                                     "%s -> %s" % (mpath, hit)))


def _tree_entries(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """(line, path) of each entry of a tree drawing, its depth taken from the column of its branch."""
    out, stack = [], []
    for n, line in lines:
        m = re.search(r"(├──|└──|\|--|`--|\+--)\s*([^\s#]+)", line)
        if not m:
            continue
        depth = m.start(1) // 4
        name = m.group(2).strip("`")
        stack = stack[:depth] + [name.rstrip("/")]
        out.append((n, "/".join(stack)))
    return out


def _check_layout(ctx: _Ctx, readme: str, text: str, rows: list[dict]) -> None:
    sections = _sections(text)
    for level, title, start, end in sections:
        if not _LAYOUT_HEAD.match("#" * level + " " + title):
            continue
        body = text.split("\n")[start:end]
        tree = [(start + i + 1, ln) for i, ln in enumerate(body) if re.search(r"[├└]──|\|--|`--|\+--", ln)]
        for n, path in _tree_entries(tree):
            if _PLACEHOLDER.search(path) or "..." in path:
                continue
            base = posixpath.dirname(readme)
            if ctx.resolve(path, base) is None and not any(path.startswith(g + "/") for g in ctx.gitlinks):
                rows.append(_row(readme, n, "layout", path, "missing", "drawn in the layout, not in the tree"))
        if posixpath.dirname(readme):
            continue
        mentioned = "\n".join(body)
        for d in sorted(x for x in ctx.source.dirs() | ctx.gitlinks if "/" not in x and not x.startswith(".")):
            if not re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(d), mentioned):
                rows.append(_row(readme, start, "layout_undocumented", d + "/", "missing",
                                 "top-level directory not described in the layout section"))


def _sections(text: str) -> list[tuple[int, str, int, int]]:
    """(level, title, first body line index, end line index) of each heading, outside code fences."""
    lines = text.split("\n")
    heads, fence = [], None
    for i, line in enumerate(lines):
        st = line.strip()
        m = re.match(r"^(```+|~~~+)", st)
        if fence:
            if st.startswith(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            heads.append((len(h.group(1)), h.group(2).strip(), i))
    out = []
    for k, (lvl, title, i) in enumerate(heads):
        end = len(lines)
        for lvl2, _, j in heads[k + 1:]:
            if lvl2 <= lvl:
                end = j
                break
        out.append((lvl, title, i + 1, end))
    return out


def _check_one(ctx: _Ctx, readme: str, rows: list[dict]) -> None:
    text = ctx.source.text(readme) or ""
    base = posixpath.dirname(readme)
    named: list[tuple[str, str, Makefile]] = []
    sub_lines = {n for n, kind, payload in _segments(text)
                 if kind == "prose" and re.search(r"\bsub-?modules?\b", payload, re.I)}
    target_line: dict[str, int] = {}

    def command(n: int, cmd: str) -> None:
        before = len(named)
        _check_command(ctx, readme, base, n, cmd, rows, named)
        for t, _, _ in named[before:]:
            target_line.setdefault(t, n)

    for n, kind, payload in _segments(text):
        if kind in ("heading", "prose", "tree"):
            continue
        if kind == "cmd":
            command(n, payload)
        elif kind == "link":
            _check_link(ctx, readme, base, n, payload, rows)
        else:
            _check_token(ctx, readme, base, n, payload, rows, named, command, n in sub_lines)
    _check_flows(ctx, readme, target_line, named, rows)
    _check_layout(ctx, readme, text, rows)


def _check_link(ctx: _Ctx, readme: str, base: str, n: int, payload: str, rows: list[dict]) -> None:
    """A relative Markdown link must name a path in the tree (one inside a submodule is not read)."""
    if payload.startswith(("http:", "https:", "mailto:", "#", "<")) or "://" in payload:
        return
    tgt = payload.split("#", 1)[0]
    if not tgt or _PLACEHOLDER.search(tgt):
        return
    resolved = posixpath.normpath(posixpath.join(base, tgt)) if not tgt.startswith("/") else tgt.lstrip("/")
    hit = ctx.source.exists(resolved)
    sub = next((g for g in ctx.gitlinks if resolved.startswith(g + "/")), None) if not hit else None
    if sub:
        rows.append(_row(readme, n, "path", tgt, "ignored", "inside submodule %s (not read)" % sub))
        return
    rows.append(_row(readme, n, "path", tgt, "ok" if hit else "missing",
                     "link target" if hit else "link target not in the tree at this commit"))


_SUBMODULE_WORDS = ("git", "submodule", "submodules", "init", "update", "recursive")


def _check_token(ctx: _Ctx, readme: str, base: str, n: int, tok: str, rows: list[dict], named: list,
                 command, near_submodules: bool) -> None:
    """An inline code token: a script, a command, a submodule name (on a line about submodules) or a path."""
    if tok.startswith("./") and " " not in tok and _looks_like_script(tok):
        _check_command(ctx, readme, base, n, tok, rows, named)
        return
    if " " in tok.strip():
        words = tok.split()
        if words[0] in ("make", "gmake", "git", "cd") or words[0] in _INTERP or "/" in words[0] \
                or re.search(r"\.(sh|py|tcl)$", words[0]):
            command(n, tok)
        return
    if near_submodules and re.match(r"^[A-Za-z0-9_][\w.-]*(/[\w.-]+)*/?$", tok) and \
            not (("." in tok.rsplit("/", 1)[-1]) and tok.rsplit(".", 1)[-1] in _FILE_EXT) \
            and tok.lower() not in _SUBMODULE_WORDS and not re.match(r"^[A-Z][A-Z0-9_]+$", tok):
        _check_submodule(ctx, readme, n, tok, rows)
        return
    if _path_like(tok) and ("/" not in tok.rstrip("/") or tok.endswith("/") or "." in tok.rsplit("/", 1)[-1]
                            or _first_component_exists(ctx, tok, base)):
        _check_path(ctx, readme, base, n, tok, rows)


def check_readmes(source: Source, *, include_docs: bool = False) -> dict:
    """The readme_check fact (without the envelope): every check, the mismatches, the generated layout."""
    ctx = _Ctx(source)
    rows: list[dict] = []
    readmes = readme_files(source, include_docs)
    for r in readmes:
        _check_one(ctx, r, rows)
    seen, uniq = set(), []
    for r in rows:
        key = (r["readme"], r["line"], r["kind"], r["token"], r["status"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    mismatches = [r for r in uniq if r["status"] == "missing"]
    unverified = [r for r in uniq if r["status"] == "unverified"]
    counts = {k: sum(1 for r in mismatches if r["kind"] == k) for k in MISMATCH_KINDS}
    return {"readmes": readmes, "checked": len(uniq), "ok": sum(1 for r in uniq if r["status"] == "ok"),
            "mismatch_count": len(mismatches), "counts": counts, "mismatches": mismatches,
            "unverified": unverified, "ignored": sum(1 for r in uniq if r["status"] == "ignored"),
            "checks": uniq, "layout": layout_section(source)}


# ---------------------------------------------------------------------------------------------------------
# Generated "Repository layout" section
# ---------------------------------------------------------------------------------------------------------
def _purpose(source: Source, d: str) -> str:
    readme = next((e.path for e in source.files()
                   if posixpath.dirname(e.path) == d and re.match(r"^readme[^/]*\.md$", e.path.rsplit("/", 1)[-1],
                                                                  re.I)), None)
    if readme is None:
        return ""
    name = d.rsplit("/", 1)[-1].lower()
    for line in (source.text(readme) or "").splitlines():
        st = line.strip()
        if not st or st.startswith(("[![", "<", "---", "===", "```", "|")):
            continue
        st = re.sub(r"^#+\s*", "", st)
        st = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", st).replace("`", "").strip()
        if not st or re.sub(r"[\W_]+", "", st.lower()) == re.sub(r"[\W_]+", "", name):
            continue
        return st if len(st) <= 110 else st[:107].rstrip() + "..."
    return ""


_TREE_ROW = re.compile(r"(├──|└──|\|--|`--|\+--)\s*([^\s#]+)\s*(?:#\s*(.*?))?\s*$")
_LIST_ROW = re.compile(r"^\s*[-*+]\s+\[?`([^`]+)`\]?(?:\([^)]*\))?\s*(?:(?::|—|–|-)\s*(.*?))?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|\s*\[?`?([^`|\]]+?)`?\]?(?:\([^)]*\))?\s*\|\s*(.*?)\s*\|")
_GEN_NOTE = "<!-- generated by `nanosoc-rm quality readme`"


def _layout_target(text: str) -> tuple[int, str, int, int] | None:
    return next(((lvl, t, s, e) for lvl, t, s, e in _sections(text) if _LAYOUT_HEAD.match("#" * lvl + " " + t)),
                None)


def _harvest(source: Source, body: list[str], base: str = "") -> tuple[dict[str, str], list[str]]:
    """What a hand-written layout section says, so a regenerated one keeps it (review Q-3): {path: description}
    for each tree row, list item or table row naming an entry, and the prose lines around them."""
    exists = {e.path for e in source.files()} | source.dirs() | source.gitlinks()
    desc: dict[str, str] = {}
    prose: list[str] = []

    def _norm(tok: str) -> str | None:
        tok = tok.strip().strip("`").rstrip("/")
        if not tok or _PLACEHOLDER.search(tok):
            return None
        p = posixpath.normpath(posixpath.join(base, tok)) if base else posixpath.normpath(tok)
        inside = any(p.startswith(g + "/") for g in source.gitlinks())   # a submodule's tree is not read
        return p if p in exists or inside else None

    fence, stack = None, []
    for line in body:
        st = line.strip()
        m = re.match(r"^(```+|~~~+)", st)
        if fence:
            if st.startswith(fence):
                fence = None
                continue
            t = _TREE_ROW.search(line)
            if t:
                depth = t.start(1) // 4
                stack = stack[:depth] + [t.group(2).strip("`").rstrip("/")]
                p = _norm("/".join(stack))
                if p and t.group(3):
                    desc[p] = t.group(3).strip()
            continue
        if m:
            fence, stack = m.group(1), []
            continue
        row = _LIST_ROW.match(line) or _TABLE_ROW.match(line)
        if row and (_norm(row.group(1)) or "/" in row.group(1) or "." in row.group(1)):
            if line.lstrip().startswith("|"):
                while prose and prose[-1].lstrip().startswith("|"):
                    prose.pop()                           # the table's header goes with its rows
            p = _norm(row.group(1))
            if p and row.group(2) and not re.fullmatch(r"[-:\s|]*", row.group(2)):
                desc.setdefault(p, re.sub(r"^submodule$", "", row.group(2)))
            continue
        if re.match(r"^\s*\|[-:\s|]+\|\s*$", line) or st.startswith(_GEN_NOTE):
            continue
        prose.append(line)
    while prose and not prose[0].strip():
        prose.pop(0)
    while prose and not prose[-1].strip():
        prose.pop()
    out, blank = [], False
    for line in prose:                                    # one blank line between kept paragraphs
        if line.strip() or not blank:
            out.append(line)
        blank = not line.strip()
    return {k: v for k, v in desc.items() if v}, out


def layout_section(source: Source, depth: int = 2, readme: str = "README.md") -> dict:
    """{entries: [{path, depth, kind, purpose}], markdown}: the top `depth` levels of directories, plus every entry
    the README's own layout section describes that still exists, with that description (else the first line of
    the directory's README). The section keeps its heading and its prose."""
    dirs = {d for d in source.dirs() if not any(p.startswith(".") for p in d.split("/"))}
    subs = {g for g in source.gitlinks() if not any(p.startswith(".") for p in g.split("/"))}
    title, desc, prose = "Repository layout", {}, []
    text = source.text(readme) if readme else None
    target = _layout_target(text) if text else None
    if target:
        lvl, title, s, e = target
        desc, prose = _harvest(source, text.split("\n")[s:e], posixpath.dirname(readme))
    files = {f.path for f in source.files()}
    paths = {p for p in dirs | subs if p.count("/") + 1 <= depth} | set(desc)
    entries = []
    for p in sorted(paths, key=lambda x: x.split("/")):
        if any(p.startswith(g + "/") for g in source.gitlinks()) and p not in desc:
            continue
        kind = "submodule" if p in source.gitlinks() else "file" if p in files else "dir"
        entries.append({"path": p, "depth": p.count("/") + 1, "kind": kind,
                        "purpose": desc.get(p) or ("" if kind != "dir" else _purpose(source, p))})
    md = ["## " + title, ""] + (prose + [""] if prose else []) + [
          "%s: top %d levels of the tree%s, and the entries this section described; edit the purposes freely -->"
          % (_GEN_NOTE, depth, " at %s" % source.commit[:12] if source.commit else ""), ""]
    for e in entries:
        purpose = e["purpose"] or ("submodule" if e["kind"] == "submodule" else "")
        md.append("%s- `%s%s`%s" % ("  " * (e["depth"] - 1), e["path"], "" if e["kind"] == "file" else "/",
                                     (": " + purpose) if purpose else ""))
    return {"entries": entries, "markdown": "\n".join(md) + "\n"}


def layout_patch(source: Source, readme: str = "README.md") -> str:
    """A patch that regenerates the README's layout section (keeping its heading, prose and the descriptions of
    entries that still exist), or inserts one (before a licence section, else at the end)."""
    from nanosoc_rm.quality.patch import file_patch
    text = source.text(readme)
    if text is None:
        return ""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    new_sec = layout_section(source, readme=readme)["markdown"].rstrip("\n").split("\n")
    sections = _sections(text)
    target = _layout_target(text)
    if target:
        lvl, _, s, e = target
        new_sec[0] = "#" * lvl + new_sec[0].lstrip("#")
        e = min(e, len(lines))
        while e > s and not lines[e - 1].strip():
            e -= 1
        new = lines[:s - 1] + new_sec + lines[e:]
    else:
        lic = next((s for lvl, title, s, e in sections if re.match(r"^licen[cs]e", title, re.I)), None)
        if lic:
            new = lines[:lic - 1] + new_sec + [""] + lines[lic - 1:]
        else:
            new = lines + ([""] if lines and lines[-1].strip() else []) + new_sec
    return file_patch(readme, text, new)
