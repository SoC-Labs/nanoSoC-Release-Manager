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


_ACCESS_TEXT = {"anonymous": "public, read anonymously", "credentials": "private, readable with credentials",
                "unreadable": "unreadable"}
_CAUSE_TEXT = {"auth": "the host refused an anonymous read (private?)", "ssh-key": "the ssh key was refused",
               "ssh-host": "the ssh host key is not known", "not-found": "not found on the host",
               "network": "the host could not be reached", "timeout": "the fetch timed out",
               "config": "private_read is misconfigured", "other": "other error"}


def mirror_summary(rows: list[dict]) -> list[dict]:
    """CR-PR1: the mirrors counted by how they were read, each with a sentence (`text`) for the page."""
    from nanosoc_rm.mirror.sync import access_summary
    out = []
    for r in access_summary(rows):
        text = _ACCESS_TEXT.get(r["access"], r["access"])
        if r["method"]:
            text += " (%s %s)" % ("tried" if r["access"] == "unreadable" else "with", r["method"])
        if r["cause"]:
            text += ": " + _CAUSE_TEXT.get(r["cause"], r["cause"])
        out.append(dict(r, text=text, hosts_text=", ".join("%d %s" % (n, h) for h, n in sorted(r["by_host"].items()))))
    return out


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
    all_compat = compat_results(config)
    active = _active_jobs(config)
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
        compat = next((d for d in all_compat if d["fact"]["configuration"] == cid), None)
        row_facts = [rec] + p + r[:1] + in_tree + er + runners + ([compat] if compat else [])
        oldest = min((d["observed_at"] for d in row_facts), key=clock.parse)
        rows.append({
            "id": cid, "record": rec, "frozen": frozen, "pins": p, "red": red_pins(p, frozen),
            "last": last, "pending": pend, "not_run": _not_run(last),
            "drifting": drifting, "unknown": unknown, "in_tree": in_tree, "errata": er, "open_errata": op,
            "age_s": _age_s(oldest, now), "pending_age_s": [_age_s(d["fact"]["run"]["triggered_at"], now) for d in pend],
            "compat": compat, "jobs": [j for j in active if j["params"].get("configuration") == cid],
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
            "pending": pending_runs(r), "errata": errata(config, cid), "compat": compat_results(config, cid),
            "splits": splits(p), "waivers": waivers_of(rec), "silicon": silicon_protection(config, cid),
            "jobs": safe_jobs(config, limit=10, configuration=cid)}


def safe_jobs(config: Config, **kw: Any) -> list[dict]:
    """Local job records (jobs.jobs), or none when the job store cannot be read."""
    from nanosoc_rm import jobs as jobsmod
    try:
        return jobsmod.jobs(config, **kw)
    except Exception:                                            # noqa: BLE001 - a page never fails on it
        return []


def _active_jobs(config: Config) -> list[dict]:
    return safe_jobs(config, limit=200, states=("queued", "running"))


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


# ---------------------------------------------------------------------------------------------------------
# Local GUI (CR-16, CR-17): compat, branches, hosts, mirrors, alerts, registry lists
# ---------------------------------------------------------------------------------------------------------
LADDER = ("FAIL", "UNVERIFIED", "INFRA", "WAIVED", "WARN", "PASS")      # README section 6, worst first


def compat_results(config: Config, cid: str | None = None) -> list[dict]:
    """Stored compat_result observations (local runs without overrides), newest first."""
    docs = db.latest("compat_result", "compat_result:%s:" % cid if cid else "compat_result:", config=config)
    return sorted(docs, key=lambda d: d["fact"]["finished_at"], reverse=True)


def run_verdict(doc: dict | None) -> str | None:
    """A compat_result's verdict as Run shows it: T0 plus the tier when one ran."""
    if not doc:
        return None
    f = doc.get("fact") or {}
    return f.get("run_verdict") or f.get("verdict")


def splits(pin_docs: Iterable[dict]) -> list[dict]:
    """B7: components pinned at more than one commit in this tree, with every pin of each."""
    out: dict[str, list[dict]] = {}
    for d in pin_docs:
        f = d["fact"]
        comp = (f.get("child") or {}).get("component")
        if f.get("split") and comp:
            out.setdefault(comp, []).append(d)
    return [{"component": c, "pins": ps} for c, ps in sorted(out.items())]


def waivers_of(record: dict | None) -> list[dict]:
    """The configuration's t0_waivers as the registry states them (only a frozen configuration may carry any)."""
    rec = (record or {}).get("fact", {}).get("record", {})
    return list(rec.get("t0_waivers") or []) if rec.get("frozen") else []


def component_refs(config: Config, component: str) -> list[dict]:
    """Every head and tag of the component's repos in its mirror: [{host, path, kind, name, sha, date}], newest
    first. Empty when there is no mirror (`nanosoc-rm mirror sync --component <id>`)."""
    from nanosoc_rm.gitrun import run_git
    from nanosoc_rm.mirror.layout import mirror_path, split_key
    if not is_valid_id(component):
        return []
    mirror = mirror_path(config, component)
    if not mirror.is_dir():
        return []
    # %(*objectname) peels an annotated tag to its commit: an override must name the commit, not the tag object
    r = run_git("for-each-ref", "--format=%(refname) %(objectname) %(*objectname) %(creatordate:iso-strict)",
                "refs/r/", cwd=mirror, timeout=60)
    if not r.ok:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(" ")
        if len(parts) < 4 or not parts[0].startswith("refs/r/"):
            continue
        parts = [parts[0], parts[2] or parts[1], parts[3]]
        rest = parts[0][len("refs/r/"):]
        host, _, tail = rest.partition("/")
        quoted, _, ref = tail.partition("/")
        kind, _, name = ref.partition("/")
        if kind not in ("heads", "tags") or not name:
            continue
        _, path = split_key("%s/%s" % (host, quoted))
        out.append({"host": host, "path": path, "kind": "tag" if kind == "tags" else "branch", "name": name,
                    "sha": parts[1], "date": parts[2] if len(parts) > 2 else ""})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def branch_rows(config: Config, component: str, *, all_repos: bool = False, refresh: bool = False
                ) -> tuple[list[dict], list[str], bool]:
    """(branch_state rows of the component, problems, computed now?). The stored rows, or when there are none
    (or `refresh`), `analysis.branches.branches` reads the mirror now and stores them (CR8)."""
    if not is_valid_id(component):
        return [], ["%r is not a component id" % component[:60]], False
    stored = [d for d in db.latest("branch_state", "branch_state:", config=config)
              if d["fact"]["component"] == component]
    if not all_repos and stored:
        canonical = _canonical_repo(config, component)
        if canonical is not None:
            stored = [d for d in stored if (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"]) == canonical]
    if stored and not refresh:
        return _sort_branches(stored), [], False
    from nanosoc_rm.analysis import AnalysisError
    from nanosoc_rm.analysis.branches import branches
    try:
        docs = [db.as_doc(d) for d in branches(component, all_repos=all_repos, config=config)]
    except AnalysisError as e:
        return _sort_branches(stored), [str(e)[:400] or type(e).__name__], False
    except Exception as e:                                       # noqa: BLE001 - shown, never a 500
        return _sort_branches(stored), ["the branch inventory could not answer: %s" % (str(e)[:300] or type(e).__name__)], False
    return _sort_branches(docs), [], True


def _canonical_repo(config: Config, component: str) -> tuple[str, str] | None:
    rec = next((d for d in components(config) if d["fact"]["id"] == component), None)
    can = ((rec or {}).get("fact", {}).get("record", {}) or {}).get("canonical") or {}
    return (can["host"], can["path"]) if can.get("host") and can.get("path") else None


_BRANCH_ORDER = {"STALE": 0, "MERGED": 1, "ACTIVE": 2, "PINNED": 3, "PROTECTED": 4}


def _sort_branches(docs: list[dict]) -> list[dict]:
    return sorted(docs, key=lambda d: (d["fact"]["repo"]["host"], d["fact"]["repo"]["path"],
                                       _BRANCH_ORDER.get(d["fact"]["verdict"], 9), d["fact"]["branch"]))


def delete_text(docs: list[dict]) -> str:
    from nanosoc_rm.analysis.branches import delete_commands
    lines = delete_commands(docs)
    return "\n".join(lines) if lines else "# nothing to delete: no MERGED or STALE branch without a pin, citation or tag"


def compare_now(config: Config, component: str | None) -> list[str]:
    """Run the host comparison (section 5) for one component, or every component with copies. Problems only."""
    from nanosoc_rm.analysis.hosts import compare_hosts, components_with_copies
    problems = []
    try:
        ids = [component] if component else components_with_copies(config=config)
    except Exception as e:                                       # noqa: BLE001
        return ["the host comparison could not start: %s" % (str(e)[:300] or type(e).__name__)]
    for cid in ids:
        try:
            compare_hosts(cid, config=config)
        except Exception as e:                                   # noqa: BLE001
            problems.append("hosts %s: %s" % (cid, str(e)[:300] or type(e).__name__))
    return problems


def all_alerts(config: Config) -> tuple[list[dict], list[dict]]:
    """(open, cleared) alerts, newest raise first."""
    docs = db.alerts(open_only=False, config=config)
    return [d for d in docs if not d["fact"]["cleared_at"]], [d for d in docs if d["fact"]["cleared_at"]]
