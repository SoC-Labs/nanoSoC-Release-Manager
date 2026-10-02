"""What each page and JSON route shows, as functions over the store (interfaces.md section 13; PR4).

Every function here reads observations (db.latest and the registry records) and returns plain dicts and
lists of observation documents. Nothing here computes a fact: facts come from the module functions that
produced and stored them (walker, analysis, ci, mirror, credentials, registry). The two exceptions call those
module functions through their contracted names: `containment` runs `analysis.contains.contains` for a query
nothing has answered yet, and `run_t0` calls `ci.trigger.trigger`, the Run button's function.

A module function that fails never turns a page into a 500: the page shows what is stored and says what could
not be done (`problems`).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from nanosoc_rm import clock, db
from nanosoc_rm.config import Config
from nanosoc_rm.web import registry_facts

__all__ = ["RED_STATES", "DRIFT_RELATIONS", "configurations", "configuration_record", "is_frozen", "pins",
           "red_pins", "runs", "latest_run", "pending_runs", "host_compares", "drift", "runner_settings",
           "errata", "open_errata", "defects", "dashboard", "configuration_page", "component_page",
           "containment", "silicon_protection", "run_by_nonce", "fact_by_subject", "mirrors", "inventory",
           "run_t0", "RunPending", "is_valid_id"]

# README section 5: red states. LOCAL_URL and NO_GITLINK are pin-level (fact.state); the rest per host.
RED_STATES = {"LOCAL_URL", "NO_GITLINK", "UNREADABLE", "REWRITTEN", "PR_ONLY", "ORPHAN", "MISSING"}
PIN_LEVEL_RED = {"LOCAL_URL", "NO_GITLINK"}
# Section 13 "host drift": AHEAD, BEHIND, DIVERGED or UNRELATED. UNKNOWN is shown as unknown, not as drift.
DRIFT_RELATIONS = {"AHEAD", "BEHIND", "DIVERGED", "UNRELATED"}
_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.@-]{0,127}$")
_SHA_PREFIX = re.compile(r"^[0-9a-f]{4,40}$")


def is_valid_id(s: str) -> bool:
    return bool(_ID.match(s or ""))


def _index(d: dict) -> int:
    m = re.search(r"/(\d+)$", d["fact"]["pointer"])
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------------------------------------
# Registry records
# ---------------------------------------------------------------------------------------------------------
def configurations(config: Config) -> list[dict]:
    """registry_record observations of the registered configurations, in eligibility.yaml order."""
    return sorted(registry_facts.current(config, "configuration"), key=_index)


def configuration_record(config: Config, cid: str) -> dict | None:
    return next((d for d in configurations(config) if d["fact"]["id"] == cid), None)


def is_frozen(record: dict | None) -> bool:
    """A frozen or silicon configuration (BRANCH_ONLY is red in its tree, README section 5)."""
    rec = (record or {}).get("fact", {}).get("record", {})
    return bool(rec.get("frozen") or rec.get("silicon"))


def errata(config: Config, configuration: str | None = None) -> list[dict]:
    docs = registry_facts.current(config, "erratum")
    docs = [d for d in docs if configuration is None or d["fact"]["record"].get("configuration") == configuration]
    return sorted(docs, key=lambda d: d["fact"]["id"])


OPEN_ERRATUM_STATUSES = ("open", "permanent")        # section 13: the dashboard's "open errata"


def open_errata(config: Config, configuration: str) -> list[dict]:
    """Errata with status open or permanent (section 13 dashboard)."""
    return [d for d in errata(config, configuration) if d["fact"]["record"].get("status") in OPEN_ERRATUM_STATUSES]


def defects(config: Config) -> list[dict]:
    return sorted(registry_facts.current(config, "defect"), key=lambda d: d["fact"]["id"])


def components(config: Config) -> list[dict]:
    return sorted(registry_facts.current(config, "component"), key=_index)


# ---------------------------------------------------------------------------------------------------------
# Stored facts
# ---------------------------------------------------------------------------------------------------------
def pins(config: Config, cid: str) -> list[dict]:
    """The configuration's latest walked tree (pin_state, one per pin), in tree-path order."""
    docs = db.latest("pin_state", "pin_state:%s:" % cid, config=config)
    return sorted(docs, key=lambda d: d["fact"]["tree_paths"][0])


def pinned_host(pin: dict) -> tuple[str | None, dict | None]:
    for name, h in sorted((pin["fact"].get("hosts") or {}).items()):
        if h.get("pinned"):
            return name, h
    return None, None


def pin_is_red(pin: dict, frozen: bool) -> bool:
    """Red on its pinned host (README section 5), or a pin-level red state. BRANCH_ONLY is red in a frozen or
    silicon tree."""
    f = pin["fact"]
    if f.get("state") in PIN_LEVEL_RED:
        return True
    _, h = pinned_host(pin)
    if h is None:
        return False
    return h["state"] in RED_STATES or (h["state"] == "BRANCH_ONLY" and frozen)


