"""Bump proposals, SHADOW MODE ONLY (Phase 4: E1-E7; built per CR-15, enabled only by the Phase 4 gate).

interfaces.md section 18. Every function returns contract documents (observations validated by
nanosoc_rm.contracts); the CLI (`nanosoc-rm bumps`) and the `/bumps` pages are front ends over them.

    proposals(configuration=None, *, config=None, store=False) -> Answer of bump_scan + bump_proposal observations
                                                                        (store=True also keeps the ledger)
    stored(configuration=None, *, config=None) -> [bump_proposal]      the queue as last computed and kept, one
                                                                        document per proposal and configuration
    get(proposal_id, *, configuration=None, config=None) -> bump_proposal   BumpUsage when unknown
    stored_ledger(*, config=None) -> (bump_ledger | None, computed_at)  what the pages show (no git in a request)
    patch_text(proposal_fact) -> str                                    the patch a person applies
    mark(proposal_id, login, decision, note=None, *, config=None)       "done" | "declined", logged with the login
    ledger(*, config=None) -> bump_ledger                               E2 agreement against what people did
    schedule_status() -> dict                                           always disabled, naming the Phase 4 gate

What it never does: open a pull request, push, write a branch or tag, or call a host's write API. There is no
bump writer credential (H3); a configured one is reported as a problem and never read. Frozen and silicon
configurations are never proposed (E3, E5), and auto_merge stays empty (E2).
"""
from __future__ import annotations

from pathlib import Path

from nanosoc_rm.analysis._inputs import Answer, CannotAnswer, config_of
from nanosoc_rm.bumps import store as _store
from nanosoc_rm.bumps.engine import BumpUsage, proposal_id, scan, scan_all, schedule_status, writer_problems
from nanosoc_rm.bumps.ledger import ledger
from nanosoc_rm.bumps.patchfile import commit_message, patch_text, person_commands
from nanosoc_rm.config import Config

__all__ = ["proposals", "stored", "stored_ledger", "store_ledger", "scans", "get", "patch_text", "commit_message", "person_commands", "mark", "marks",
           "ledger", "schedule_status", "writer_problems", "proposal_id", "BumpUsage", "CannotAnswer", "DECISIONS"]

DECISIONS = _store.DECISIONS


def proposals(configuration: str | None = None, *, config: Config | str | Path | None = None,
              store: bool = False) -> Answer:
    """Scan one configuration (or every registered one) now: the bump_scan observations first, then the
    bump_proposal observations deepest first. `store=True` keeps them for the queue page and the ledger."""
    cfg = config_of(config)
    if configuration:
        s, p = scan(configuration, config=cfg, store_result=store)
        ans = Answer([s] + p)
    else:
        scans_, props, unanswered = scan_all(config=cfg, store_result=store)
        ans = Answer(scans_ + props, unanswered)
    if store:
        store_ledger(config=cfg)
    return ans


def store_ledger(*, config: Config | str | Path | None = None) -> dict:
    """Compute the shadow ledger (mirror reads) and keep it for the pages. Runs in the CLI or a job only."""
    cfg = config_of(config)
    obs = ledger(config=cfg)
    _store.record_ledger(cfg, obs)
    return obs


def stored_ledger(*, config: Config | str | Path | None = None) -> tuple[dict | None, str | None]:
    """(the kept bump_ledger, when it was computed): what the pages show; never reads a mirror."""
    return _store.ledger(config_of(config))


def stored(configuration: str | None = None, *, config: Config | str | Path | None = None) -> list[dict]:
    """The kept proposals, oldest first: one document per proposal and configuration it was computed for (that
    configuration's tree paths, order and matrix pins); only that configuration's when named."""
    cfg = config_of(config)
    out = []
    for r in _store.proposals(cfg):
        for cid, d in sorted(r["docs"].items()):
            if not configuration or cid == configuration:
                out.append(d)
    return out


def scans(*, config: Config | str | Path | None = None) -> list[dict]:
    return _store.scans(config_of(config))


def get(pid: str, *, configuration: str | None = None, config: Config | str | Path | None = None) -> dict:
    """One kept proposal: that configuration's document when named (BumpUsage when it was never computed for it),
    else the latest computed."""
    r = _store.proposal(config_of(config), pid)
    if r is None:
        raise BumpUsage("no proposal %r is kept: run `nanosoc-rm bumps CONFIG --store` first" % pid)
    if configuration:
        if configuration not in r["docs"]:
            raise BumpUsage("proposal %r was not computed for %s (it was for %s)"
                            % (pid, configuration, ", ".join(sorted(r["docs"]))))
        return r["docs"][configuration]
    return r["doc"]


def mark(pid: str, login: str, decision: str, note: str | None = None, *,
         config: Config | str | Path | None = None) -> dict:
    """A person's word on a proposal: they applied it ("done") or will not ("declined"). Written only to the
    service's own bumps.sqlite, with the login (H2). The ledger still judges by the mirror, not by the mark."""
    cfg = config_of(config)
    try:
        return _store.mark(cfg, pid, login, decision, note)
    except KeyError:
        raise BumpUsage("no proposal %r is kept" % pid) from None
    except ValueError as e:
        raise BumpUsage(str(e)) from None


def marks(pid: str | None = None, *, config: Config | str | Path | None = None) -> list[dict]:
    return _store.marks(config_of(config), pid)
