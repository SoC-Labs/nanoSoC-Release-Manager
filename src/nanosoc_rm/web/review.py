"""The review queue of one component's comment findings, one at a time, and the per-user patch basket (Wave C spec
4.5; build-first 5). Reads the stored `quality_finding` (and `readme_check`) facts, the person's decisions in
`gui.sqlite` (web.uistate), and blobs only through `quality.review_text` (local reads, never a fetch).

    stored_fact(config, comp, kind, ref=None, commit=None) -> obs | None   the analysed result to review
    queue(obs) -> [item]                    findings outside the decoration rule (banner, boilerplate), in review
                                            order (suggested edit first), each with a stable key
    key(commit, finding) -> str             "{commit}:{path}:{line}:{kind}" ("#n" on a repeat in one line)
    rank, severity, caution                 from quality.review_text (rules tested there and here)
    state(config, login, comp, obs, i=None) -> dict    the page model: progress, steps, current item, basket
    next_index(items, decisions, after) -> int | None  the next undecided item
    build_patch(config, login, comp, obs) -> (text, problems)   the combined patch of the basket

A decision is accept (the suggested edit), edit (the person's replacement lines; empty means delete only), skip
or dismiss ("not a finding"). The basket is scoped to (login, component, analysed commit): a new analysis starts a
new basket; the old one stays readable with ?commit=.
"""
from __future__ import annotations

from typing import Any

from nanosoc_rm import quality as q
from nanosoc_rm.quality import review_text as rt
from nanosoc_rm.quality.findings import is_bulk
from nanosoc_rm.quality.patch import combine, file_patch
from nanosoc_rm.quality.source import SourceError
from nanosoc_rm.web import uistate

__all__ = ["stored_fact", "queue", "key", "rank", "severity", "caution", "state", "next_index", "build_patch",
           "basket", "commit_of", "STATUS_WORD", "BANNER_FLAG", "readme_rows", "PREVIEW_LINES"]

rank, severity, caution = rt.rank, rt.severity, rt.caution
BANNER_FLAG = "banner"
README_FLAG = "readme:"
PREVIEW_LINES = 40
STEP_WINDOW = 15
STATUS_WORD = {None: "to review", "accept": "in patch", "edit": "edited", "skip": "skipped",
               "dismiss": "not a finding"}
STATUS_TONE = {None: "c-notrun", "accept": "c-ink", "edit": "c-ink", "skip": "c-waived", "dismiss": "c-infra"}
SEVERITY_TONE = {"High": "c-fail", "Medium": "c-warn", "Check": "c-notrun", "Low": "c-neutral"}


def commit_of(obs: dict) -> str:
    f = obs["fact"]
    return (f.get("commit") or "worktree") + ("+worktree" if f.get("worktree") and f.get("commit") else "")


def stored_fact(config: Any, comp: str, kind: str = "comments", ref: str | None = None,
                commit: str | None = None) -> dict | None:
    """The newest stored observation of `kind` for the component; with `commit` (a prefix) or `ref` the newest one
    analysed at that commit or ref. Never analyses (CR-R2: a missing result is a job, not a request)."""
    try:
        hist = q.stored(kind, comp, config=config)
    except Exception:                                            # noqa: BLE001 - no store yet reads as nothing
        return None
    hist = [d for d in hist if d["fact"].get("target") == comp]
    if commit:
        hist = [d for d in hist if (d["fact"].get("commit") or "worktree").startswith(commit.split("+")[0])]
    elif ref:
        hist = [d for d in hist if d["fact"].get("ref") == ref or (d["fact"].get("commit") or "").startswith(ref)]
    return hist[-1] if hist else None


def key(commit: str, f: dict) -> str:
    """The stable key of a finding; queue() adds "#2", "#3" when one line holds several findings of a kind."""
    return "%s:%s:%s:%s" % (commit, f["path"], f["line"], f["kind"])


def queue(obs: dict | None) -> list[dict]:
    """The findings of a stored result outside the decoration rule (CR-WD-Q1), ranked: a suggested edit first,
    then High, Medium, Check, Low; then path and line (CR-WD-Q3)."""
    if obs is None:
        return []
    c = commit_of(obs)
    items, seen = [], {}
    for n, f in enumerate(rank(x for x in obs["fact"].get("findings") or [] if not is_bulk(x))):
        k = key(c, f)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:                       # two findings of one kind on one line (two identifiers): #2, #3...
            k = "%s#%d" % (k, seen[k])
        items.append({"key": k, "n": n, "finding": f, "path": f["path"], "line": f["line"],
                      "kind": f["kind"], "severity": severity(f), "severity_tone": SEVERITY_TONE[severity(f)]})
    return items


