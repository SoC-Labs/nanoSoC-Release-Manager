"""CLI front ends of the versioning functions (C1-C12, F6, G1, G5, G6). No logic of their own.

    nanosoc-rm changelog lint   [TARGET] [--ref R] [--path DIR] [--store] [--json]            C2, C5
    nanosoc-rm version next     [TARGET] [--ref R] [--path DIR] [--store] [--json]            C1, C3, C6-C9, C12
    nanosoc-rm version tags     [TARGET] [--path DIR] [--store] [--alerts] [--json]           C10
    nanosoc-rm version refresh  [COMPONENT]... [--json]     lint + next + tags, stored, for every (or each) component
    nanosoc-rm version draft    TARGET [--out DIR] [--date YYYY-MM-DD] [--json]               G1, G5: a draft folder
    nanosoc-rm version approve  DRAFT_DIR --step STEP --login LOGIN [--json]                   G5
    nanosoc-rm version yank     TARGET VERSION --reason R --by LOGIN [--patch OUT] [--json]    G6: a registry patch
    nanosoc-rm version fixed-in TARGET [--ref R] [--patch OUT] [--json]                        F6: a registry patch
    nanosoc-rm manifest init    [TARGET] [--ref R] [--path DIR] [--patch OUT] [--json]        C11: a repo patch

Nothing here writes to a repository, a host or the registry: patches go to stdout or a NEW file (`--patch`), a
draft only into a new or empty directory. Exit: 0 answered (PASS, or WARN with its reason), 1 finding or FAIL,
2 could not answer (UNVERIFIED: no mirror, no class at all), 3 usage or config error.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, emit, json_option, subgroup, table

__all__ = ["register", "JOB_KINDS", "ensure_job_kinds"]

_EXIT = {"PASS": EXIT_OK, "WARN": EXIT_OK, "FAIL": EXIT_FINDING, "UNVERIFIED": EXIT_UNVERIFIED}
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}\Z")


def _guard(fn):
    from nanosoc_rm.analysis._inputs import CannotAnswer, UsageProblem
    from nanosoc_rm.versioning import VersioningError, VersioningRefused, VersioningUsage
    try:
        return fn(), None
    except VersioningRefused as e:
        click.echo("nanosoc-rm: REFUSED: %s" % e, err=True)
        return None, EXIT_FINDING
    except (VersioningUsage, UsageProblem) as e:
        click.echo("nanosoc-rm: %s" % e, err=True)
        return None, EXIT_USAGE
    except (VersioningError, CannotAnswer) as e:
        click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
        return None, EXIT_UNVERIFIED


def _write_patch(out: str | None, text: str, what: str) -> None:
    """The patch to a NEW file (never overwritten), or to stdout after the table."""
    if out is None:
        if text.strip():
            click.echo("\n# %s: a patch for a person (review, then `git apply`); nothing was applied\n%s" % (what, text),
                       nl=False)
        return
    p = Path(out)
    if p.exists() or p.is_symlink():
        raise click.UsageError("%s exists: give a new file for the patch" % out)
    if not text.strip():
        click.echo("patch: nothing to patch; %s not written" % out, err=True)
        return
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    click.echo("patch: %s; review it, then `git apply %s` (nothing was applied)" % (out, out), err=True)


def _where(f: dict) -> str:
    return "%s @ %s%s" % (f["target"], (f.get("commit") or "work tree")[:12], " + work tree" if f.get("worktree") and f.get("commit") else "")


def _common(f, ref=True, path=True):
    f = click.option("--store", is_flag=True, help="Store the result (the /versions pages read stored results).")(f)
    if path:
        f = click.option("--path", "path", type=click.Path(file_okay=False), default=None,
                         help="Read a local checkout (read-only) instead of the component's mirror.")(f)
    if ref:
        f = click.option("--ref", default=None, help="Branch, tag or commit (default: the canonical default branch).")(f)
    f = click.argument("target", required=False)(f)
    return json_option(f)


def _need_target(target, path):
    if not target and not path:
        raise click.UsageError("give a TARGET (catalogue component) or --path DIR")


# ---------------------------------------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------------------------------------
def _print_lint(obs: dict) -> None:
    f = obs["fact"]
    click.echo("%s  CHANGELOG.md %s  %s (%d error, %d warning)" % (_where(f), "present" if f["present"] else "MISSING",
                                                                  f["verdict"], f["counts"]["error"], f["counts"]["warning"]))
    if f["unreleased"]:
        click.echo("Unreleased: " + ", ".join("%d %s" % (n, c) for c, n in sorted(f["unreleased"].items())))
    if f["findings"]:
        click.echo(table([{"line": x["line"] or "", "rule": x["rule"], "severity": x["severity"], "message": x["message"],
                           "suggestion": x.get("suggestion") or ""} for x in f["findings"]],
                         ("line", "rule", "severity", "message", "suggestion")))


def _print_plan(obs: dict) -> None:
    f = obs["fact"]
    c, n = f["current"], f["next"]
    click.echo("%s  %s" % (_where(f), f["verdict"]))
    click.echo("current: %s%s" % ("%s (%s, %s)" % (c["tag"], c["version"], c["kind"]) if c["tag"] else "no release tag",
                                  "; AMBIGUOUS baseline: a person confirms it" if c.get("ambiguous") else ""))
    click.echo("commits since: %s" % (f["commits_since"] if f["commits_since"] is not None else "?"))
    for cls in ("breaking", "feature", "fix", "none", "unclassed"):
        if f["unreleased"].get(cls):
            click.echo("unreleased %s: %d" % (cls, len(f["unreleased"][cls])))
    k = f["classification"]
    click.echo("class: %s (%s)%s" % (k["class"] or "unmeasured", k["status"], ": " + k["reason"] if k.get("reason") else ""))
    click.echo(table([{"signal": s["signal"], "class": s["class"] or "", "status": s["status"], "detail": s["detail"]}
                      for s in k["signals"]], ("signal", "class", "status", "detail")))
    h = f["hint"]
    click.echo("hint (commit messages, never used): %s over %d commit(s)" % (h["class"] or "none", h["commits"]))
    click.echo("next: %s (%s)" % (n["version"] or "none", n["reason"]))
    click.echo("manifest: %s; API.md: %s; changelog: %s" % (
        "valid" if f["manifest"]["valid"] else ("invalid" if f["manifest"]["present"] else "missing"),
        "present" if f["api_md"] else "missing",
        f["changelog"]["verdict"] if f["changelog"]["present"] else "missing"))
    for x in f["notes"]:
        click.echo("note: %s" % x)
    for x in f["findings"]:
        click.echo("FINDING: %s" % x)


def _print_tags(obs: dict) -> None:
    f = obs["fact"]
    click.echo("%s  %s  scheme %s; %d finding(s)" % (f["target"], f["verdict"],
                                                    "from %s" % f["scheme_from"] if f["scheme_from"] else
                                                    "not adopted (every existing tag grandfathered; advisory)",
                                                    f["findings"]))
    click.echo(table([{"tag": r["tag"], "kind": r["kind"] or "outside", "grandfathered": "yes" if r["grandfathered"] else "",
                       "moved": "MOVED" if r["moved"] else "", "finding": "FINDING" if r["finding"] else "",
                       "problems": "; ".join(r["problems"])} for r in f["rows"]],
                     ("tag", "kind", "grandfathered", "moved", "finding", "problems")))
    for b in f["branches"]:
        if not b["ok"]:
            click.echo("FINDING: branch %s on %s is not release/X.Y (C10)" % (b["branch"], b["repo"]))


# ---------------------------------------------------------------------------------------------------------
# Job kinds (the GUI queues these; jobs.register_kind)
# ---------------------------------------------------------------------------------------------------------
def _check_refresh(params: dict) -> dict:
    comps = [c for c in (params.get("components") or []) if str(c).strip()]
    for c in comps:
        if not _ID.match(str(c)):
            raise ValueError("%r is not a component id" % str(c)[:60])
    return {"components": [str(c) for c in comps]}


def _check_draft(params: dict) -> dict:
    c = params.get("component")
    if not isinstance(c, str) or not _ID.match(c):
        raise ValueError("%r is not a component id" % str(c)[:60])
    return {"component": c}


JOB_KINDS = {
    "versions-refresh": (_check_refresh, lambda p: ["version", "refresh"] + list(p["components"])),
    "version-draft": (_check_draft, lambda p: ["version", "draft", p["component"]]),
}


def ensure_job_kinds() -> None:
    """Register this module's job kinds with the local job runner (idempotent)."""
    from nanosoc_rm import jobs
    for name, (check, args) in JOB_KINDS.items():
        jobs.register_kind(name, check, args)


