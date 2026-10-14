"""What other Wave B modules read from versioning (wired at the Wave B integration).

    changelog_entries(component, old, new, *, config) -> [{class, summary, commit}]    the releases module (notes.py)
    classify_bump(component, from_commit, to_commit, *, config) -> {class, detail, sources} | None
                                                                                       the bump engine (section 19.6)

Both read the component's mirror only (never a work tree), through the same RepoView as `version next`.

`changelog_entries` lists the classed changelog entries (C2) that `new` brings over `old`: its [Unreleased]
entries and the release sections `old` does not have (old None: every entry up to `new`); an entry outside a
class subsection is `unclassed`. It raises LookupError when it cannot class
the range (an unknown component, no mirror, no CHANGELOG.md at `new`), and the caller falls back to its own reading.

`classify_bump` is the C3 classifier over one pin move: the declared signal (the changelog entries the move adds),
the API.md diff between the two commits (C12), and the consumer regeneration (C3 3/3) with its C7 build signal:
the stored regen_class measurements of exactly this move (`regen_signals`, interfaces.md section 20), else
`unmeasured` (NoGenerator, naming the command that measures it). It returns None when no signal gives a class,
and the bump engine then keeps the changelog's declared class (its own fallback).
"""
from __future__ import annotations

from nanosoc_rm.versioning import changelog as cl
from nanosoc_rm.versioning import classify as cf
from nanosoc_rm.versioning import manifest as mf
from nanosoc_rm.versioning.repo import VersioningError, open_repo

__all__ = ["changelog_entries", "classify_bump", "regen_signals"]


def _view(component: str, config):
    from nanosoc_rm.analysis import _inputs as inp
    try:
        view = open_repo(component, config=config)
    except (VersioningError, inp.UsageProblem, inp.CannotAnswer) as e:
        raise LookupError("cannot read %s: %s" % (component, e)) from None
    if view.git_dir is None:
        raise LookupError("no mirror of %s" % component)
    return view


def _parsed(view, commit: str | None):
    """The parsed CHANGELOG.md at `commit`, or None when it has none."""
    if not commit:
        return None
    text = view.files_at(commit, [cl.FILE])[cl.FILE]
    return None if text is None else cl.parse(text)


def _added(view, old: str | None, new: str) -> list[tuple[str, str]]:
    """[(class, text)] of the entries `new` brings: its [Unreleased] entries and every release section whose
    version is not a release section at `old` (an entry moved from [Unreleased] into a release still counts)."""
    now = _parsed(view, new)
    if now is None:
        raise LookupError("no %s at %s" % (cl.FILE, new[:12]))
    before = _parsed(view, old)
    released = {s.version_text for s in before.releases} if before is not None else set()
    out = []
    for s in now.sections:
        if s.unreleased or s.version_text not in released:
            out += [(e.cls or "unclassed", " ".join(e.text.split())) for e in s.entries]
    return out + [("unclassed", " ".join(e.text.split())) for e in now.loose]


def changelog_entries(component: str, old: str | None, new: str, *, config=None) -> list[dict]:
    """The classed changelog entries `new` adds over `old` (module docstring). LookupError: not classable here."""
    view = _view(component, config)
    return [{"class": c, "summary": t[:200], "commit": new} for c, t in _added(view, old, new)]


def regen_signals(component: str, base: str | None, head: str | None, config, regenerator=None) -> tuple[dict, dict]:
    """The C3 consumers and C7 build signals of a move: a live `regenerator` when the caller passes one, else the
    stored regen_class measurements of exactly this move (nanosoc_rm.regen, section 20), else unmeasured with the
    reason and the command that would measure it."""
    if regenerator is not None:
        return (cf.consumer_signal(component, base, head or "", regenerator),
                cf.build_signal(component, base, head or "", regenerator))
    got = None
    try:
        from nanosoc_rm.regen import stored_signals
        got = stored_signals(component, base, head, config=config)
    except ImportError:                                                # the regenerator is not built here
        got = None
    if got is not None:
        return got
    why = cf.NO_GENERATOR
    if base and head:
        why += ("; no stored regen_class for %s %s..%s (`nanosoc-rm regen class CONFIG --component %s --from %s "
                "--to %s --store` measures it)" % (component, base[:12], head[:12], component, base, head))
    gen = cf.NoGenerator(why)
    return cf.consumer_signal(component, base, head or "", gen), cf.build_signal(component, base, head or "", gen)


def classify_bump(component: str, from_commit: str, to_commit: str, *, config=None) -> dict | None:
    """The C3 class of moving `component` from `from_commit` to `to_commit` (module docstring), or None."""
    try:
        view = _view(component, config)
    except LookupError:
        return None
    try:
        added = _added(view, from_commit, to_commit)
        present = True
    except LookupError:
        added, present = [], False
    classes: dict[str, list] = {}
    for c, t in added:
        classes.setdefault(c, []).append(t)
    api = view.files_at(from_commit, [mf.API_FILE])[mf.API_FILE], view.files_at(to_commit, [mf.API_FILE])[mf.API_FILE]
    signals = [cf.declared_signal(classes, present), cf.api_signal(*api)] + \
        list(regen_signals(component, from_commit, to_commit, view.config))
    klass = cf.classify(signals)
    if klass["class"] is None:
        return None
    detail = "C3 %s: %s" % (klass["status"], "; ".join("%s %s (%s)" % (s["signal"], s["class"] or "-", s["status"])
                                                        for s in signals))
    measured_by = [x for sig in signals for x in sig.get("sources") or [] if sig["signal"] == "consumers"]
    return {"class": klass["class"], "detail": detail[:500], "sources": list(view.sources) + measured_by}