def banners(obs: dict | None) -> list[dict]:
    """The findings of the decoration rule (banner and boilerplate, CR-WD-Q1): one basket flag applies them all."""
    return [x for x in (obs or {}).get("fact", {}).get("findings") or [] if is_bulk(x)]


def readme_rows(obs: dict | None) -> list[dict]:
    """readme_check rows other than ok, each with a stable key; layout rows have a mechanical edit (the layout
    patch). The root README first, then the README with most rows."""
    if obs is None:
        return []
    rows = [dict(r, key="%s:%s:%s:%s" % (r.get("readme"), r.get("line"), r.get("kind"), r.get("token")),
                 mechanical=str(r.get("kind") or "").startswith("layout"), severity=severity(r))
            for r in obs["fact"].get("mismatches") or [] if r.get("status") != "ok"]
    per: dict[str, int] = {}
    seen_layout: set[str] = set()
    for r in rows:
        per[r["readme"]] = per.get(r["readme"], 0) + 1
        if r["mechanical"]:                   # one layout section per README: its first layout row carries it
            r["layout_covered"] = r["readme"] in seen_layout
            r["mechanical"] = not r["layout_covered"]
            seen_layout.add(r["readme"])
    rows.sort(key=lambda r: (r["readme"] != "README.md", -per[r["readme"]], r["readme"], int(r.get("line") or 0)))
    return rows


def next_index(items: list[dict], decisions: dict, after: int | None = None) -> int | None:
    """The first undecided item after `after` (wrapping to the start), or None when every item is decided."""
    n = len(items)
    start = 0 if after is None else after + 1
    for k in range(n):
        i = (start + k) % n
        if items[i]["key"] not in decisions:
            return i
    return None


def _source(config: Any, comp: str, obs: dict) -> tuple[Any, str | None]:
    try:
        return rt.local_source(config, comp, obs["fact"]), None
    except (SourceError, OSError) as e:
        return None, str(e)[:200]
    except Exception as e:                                       # noqa: BLE001 - shown, never a 500
        return None, ("the analysed snapshot cannot be read here: %s" % e)[:200]


def _edit_of(item_finding: dict, decision: dict | None) -> dict | None:
    """The edit a decision puts in the patch: accept -> the suggested edit; edit -> the person's lines over the
    finding's lines (empty text deletes them)."""
    if decision is None:
        return None
    if decision["decision"] == "accept":
        return item_finding.get("edit") or None
    if decision["decision"] == "edit":
        text = (decision.get("text") or "").replace("\r\n", "\n")
        if text.endswith("\n"):                     # Enter at the end of the textarea is not a blank line
            text = text[:-1]
        lines = text.split("\n") if text.strip("\n") != "" else []
        ed = item_finding.get("edit") or {}
        start = int(ed.get("start") or item_finding["line"])
        end = int(ed.get("end") or item_finding.get("end_line") or item_finding["line"])
        return {"start": start, "end": end, "replacement": lines}
    return None


def basket(config: Any, login: str, comp: str, obs: dict, items: list[dict] | None = None) -> dict:
    """The person's basket: in-patch items (accepted or edited), the flags, and counts."""
    c = commit_of(obs)
    items = queue(obs) if items is None else items
    dec = uistate.decisions(config, login, comp, c)
    flags = uistate.flags(config, login, comp, c)
    rows = [dict(it, decision=dec[it["key"]]["decision"]) for it in items
            if it["key"] in dec and dec[it["key"]]["decision"] in ("accept", "edit")]
    return {"items": rows, "banner": flags.get(BANNER_FLAG) == "1", "n_banner": len(banners(obs)),
            "readme": sorted(k[len(README_FLAG):] for k, v in flags.items() if k.startswith(README_FLAG) and v == "1"),
            "decisions": dec, "commit": c,
            "file_name": "%s-comments-%s.patch" % (comp, (obs["fact"].get("commit") or "worktree")[:7])}