def red_pins(pin_docs: Iterable[dict], frozen: bool) -> list[dict]:
    return [p for p in pin_docs if pin_is_red(p, frozen)]


def _triggered(d: dict) -> tuple:
    return (d["fact"]["run"]["triggered_at"], d["observed_at"])


def runs(config: Config, cid: str | None = None) -> list[dict]:
    """t0_result observations (the latest per run), newest trigger first."""
    docs = db.latest("t0_result", "t0_result:%s:" % cid if cid else "t0_result:", config=config)
    return sorted(docs, key=_triggered, reverse=True)


def latest_run(run_docs: list[dict]) -> dict | None:
    return max(run_docs, key=_triggered) if run_docs else None


def pending_runs(run_docs: Iterable[dict]) -> list[dict]:
    return [d for d in run_docs if d["fact"]["acceptance"] == "pending"]


def host_compares(config: Config, component: str | None = None) -> list[dict]:
    docs = db.latest("host_compare", "host_compare:%s:" % component if component else "host_compare:", config=config)
    return sorted(docs, key=lambda d: d["subject"])


def drift(compares: Iterable[dict], tree_components: set[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """(drifting, unknown, in-tree) host_compare rows of the components in a tree (section 13 host drift)."""
    mine = [d for d in compares if d["fact"]["component"] in tree_components]
    return ([d for d in mine if d["fact"]["relation"] in DRIFT_RELATIONS],
            [d for d in mine if d["fact"]["relation"] == "UNKNOWN"], mine)


def runner_settings(config: Config) -> list[dict]:
    """The latest runner_settings per runner; the allowed CI runners first (ci.allowed_runner_ids)."""
    docs = db.latest("runner_settings", config=config)
    allowed = set((config.get("ci") or {}).get("allowed_runner_ids") or [])
    if allowed and any(d["fact"]["runner_id"] in allowed for d in docs):
        docs = [d for d in docs if d["fact"]["runner_id"] in allowed]
    return sorted(docs, key=lambda d: d["fact"]["runner_id"])


def mirrors(config: Config) -> list[dict]:
    return sorted(db.latest("mirror_status", config=config), key=lambda d: d["subject"])


def inventory(config: Config, unclassified: bool = False) -> list[dict]:
    docs = db.latest("repo_inventory", config=config)
    if unclassified:
        docs = [d for d in docs if d["fact"]["role"] == "unclassified"]
    return sorted(docs, key=lambda d: d["subject"])


def silicon_protection(config: Config, configuration: str | None = None) -> list[dict]:
    """silicon_protection rows, by configuration and host, uncovered first (section 13)."""
    docs = db.latest("silicon_protection", "silicon_protection:%s:" % configuration if configuration
                     else "silicon_protection:", config=config)
    return sorted(docs, key=lambda d: (d["fact"]["configuration"], d["fact"]["repo"]["host"],
                                       d["fact"]["covered"], d["fact"]["component"], d["fact"]["commit"]))


def run_by_nonce(config: Config, nonce: str) -> dict | None:
    docs = [d for d in db.latest("t0_result", config=config) if d["fact"]["run"]["nonce"] == nonce]
    return max(docs, key=lambda d: d["observed_at"]) if docs else None


def fact_by_subject(config: Config, subject: str) -> tuple[dict | None, bool]:
    """(the observation, is it current?). The current value first; else the newest stored one."""
    kind = subject.split(":", 1)[0]
    if kind in db.SET_KINDS or kind in db.EVENT_KINDS:
        cur = [d for d in db.latest(kind, subject, config=config) if d["subject"] == subject]
        if cur:
            return cur[-1], True
    hist = db.history(subject, config=config)
    return (hist[-1], False) if hist else (None, False)


# ---------------------------------------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------------------------------------
def _age_s(at: str, now) -> int:
    return int((now - clock.parse(at)).total_seconds())


def dashboard(config: Config, now=None) -> dict:
    """Section 13 dashboard: per configuration the six H1 items, plus open alerts as banners."""
    now = now or clock.now()
    compares = host_compares(config)
    runners = runner_settings(config)
    all_runs = runs(config)
    rows = []
    for rec in configurations(config):
        cid = rec["fact"]["id"]
        frozen = is_frozen(rec)
        p = pins(config, cid)
        r = [d for d in all_runs if d["fact"]["configuration"] == cid]
        last = latest_run(r)
        pend = pending_runs(r)
        tree = {d["fact"]["child"]["component"] for d in p if d["fact"].get("child", {}).get("component")}
        drifting, unknown, in_tree = drift(compares, tree)
        er = errata(config, cid)
        op = [d for d in er if d["fact"]["record"].get("status") in OPEN_ERRATUM_STATUSES]
        row_facts = [rec] + p + r[:1] + in_tree + er + runners
        oldest = min((d["observed_at"] for d in row_facts), key=clock.parse)
        rows.append({
            "id": cid, "record": rec, "frozen": frozen, "pins": p, "red": red_pins(p, frozen),
            "last": last, "pending": pend, "not_run": _not_run(last),
            "drifting": drifting, "unknown": unknown, "in_tree": in_tree, "errata": er, "open_errata": op,
            "age_s": _age_s(oldest, now), "pending_age_s": [_age_s(d["fact"]["run"]["triggered_at"], now) for d in pend],
        })
    return {"rows": rows, "runners": runners, "alerts": db.alerts(open_only=True, config=config)}


def _not_run(run: dict | None) -> int:
    if not run:
        return 0
    return sum(1 for g in run["fact"]["gates"] if g["verdict"] == "NOT-RUN")


def configuration_page(config: Config, cid: str) -> dict | None:
    rec = configuration_record(config, cid)
    p = pins(config, cid)
    r = runs(config, cid)
    if rec is None and not p and not r:
        return None
    frozen = is_frozen(rec)
    return {"id": cid, "record": rec, "frozen": frozen, "pins": p, "red": red_pins(p, frozen), "runs": r,
            "pending": pending_runs(r), "errata": errata(config, cid)}


def component_page(config: Config, comp: str) -> dict | None:
    rec = next((d for d in components(config) if d["fact"]["id"] == comp), None)
    hc = host_compares(config, comp)
    consumers = [d for d in db.latest("pin_state", config=config)
                 if (d["fact"].get("child") or {}).get("component") == comp]
    consumers.sort(key=lambda d: (d["fact"]["configuration"], d["fact"]["tree_paths"][0]))
    mir = [d for d in mirrors(config) if d["fact"].get("component") == comp]
    cited = [d for d in defects(config) if _cites_component(d["fact"]["record"], comp)]
    if rec is None and not hc and not consumers and not mir and not cited:
        return None
    tags: dict[str, set[str]] = {}
    for d in consumers:
        for host, h in (d["fact"].get("hosts") or {}).items():
            tags.setdefault(host, set()).update(h.get("tags") or [])
    return {"id": comp, "record": rec, "host_compares": hc, "consumers": consumers, "mirrors": mir,
            "defects": cited, "tags": {h: sorted(t) for h, t in sorted(tags.items())}}


def _cites_component(defect: dict, comp: str) -> bool:
    if defect.get("component") == comp:
        return True
    stack: list[Any] = [defect]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if x.get("component") == comp and "commit" in x:
                return True
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return False


def containment(config: Config, sha: str, component: str | None = None) -> tuple[list[dict], list[str], bool]:
    """(containment rows, problems, computed now?). Stored rows of the latest query for this commit prefix
    first; when none is stored, `analysis.contains.contains` answers it (and stores it, section 6)."""
    sha = (sha or "").strip().lower()
    if not _SHA_PREFIX.match(sha):
        return [], ["%r is not a commit id (4 to 40 hex characters)" % sha[:60]], False
    if component is not None and not is_valid_id(component):
        return [], ["%r is not a component id" % component[:60]], False
    prefix = "containment:%s:%s" % (component, sha) if component else "containment:"
    stored = [d for d in db.latest("containment", prefix, config=config)
              if d["fact"]["query"]["commit"].startswith(sha)
              and (component is None or d["fact"]["query"]["component"] == component)]
    if stored:
        return sorted(stored, key=lambda d: d["subject"]), [], False
    from nanosoc_rm.analysis.contains import contains
    try:
        docs = [db.as_doc(d) for d in contains(sha, component=component)]
    except Exception as e:                                       # noqa: BLE001 - shown, never a 500
        return [], ["the containment query could not answer: %s" % (str(e)[:300] or type(e).__name__)], False
    return sorted(docs, key=lambda d: d["subject"]), [], True


# ---------------------------------------------------------------------------------------------------------
# Run T0 (section 10 Trigger; the Run button)
# ---------------------------------------------------------------------------------------------------------
class RunPending(Exception):
    """A run of this configuration is still pending: at most one at a time (section 10)."""

    def __init__(self, run: dict):
        self.run = run
        super().__init__("run %s is still pending" % run["fact"]["run"]["nonce"])


def run_t0(config: Config, cid: str, login: str) -> dict:
    """Press Run T0 for `cid` as `login`: `ci.trigger.trigger(cid, by=login, kind="manual")`, which stores the
    run, makes the one write call, and logs the `run_t0` action (section 10 Trigger steps 2 to 5). Returns
    the t0_result (pending, or no-bundle when the trigger failed)."""
    pend = pending_runs(runs(config, cid))
    if pend:
        raise RunPending(pend[0])
    from nanosoc_rm.ci.trigger import trigger
    return db.as_doc(trigger(cid, by=login, kind="manual"))