def register(main: click.Group) -> None:
    ensure_job_kinds()

    changelog = subgroup(main, "changelog", help="The classed changelog (C2): lint it.")
    version = subgroup(main, "version", help="Versions and change classes (C1-C12), tool releases (G1, G5, G6), F6.")
    manifest = subgroup(main, "manifest", help="The component manifest nanosoc-component.yaml (C11).")

    @changelog.command("lint")
    @_common
    def lint_cmd(target, ref, path, store, as_json):
        """C2: lint CHANGELOG.md (format, classes, Unreleased, links, tags; C5 rule). Exit 1 on an error."""
        _need_target(target, path)
        from nanosoc_rm import versioning
        obs, rc = _guard(lambda: versioning.changelog_lint(target, ref=ref, path=path, store=store))
        if rc is not None:
            return rc
        if as_json:
            emit([obs], True)
        else:
            _print_lint(obs)
        return _EXIT[obs["fact"]["verdict"]]

    @version.command("next")
    @_common
    def next_cmd(target, ref, path, store, as_json):
        """C1, C3: the next version from the classed changelog and the measured signals. Exit 0 PASS or WARN
        (a partial class: some signal unmeasured), 1 a finding, 2 no class at all."""
        _need_target(target, path)
        from nanosoc_rm import versioning
        obs, rc = _guard(lambda: versioning.plan(target, ref=ref, path=path, store=store))
        if rc is not None:
            return rc
        if as_json:
            emit([obs], True)
        else:
            _print_plan(obs)
        return _EXIT[obs["fact"]["verdict"]]

    @version.command("tags")
    @click.option("--alerts", is_flag=True, help="Raise a tag_moved alert for each moved release tag.")
    @_common
    def tags_cmd(target, path, store, alerts, as_json, ref=None):
        """C10: every tag on every host by kind; names outside the scheme, lightweight or non-canonical release
        tags, and moved release tags (from the mirror's reflog). Exit 1 on a finding."""
        _need_target(target, path)
        from nanosoc_rm import versioning
        obs, rc = _guard(lambda: versioning.tag_check(target, path=path, store=store, alerts=alerts))
        if rc is not None:
            return rc
        if as_json:
            emit([obs], True)
        else:
            _print_tags(obs)
        return _EXIT[obs["fact"]["verdict"]]

    @version.command("refresh")
    @click.argument("components", nargs=-1)
    @json_option
    def refresh_cmd(components, as_json):
        """Lint, next and tags for every catalogue component (or those named), stored for the /versions pages."""
        from nanosoc_rm import versioning
        from nanosoc_rm.analysis import _inputs as inp
        from nanosoc_rm.config import load_config
        cfg = load_config()
        reg, rc = _guard(lambda: inp.registry(cfg))
        if rc is not None:
            return rc
        ids = list(components) or [c["id"] for c in reg.catalogue["components"] if c.get("class") != "legacy"]
        docs, worst = [], EXIT_OK
        for cid in ids:
            mirror = inp.mirror_path(cfg, cid)
            if not (mirror / "HEAD").is_file():
                click.echo("%s: no mirror (run `nanosoc-rm mirror sync --component %s`)" % (cid, cid), err=True)
                worst = max(worst, EXIT_UNVERIFIED)
                continue
            for fn in (versioning.changelog_lint, versioning.plan, versioning.tag_check):
                obs, rc = _guard(lambda: fn(cid, store=True))
                if rc is not None:
                    worst = max(worst, rc if rc != EXIT_USAGE else EXIT_UNVERIFIED)
                    continue
                docs.append(obs)
                if not as_json:
                    click.echo("%-28s %-15s %s" % (cid, obs["kind"], obs["fact"]["verdict"]))
        if as_json:
            emit(docs, True)
        return worst if worst != EXIT_FINDING else EXIT_OK

    @version.command("draft")
    @click.argument("target")
    @click.option("--out", "out", type=click.Path(file_okay=False), default=None,
                  help="A new or empty directory (default <data_dir>/drafts/<component>/<version>).")
    @click.option("--date", default=None, help="Release date YYYY-MM-DD (default today, UTC).")
    @json_option
    def draft_cmd(target, out, date, as_json):
        """G1: write a release draft folder (version bump patch, classed changelog, submodules, defects, gates,
        the G5 approval record) for a person. Nothing is tagged or pushed."""
        from nanosoc_rm import versioning
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise click.UsageError("--date must be YYYY-MM-DD")
        obs, rc = _guard(lambda: versioning.draft(target, out, date=date))
        if rc is not None:
            return rc
        if as_json:
            emit([obs], True)
        else:
            f = obs["fact"]
            click.echo("draft: %s %s (class %s, %s) in %s" % (f["target"], f["version"], f["class"], f["class_status"], f["dir"]))
            click.echo("files: %s. Nothing was tagged or pushed; read README.md, then commands.txt." % ", ".join(f["files"]))
        return EXIT_OK

    @version.command("approve")
    @click.argument("draft_dir", type=click.Path(file_okay=False, exists=True))
    @click.option("--step", required=True, help="tool-candidate, tool-final or tool-final-breaking.")
    @click.option("--login", required=True, help="The approving person's login (roles from the service config).")
    @click.option("--password-stdin", "pw_stdin", is_flag=True,
                  help="Read the login's local password from stdin instead of prompting.")
    @json_option
    def approve_cmd(draft_dir, step, login, pw_stdin, as_json):
        """G5: record a person's approval of one step in the draft's release.json (roles enforced). The login is
        proven by its local password (local sign-in, `[[auth.users]]`); with GitHub sign-in, approve in the GUI
        (/versions/<component>), where the signed-in login is recorded."""
        import sys
        from nanosoc_rm import versioning
        from nanosoc_rm.auth import local_users
        from nanosoc_rm.config import ConfigError, load_config
        try:
            cfg = load_config()
        except ConfigError as e:
            click.echo("nanosoc-rm: %s" % e, err=True)
            return EXIT_USAGE
        if (cfg.get("auth") or {}).get("mode") != "local" or not local_users(cfg):
            click.echo("nanosoc-rm: the CLI proves an approver by a local password, and this service has no local "
                       "users: approve in the GUI (/versions/<component>, signed in), which records your login", err=True)
            return EXIT_USAGE
        if pw_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = click.prompt("password for %s" % login, hide_input=True, default="", show_default=False)
        st, rc = _guard(lambda: versioning.approve(draft_dir, step, login, config=cfg, password=password))
        if rc is not None:
            return rc
        if as_json:
            click.echo(json.dumps(st, separators=(",", ":")))
        else:
            click.echo("%s: %s (%s)" % (step, st["state"], ", ".join("%s by %s" % (a["role"], a["by"]) for a in st["approvals"])))
        return EXIT_OK

    @version.command("yank")
    @click.argument("target")
    @click.argument("version_")
    @click.option("--reason", required=True)
    @click.option("--by", "by", required=True, help="Who decided the yank.")
    @click.option("--patch", "patch_out", default=None, help="Write the registry patch to this NEW file.")
    @json_option
    def yank_cmd(target, version_, reason, by, patch_out, as_json):
        """G6: propose yanking a tool release (recorded in the registry's versioning.yaml, never bumped to again,
        tag kept). Refused for a silicon release (exit 1)."""
        from nanosoc_rm import versioning
        res, rc = _guard(lambda: versioning.yank(target, version_, reason, by))
        if rc is not None:
            return rc
        obs, patch = res
        f = obs["fact"]
        if as_json:
            emit([obs], True)
        else:
            click.echo("yank %s %s: %s" % (f["target"], f["version"], ("REFUSED: " + f["refused"]) if f["refused"]
                                          else "proposed (tag %s kept)" % f["tag"]))
        if not f["refused"]:
            _write_patch(patch_out, patch, "registry versioning.yaml")
        return EXIT_FINDING if f["refused"] else EXIT_OK

    @version.command("fixed-in")
    @click.argument("target")
    @click.option("--ref", default=None)
    @click.option("--patch", "patch_out", default=None, help="Write the registry patch to this NEW file.")
    @json_option
    def fixed_in_cmd(target, ref, patch_out, as_json):
        """F6: Defect:/Fixes: trailers on the default branch -> fixed_in entries, as a registry patch. Exit 1
        when a defect record needs updating, 2 when a trailer could not be resolved and none needs updating."""
        from nanosoc_rm import versioning
        res, rc = _guard(lambda: versioning.defect_trailers(target, ref=ref))
        if rc is not None:
            return rc
        obs, patch = res
        f = obs["fact"]
        if as_json:
            emit([obs], True)
        else:
            click.echo("%s: %d proposed, %d already recorded, %d unresolved" % (_where(f), f["proposed"], f["already"],
                                                                                f["unresolved"]))
            if f["rows"]:
                click.echo(table([{"commit": r["commit"][:12], "trailer": r["trailer"], "defect": r["defect"] or "",
                                   "form": r["form"] or "", "state": r["state"], "why": r.get("why") or ""}
                                  for r in f["rows"]], ("commit", "trailer", "defect", "form", "state", "why")))
        _write_patch(patch_out, patch, "registry defects")
        return EXIT_FINDING if f["proposed"] else (EXIT_UNVERIFIED if f["unresolved"] else EXIT_OK)

    @manifest.command("init")
    @click.option("--patch", "patch_out", default=None, help="Write the patch to this NEW file.")
    @_common
    def init_cmd(target, ref, path, store, patch_out, as_json):
        """C11: propose nanosoc-component.yaml from the catalogue and the tree, as a patch for a person."""
        _need_target(target, path)
        from nanosoc_rm import versioning
        res, rc = _guard(lambda: versioning.manifest_init(target, ref=ref, path=path))
        if rc is not None:
            return rc
        obs, patch = res
        f = obs["fact"]
        if as_json:
            emit([obs], True)
        else:
            click.echo("%s: manifest %s; proposed version %s (from %s); %d placeholder(s) to fill: %s" % (
                _where(f), "present (the patch replaces it)" if f["present"] else "missing", f["proposed"]["version"],
                f["version_from"], len(f["placeholders"]), ", ".join(f["placeholders"])))
        _write_patch(patch_out, patch, "nanosoc-component.yaml")
        return EXIT_OK
