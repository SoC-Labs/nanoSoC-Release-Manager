"""The patch a person applies for one proposal (E1, shadow mode; interfaces.md section 19.3).

The file is a header of plain text (the proposal, the suggested commit message and the commands for a person),
then one `diff --git` that moves the gitlink. `git apply` ignores the text before the first `diff --git`.

    git -C <consumer checkout> apply --index <id>.diff     # updates the gitlink in the index
    git -C <consumer checkout> commit -F <the message below>

`--index` is needed: a gitlink lives only in the index, so a plain `git apply` with the submodule not checked out
changes nothing. No generated RTL is regenerated (REGEN_NOTE). The tool writes this text; it never applies it,
commits, pushes or opens a pull request.
"""
from __future__ import annotations

from nanosoc_rm.gitquote import quote_path

__all__ = ["patch_text", "commit_message", "person_commands"]


def _ver(ref: dict) -> str:
    if not ref["tag"]:
        return "no release"
    return ref["tag"] if ref.get("exact", True) else "contains %s" % ref["tag"]


def commit_message(f: dict) -> str:
    """The commit message a person may use (first line under 72 characters where the names allow)."""
    lines = ["Bump %s to %s" % (f["dependency"]["component"], f["to"]["tag"] or f["to"]["commit"][:12]), ""]
    lines.append("Moves %s from %s (%s) to %s (%s)." % (f["path"], f["from"]["commit"][:12], _ver(f["from"]),
                                                        f["to"]["commit"][:12], _ver(f["to"])))
    ch = f["change"]
    lines.append("Change class: %s (%s; %s)." % (ch["class"], ch["class_source"], ch["class_detail"]))
    lines.append("Commits: %d%s." % (ch["commits"]["count"], ", DIVERGED: the old pin is not an ancestor"
                                     if ch["diverged"] else ""))
    for d in f["defects"]["fixed"]:
        lines.append("Fixes %s (%s)%s." % (d["id"], d["title"], (" forms " + ", ".join(d["forms"])) if d.get("forms") else ""))
    for d in f["defects"]["introduced"]:
        lines.append("INTRODUCES %s (%s)." % (d["id"], d["title"]))
    m = f["matrix"]
    lines.append("Matrix: %s." % ("%s (%s)" % (m["verdict"], m["source"]) if m["verdict"] else "no stored result"))
    lines.append("Generated RTL: not regenerated (needs the generator).")
    lines += ["", "Proposed-by: nanosoc-rm bumps, shadow mode (%s)" % f["id"]]
    return "\n".join(lines)


def person_commands(f: dict) -> list[str]:
    """What a person runs, in a checkout of the consumer repo. Text only: the tool runs none of it."""
    c = f["consumer"]
    branch = "bump/%s-%s" % (f["dependency"]["component"], f["to"]["tag"] or f["to"]["commit"][:12])
    return [
        "git fetch origin %s" % c["branch"],
        "git switch -c %s %s" % (branch, c["base"]),
        "git apply --index %s" % f["patch"]["file"],
        "git commit    # with the suggested message below",
        "git submodule update --init -- %s" % f["path"],
        "# regenerate build_soc if this repo commits generated RTL, then run its T1/T2",
        "git push origin %s   # then open a pull request into %s" % (branch, c["branch"]),
    ]


def patch_text(f: dict) -> str:
    """The patch file for one bump_proposal fact."""
    head = [
        "nanosoc-rm bump proposal %s (SHADOW MODE: the tool never applies, commits, pushes or opens a pull request)"
        % f["id"],
        "",
        "Consumer: %s %s:%s, branch %s at %s" % (f["consumer"]["component"], f["consumer"]["repo"]["host"],
                                                 f["consumer"]["repo"]["path"], f["consumer"]["branch"],
                                                 f["consumer"]["base"]),
        "Gitlink:  %s  %s -> %s" % (f["path"], f["from"]["commit"], f["to"]["commit"]),
        "Note:     %s" % f["patch"]["note"],
    ]
    if f["state"] != "proposed":
        head.append("BLOCKED:  %s" % f["reason"])
    head += ["", "Commands for a person, in a checkout of the consumer repo:"]
    head += ["    " + c for c in person_commands(f)]
    head += ["", "Suggested commit message:", ""]
    head += ["    " + ln if ln else "" for ln in commit_message(f).splitlines()]
    head.append("")
    p, a, b = f["path"], f["from"]["commit"], f["to"]["commit"]
    qa, qb = quote_path("a/" + p), quote_path("b/" + p)          # git's C quoting (ROB-property-13)
    diff = ["diff --git %s %s" % (qa, qb), "index %s..%s 160000" % (a, b), "--- %s" % qa, "+++ %s" % qb,
            "@@ -1 +1 @@", "-Subproject commit %s" % a, "+Subproject commit %s" % b]
    return "\n".join(head + diff) + "\n"
