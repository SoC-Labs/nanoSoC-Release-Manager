"""CLI front ends of the quality functions (Q1-Q6, CR-14). No logic of their own.

    nanosoc-rm quality density  [TARGET] [--ref R] [--path DIR] [--thresholds FILE] [--top N] [--store] [--json]
    nanosoc-rm quality comments [TARGET] [--ref R] [--path DIR] [--kind K]... [--patch OUT] [--all] [--store] [--json]
    nanosoc-rm quality readme   [TARGET] [--ref R] [--path DIR] [--docs] [--patch OUT] [--store] [--json]
    nanosoc-rm quality docs     [TARGET] [--ref R] [--path DIR] [--rewrite] [--patch OUT] [--store] [--json]
    nanosoc-rm quality all      [TARGET] [--ref R] [--path DIR] [--store]           every report, one line each
    nanosoc-rm quality trend    KIND TARGET [--json]                                 stored results, oldest first

TARGET: a catalogue component (read from its mirror; --ref a branch, tag or sha), a `[quality] paths` name, or a
label for --path. --json: density prints one row per file, comments one finding per line, readme one check per
line, docs one file per line, trend one point per line. --patch writes a unified diff for a person to apply with
`git apply`; the tools never edit a repository. Exit: 0 clean, 1 findings (outliers, findings, mismatches, a
doc under 60), 2 could not answer, 3 usage or config error.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import click

from nanosoc_rm.cliutil import EXIT_FINDING, EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE, json_option, subgroup, table

DOC_FLOOR = 60.0


def _common(f):
    f = click.option("--store", is_flag=True, help="Store the result per (target, commit) for trends (Q5).")(f)
    f = click.option("--path", "path", type=click.Path(file_okay=False), default=None,
                     help="Read a local directory (read-only) instead of a mirror.")(f)
    f = click.option("--ref", default=None, help="Branch, tag or commit (default: the default branch; with "
                     "--path: the work tree as it is).")(f)
    f = click.argument("target", required=False)(f)
    return json_option(f)


def _command() -> str:
    return "nanosoc-rm " + " ".join(shlex.quote(a) for a in sys.argv[1:]) if sys.argv[1:] else "nanosoc-rm quality"


def _guard(fn):
    from nanosoc_rm import quality
    from nanosoc_rm.quality.settings import QualityConfigError
    try:
        return fn(), None
    except (quality.UsageError, QualityConfigError) as e:
        click.echo("nanosoc-rm: %s" % e, err=True)
        return None, EXIT_USAGE
    except (quality.QualityError, quality.SourceError) as e:
        click.echo("nanosoc-rm: could not answer: %s" % e, err=True)
        return None, EXIT_UNVERIFIED


def _write_patch(out: str, text: str) -> None:
    p = Path(out)
    if p.exists() or p.is_symlink():
        raise click.UsageError("%s exists: give a new file for the patch" % out)
    if not text.strip():
        click.echo("patch: nothing to patch (no mechanical edits); %s not written" % out, err=True)
        return
    try:                                                  # O_EXCL|O_NOFOLLOW: no race onto a link or a file
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        raise click.UsageError("%s exists: give a new file for the patch" % out) from None
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    n = sum(1 for ln in text.splitlines() if ln.startswith("diff --git "))
    click.echo("patch: %s (%d file%s); review it, then `git apply %s`" % (out, n, "" if n == 1 else "s", out),
               err=True)


def _head(obs: dict) -> str:
    f = obs["fact"]
    where = (f["commit"] or "no commit")[:12] + (" + work tree" if f["worktree"] else "")
    return "%s @ %s%s" % (f["target"], where, (" (ref %s)" % f["ref"]) if f.get("ref") else "")


def register(main: click.Group) -> None:
    grp = subgroup(main, "quality", help="Code and documentation quality: comment density, comment problems, "
                                         "README accuracy, doc conciseness (Q1-Q6). Read-only; patches only.")

    @grp.command("density")
    @_common
    @click.option("--thresholds", type=click.Path(dir_okay=False, exists=True), default=None,
                  help="A quality.yaml to use instead of the registry's.")
    @click.option("--top", default=10, show_default=True, help="Outlier files to list.")
    def density_cmd(target, ref, path, store, as_json, thresholds, top):
        """Q1: comment ratio per language, directory and file; outliers against per-language thresholds."""
        from nanosoc_rm import quality
        obs, err = _guard(lambda: quality.density(target, ref=ref, path=path, store=store, thresholds=thresholds,
                                                  command=_command()))
        if err is not None:
            return err
        f = obs["fact"]
        if as_json:
            for r in f["files"]:
                click.echo(json.dumps(r, separators=(",", ":")))
        else:
            t = f["totals"]
            click.echo("%s: %d files, code %d, comment %d, docstring %d, blank %d, ratio %.3f  (thresholds: %s)"
                       % (_head(obs), t["files"], t["code"], t["comment"], t["docstring"], t["blank"], t["ratio"],
                          f["thresholds"]["origin"]))
            click.echo(table([dict(lg, flag="OVER" if lg["over"] else "") for lg in f["languages"]],
                             ("language", "files", "code", "comment", "docstring", "ratio", "max_ratio", "outliers",
                              "flag")))
            rows = {r["path"]: r for r in f["files"]}
            if f["outliers"]:
                click.echo("\ntop outlier files (comment lines over the threshold):")
                click.echo(table([rows[p] for p in f["outliers"][:top]],
                                 ("path", "language", "code", "comment", "docstring", "ratio", "max_ratio",
                                  "excess_lines")))
            d = f["docs"]
            click.echo("\ndocs (Markdown, not in the ratios): %d files, %d prose lines, %d words"
                       % (d["files"], d["prose"], d["words"]))
        return EXIT_FINDING if f["outliers"] else EXIT_OK

    @grp.command("comments")
    @_common
    @click.option("--kind", "kinds", multiple=True, help="Only these finding kinds (repeatable).")
    @click.option("--patch", "patch_out", default=None, help="Write the mechanical edits as a unified diff.")
    @click.option("--all", "show_all", is_flag=True, help="List every finding (default: the first 60).")
    def comments_cmd(target, ref, path, store, as_json, kinds, patch_out, show_all):
        """Q2: commented-out code, restating comments, banners, boilerplate, stale TODOs, unknown names."""
        from nanosoc_rm import quality
        from nanosoc_rm.quality.findings import KINDS
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            click.echo("nanosoc-rm: unknown kind %s; kinds: %s" % (", ".join(bad), ", ".join(KINDS)), err=True)
            return EXIT_USAGE
        res, err = _guard(lambda: quality.comments(target, ref=ref, path=path, store=store, command=_command(),
                                                   with_source=True))
        if err is not None:
            return err
        obs, src = res
        f = obs["fact"]
        found = [x for x in f["findings"] if not kinds or x["kind"] in kinds]
        if as_json:
            for x in found:
                click.echo(json.dumps(x, separators=(",", ":")))
        else:
            click.echo("%s: %d findings  %s" % (_head(obs), len(found),
                                                 "  ".join("%s=%d" % kv for kv in f["counts"].items())))
            shown = found if show_all else found[:60]
            click.echo(table([{"where": "%s:%d" % (x["path"], x["line"]), "kind": x["kind"],
                               "edit": "yes" if x["edit"] else "", "evidence": x["evidence"][:70],
                               "suggestion": x["suggestion"][:80]} for x in shown],
                             ("where", "kind", "edit", "evidence", "suggestion")))
            if len(shown) < len(found):
                click.echo("... %d more (--all, or --json)" % (len(found) - len(shown)))
        if patch_out:
            text, skipped = quality.comment_patch(src, {"fact": {"findings": found}})
            _write_patch(patch_out, text)
            if skipped:
                click.echo("%d edit(s) not in the patch (overlapping or CRLF)" % len(skipped), err=True)
        return EXIT_FINDING if found else EXIT_OK

    @grp.command("readme")
    @_common
    @click.option("--docs", "include_docs", is_flag=True, help="Also check docs/*.md.")
    @click.option("--patch", "patch_out", default=None,
                  help="Write a patch that puts the generated Repository layout section in README.md.")
    def readme_cmd(target, ref, path, store, as_json, include_docs, patch_out):
        """Q3: every path, make target, script, submodule and flow the READMEs name, against the tree."""
        from nanosoc_rm import quality
        from nanosoc_rm.quality.readmes import layout_patch
        res, err = _guard(lambda: quality.readme(target, ref=ref, path=path, store=store, include_docs=include_docs,
                                                 command=_command(), with_source=True))
        if err is not None:
            return err
        obs, src = res
        f = obs["fact"]
        if as_json:
            for r in f["checks"]:
                click.echo(json.dumps(r, separators=(",", ":")))
        else:
            click.echo("%s: %d README(s), %d checks, %d ok, %d mismatches, %d unverified, %d ignored"
                       % (_head(obs), len(f["readmes"]), f["checked"], f["ok"], f["mismatch_count"],
                          len(f["unverified"]), f["ignored"]))
            if f["mismatches"]:
                click.echo(table([{"where": "%s:%d" % (r["readme"], r["line"]), "kind": r["kind"], "names": r["token"],
                                   "detail": r["detail"][:90]} for r in f["mismatches"]],
                                 ("where", "kind", "names", "detail")))
            click.echo("\nGenerated layout section (from the tree):\n")
            click.echo(f["layout"]["markdown"])
        if patch_out:
            _write_patch(patch_out, layout_patch(src))
        return EXIT_FINDING if f["mismatch_count"] else EXIT_OK

    @grp.command("docs")
    @_common
    @click.option("--rewrite", is_flag=True, help="Ask Claude for shorter section rewrites (needs an API key; "
                  "sends section text to the Anthropic API).")
    @click.option("--patch", "patch_out", default=None, help="With --rewrite: write the accepted rewrites as a diff.")
    @click.option("--top", default=15, show_default=True, help="Files to list, least concise first.")
    def docs_cmd(target, ref, path, store, as_json, rewrite, patch_out, top):
        """Q4: conciseness score per Markdown file and section; optional Claude rewrite as a patch."""
        from nanosoc_rm import quality
        res, err = _guard(lambda: quality.docs(target, ref=ref, path=path, store=store, command=_command(),
                                               with_source=True))
        if err is not None:
            return err
        obs, src = res
        f = obs["fact"]
        if as_json:
            for r in f["files"]:
                click.echo(json.dumps({k: v for k, v in r.items() if k != "sections"}, separators=(",", ":")))
        else:
            if not f["words"]:
                click.echo("%s: no Markdown words to score (%d files): nothing measured"
                           % (_head(obs), len(f["files"])))
            else:
                click.echo("%s: %d Markdown files, %d words, score %.1f (word-weighted; 100 = concise)"
                           % (_head(obs), len(f["files"]), f["words"], f["score"]))
            click.echo(table(f["files"][:top], ("path", "score", "words", "mean_sentence", "filler_per_100",
                                                "passive_share", "repeated", "duplicated")))
            if f["duplicates"]:
                click.echo("\n%d paragraph(s) duplicated across files, e.g. %s = %s"
                           % (len(f["duplicates"]), f["duplicates"][0]["a"], f["duplicates"][0]["b"]))
        if rewrite:
            from nanosoc_rm.quality.rewrite import rewrite_sections
            rw = rewrite_sections(src, f)
            if not rw["enabled"]:
                click.echo("rewrite: off (%s); no network call was made" % rw["reason"], err=True)
            else:
                click.echo("rewrite (%s): %d proposed, %d rejected" % (rw["model"], len(rw["proposals"]),
                                                                       len(rw["rejected"])), err=True)
                for r in rw["rejected"]:
                    click.echo("  rejected %s:%d %s: %s" % (r["path"], r["line"], r["title"], r["reason"]), err=True)
                if patch_out and rw["patch"]:
                    _write_patch(patch_out, rw["patch"])
        elif patch_out:
            click.echo("nanosoc-rm: --patch needs --rewrite for docs", err=True)
            return EXIT_USAGE
        return EXIT_FINDING if any(r["score"] < DOC_FLOOR for r in f["files"]) else EXIT_OK

    @grp.command("all")
    @_common
    def all_cmd(target, ref, path, store, as_json):
        """Every quality report for one target (and store them with --store): one line each."""
        from nanosoc_rm import quality
        code = EXIT_OK
        for kind in ("density", "comments", "readme", "docs"):
            obs, err = _guard(lambda: getattr(quality, kind)(target, ref=ref, path=path, store=store,
                                                             command=_command()))
            if err is not None:
                return err
            f = obs["fact"]
            found = {"density": lambda: f["outliers"], "comments": lambda: f["total"],
                     "readme": lambda: f["mismatch_count"],
                     "docs": lambda: any(r["score"] < DOC_FLOOR for r in f["files"])}[kind]()
            if found:
                code = EXIT_FINDING                       # the exit each single command gives (interfaces 14a)
            if as_json:
                click.echo(json.dumps(obs, separators=(",", ":")))
                continue
            line = {"density": lambda: "comment ratio %.3f, %d outlier files" % (f["totals"]["ratio"], len(f["outliers"])),
                    "comments": lambda: "%d findings %s" % (f["total"], f["counts"]),
                    "readme": lambda: "%d mismatches in %d README(s)" % (f["mismatch_count"], len(f["readmes"])),
                    "docs": lambda: ("doc score %.1f over %d files" % (f["score"], len(f["files"])) if f["words"]
                                     else "no Markdown words to score: nothing measured")}[kind]()
            click.echo("%-9s %s: %s%s" % (kind, _head(obs), line, "  [stored]" if store else ""))
        return code

    @grp.command("trend")
    @click.argument("kind", type=click.Choice(["density", "comments", "readme", "docs"]))
    @click.argument("target")
    @json_option
    def trend_cmd(kind, target, as_json):
        """Q5: the stored results of one target, oldest first."""
        from nanosoc_rm import quality
        rows, err = _guard(lambda: quality.trend(kind, target))
        if err is not None:
            return err
        if as_json:
            for r in rows:
                click.echo(json.dumps(r, separators=(",", ":")))
        else:
            click.echo(table([dict(r, commit=(r["commit"] or "")[:12]) for r in rows],
                             ("observed_at", "commit", "worktree", "value")))
        return EXIT_OK if rows else EXIT_UNVERIFIED


__all__ = ["register", "DOC_FLOOR"]