def build_patch(config: Any, login: str, comp: str, obs: dict, readme_obs: dict | None = None
                ) -> tuple[str, list[str]]:
    """(the combined patch, problems) from stored facts, local blobs and the person's decisions: accepted and
    edited findings (quality.patch.combine per file; overlaps are refused and reported), the banner rule when
    its flag is set, and the README layout section when a layout row is in the basket."""
    b = basket(config, login, comp, obs)
    problems: list[str] = []
    edits: dict[str, list[tuple[dict, str]]] = {}
    for it in b["items"]:
        ed = _edit_of(it["finding"], b["decisions"].get(it["key"]))
        if ed:
            edits.setdefault(it["path"], []).append((ed, "%s:%s %s" % (it["path"], it["line"], it["kind"])))
        else:
            problems.append("%s:%s %s has no mechanical edit; use Edit to give the lines" % (
                it["path"], it["line"], it["kind"]))
    if b["banner"]:
        for f in banners(obs):
            if f.get("edit"):
                edits.setdefault(f["path"], []).append((f["edit"], "%s:%s %s" % (f["path"], f["line"], f["kind"])))
    readmes = []
    if b["readme"] and readme_obs is not None:
        wanted = set(b["readme"])
        readmes = sorted({r["readme"] for r in readme_rows(readme_obs) if r["key"] in wanted and r["mechanical"]})
    if not edits and not readmes:
        return "", problems
    source, why = _source(config, comp, obs)
    if source is None:
        return "", problems + [why or "the analysed snapshot cannot be read here"]
    parts = []
    for path in sorted(edits):
        try:
            raw = source.read(path)
        except (SourceError, OSError):
            raw = None
        if raw is None:
            problems.append("%s: %s (the blob is not on this host; run the quality job)" % (path, rt.NOT_LOCAL))
            continue
        text = raw.decode("utf-8", "replace")
        if "\r\n" in text:
            problems.append("%s has CRLF line ends: edit it by hand" % path)
            continue
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        new, skipped = combine(lines, [e for e, _ in edits[path]])
        for e, label in edits[path]:
            if e in skipped:
                problems.append("%s overlaps another edit of the same lines: not applied" % label)
        parts.append(file_patch(path, text, new))
    if readmes:
        missing = getattr(source, "missing", set())
        blobs = getattr(source, "_blob", {})
        if any(blobs.get(p) in missing for p in blobs if p.rsplit("/", 1)[-1].lower().startswith("readme")):
            problems.append("README layout: some README blobs are not on this host; run the quality job")
        else:
            from nanosoc_rm.quality.readmes import layout_patch
            for name in readmes:
                parts.append(layout_patch(source, readme=name))
    return "".join(p for p in parts if p), problems


def state(config: Any, login: str, comp: str, obs: dict, i: int | None = None) -> dict:
    """Everything the review page shows (templates only loop and print)."""
    items = queue(obs)
    b = basket(config, login, comp, obs, items)
    dec = b["decisions"]
    counts = {"accept": 0, "edit": 0, "skip": 0, "dismiss": 0}
    for it in items:
        d = dec.get(it["key"])
        if d is not None:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    done = sum(counts.values())
    if i is None or not (0 <= i < len(items)):
        i = next_index(items, dec)
    steps = []
    for it in items:
        d = (dec.get(it["key"]) or {}).get("decision")
        steps.append({"n": it["n"], "word": STATUS_WORD[d], "tone": STATUS_TONE[d], "current": it["n"] == i})
    lo = 0 if i is None else max(0, min(i - STEP_WINDOW // 2, len(steps) - STEP_WINDOW))
    cur = None
    if i is not None:
        it = items[i]
        f = it["finding"]
        source, why = _source(config, comp, obs)
        ex = rt.excerpt(source, f["path"], int(f["line"]), int(f.get("end_line") or f["line"]), finding=f)
        all_lines = ex.get("all") if ex else None
        diff, read = rt.edit_lines_for(source, f)
        d = dec.get(it["key"])
        cur = dict(it, excerpt=ex, excerpt_note=None if ex else "%s%s" % (rt.NOT_LOCAL, (": " + why) if why else "."),
                   diff=diff, diff_read=read, caution=caution(f, all_lines), why=f.get("suggestion") or "",
                   evidence=f.get("evidence") or "", decision=(d or {}).get("decision"),
                   status_word=STATUS_WORD[(d or {}).get("decision")],
                   edit_text=_edit_text(diff, ex, d))
    return {"items": items, "total": len(items), "done": done, "in_patch": counts["accept"] + counts["edit"],
            "skipped": counts["skip"], "dismissed": counts["dismiss"], "left": len(items) - done,
            "steps": steps[lo:lo + STEP_WINDOW], "steps_before": lo, "steps_after": max(0, len(steps) - lo - STEP_WINDOW),
            "i": i, "current": cur, "basket": b, "commit": b["commit"]}


def _edit_text(diff: list, ex: dict | None, d: dict | None) -> str:
    """The Edit box's starting text: the person's saved lines; else the suggested replacement; else, for a
    finding with no suggested edit, the flagged lines as they are (so Edit changes them in place)."""
    if d and d["decision"] == "edit":
        return d.get("text") or ""
    if diff:
        return "\n".join(t for s, t in diff if s == "+")
    return "\n".join(t for _, t, flagged in (ex or {}).get("lines") or [] if flagged)


def preview(text: str, n: int = PREVIEW_LINES) -> list[tuple[str, str]]:
    """The first `n` lines of a patch, each with its diff class (ins, del, ctx)."""
    out = []
    for ln in text.split("\n")[:n]:
        cls = "ins" if ln.startswith("+") and not ln.startswith("+++") else \
            "del" if ln.startswith("-") and not ln.startswith("---") else "ctx"
        out.append((cls, ln))
    return out
