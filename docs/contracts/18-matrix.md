# 18. Compatibility matrix (built per CR-15)

Section 18 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Requirements: D3, D4, D7, D8, D9 (Phase 2), B7; CR-15 (build now, enable at the Phase 2 gate) and CR-18-matrix
(`docs/CHANGES.md`). Package `nanosoc_rm.matrix`; front ends `nanosoc-rm matrix` and `/matrix/` over the same
functions (section 0.1). Local mode only: every cell is a local compat T0 run (section 9a) queued as a local job
(section 13a). Nothing is scheduled: the nightly matrix ships disabled.

```python
# nanosoc_rm.matrix
COLUMNS = ("pinned", "in-range", "latest-tag", "head")
def matrix(configuration, *, columns=None, components=None, config=None, now=None) -> dict   # matrix_view (not stored)
def queue(configuration, *, columns=None, components=None, cells=None, by, config=None) -> list[dict]   # jobs
def nightly_plan(configuration, now, *, config=None, weekly_day=6) -> list[tuple]           # what it WOULD queue
NIGHTLY = Nightly(name="nightly_matrix", enabled=False, gate="Phase 2 entry gate ...", covers=...)
# nanosoc_rm.matrix.cells
def run_cell(configuration, column, component=None, *, config=None, store=False, by="cli", job=None,
             compat_fn=None, capture_fn=None, url_map=None, policy=None) -> list[dict]  # [matrix_cell, matrix_quarantine...]
def compare(cell_fact, baseline, quarantined=frozenset()) -> dict;  def baseline_for(cell_fact, stored) -> dict | None
def quarantine(configuration, config) -> list[dict];  def set_owner(configuration, test, owner, by, *, config) -> dict
# nanosoc_rm.matrix.provenance
def capture(run_dir, root_commit, requested, overrides, scratch) -> dict;  def moved(requested, captured) -> list
# nanosoc_rm.matrix.versions (VersionPolicy: latest, parse_range, latest_in_range; the Phase 3 versioning module
# may supply its own policy with these three functions)
```

1. **Rows and columns.** One row per component of the configuration's latest stored walk (section 4), in tree
   order; a component pinned at more than one commit (B7) is one row listing every pin, and its cells move every
   pin (a pin below another moved pin of the same component is left to the outer override). Columns, each a commit
   of the component's canonical repo read from its mirror: `pinned` (no override; one run serves every row and is
   the D9 baseline); `in-range` (the highest release tag inside the range the parent's `nanosoc-component.yaml`
   `requires[]` gives this id and gitlink path; else "no range"); `latest-tag` (the highest release tag
   `vMAJOR.MINOR[.PATCH]`, no pre-release; keep/, archive/ and freeze names are not versions); `head` (the default
   branch head the host advertised at the last sync). No commit: the cell says why and runs nothing. A commit equal
   to every pinned commit: "same as pinned", which shows the pinned result.
2. **A cell** (`run_cell`): `analysis.compat(configuration, overrides)` with its work directory kept; after the
   run the tree is re-read from the run's own pin fetches (`provenance.capture`, offline: one bare repo borrowing
   the objects of the `fetch-*` slots only, never the reachability clones `refs-*`, the negative controls or the
   upgrade work) and the directory is deleted. A pin is captured only when the run fetched its commit; a pin the
   run did not fetch (`t0.pins.fetchable` "not fetched", or below one) is null, "not read", and never counts as
   "captured equals requested". T0 checks out nothing and runs no setup script, so on a T0 run the capture can
   only confirm or fail to read a pin, never find one moved; the capture's `note` and the cell page say so. The
   rule (moved = UNVERIFIED) is for runs that can move a pin (a later tier with setup scripts). Recorded per attempt: requested and captured
   commits, the moved pins, gates (id, verdict, detail), compat's subject, host CPUs and 1-minute load at start and
   end; per cell: tool versions (git, python, nanosoc_rm), the harness, `ip_hash` null (T0 reads no IP file list).
3. **Verdict.** D7: MemoryError, OSError, TimeoutError, or a FAIL whose every failing gate's detail matches an
   infrastructure signature is INFRA, never FAIL. Signatures are whole phrases a host prints (`timed out`, `timeout
   after`, `out of memory`, `killed by signal`, `OOM-killer`, `licence checkout denied`, `no space left on device`,
   ...), never a bare word, so a real finding that names a file such as `watchdog_timeout.v` stays FAIL; one
   failing gate without a signature keeps the cell FAIL. Overloaded host: each attempt records `host.overloaded`
   (1-minute load above `provenance.OVERLOAD_PER_CPU` = 2.0 per CPU at start or end); a FAIL on an overloaded host
   whose re-run is green is INFRA, not FLAKY (and never counts toward quarantine); red twice stays FAIL, with the
   overload noted in the reason. D4: a pin captured at another commit than requested makes the attempt UNVERIFIED. D8: a FAIL attempt is run
   once more in the same job (the worker runs one job at a time: alone); FAIL again = FAIL, then PASS/WARN/WAIVED =
   FLAKY (`flaky_gates`: the gates that failed only the first time; `blocks_auto_merge`), anything else UNVERIFIED.
4. **Quarantine (D8).** A test is one T0 gate of one configuration (`cell` when no gate differs). The second FLAKY
   of a test within 14 days stores a `matrix_quarantine` with `owner` (the login that ran the cell), `since` and
   `expires_at` = since + 30 days. Expiry needs no action: an entry past `expires_at` is listed as expired and no
   longer counts. `nanosoc-rm matrix quarantine CONFIG --owner GATE=LOGIN` names another owner.
5. **Baseline (D9).** A cell is compared gate by gate with the pinned cell of the same configuration commit whose
   start is nearest within 12 hours (the same night); the comparison names that run (`baseline.subject`, `nonce`).
   With none, it says so and compares with nothing: never with older history. A pinned run with no gate results
   (INFRA, unanswered) or one that read moved pins is used only when no other run of that night is usable; it is
   then named, and the comparison has no rows and a note saying why it was not compared. Each row's `change` is
   `same`, `new-failure` (the cell FAILs or is UNVERIFIED where the baseline was not), `fixed` (the baseline failed and
   the cell answered PASS, WARN or WAIVED), `unanswered` (the baseline failed and the cell gave no answer: INFRA,
   NOT-RUN or no row; never shown as fixed) or `changed`.
6. **Jobs.** Kind `matrix-cell` (params `configuration`, `component`, `column`) through `jobs.register_kind`
   (`nanosoc_rm.matrix.jobkind`: params, cli_args, outcome, limit_s, action): the worker runs
   `nanosoc-rm matrix cell CONFIG --column COL [--component C] --store --json`; the cell takes its job id from
   `NANOSOC_RM_JOB` and the login from that job's record. Ceiling 3600 s (two T0 runs). Submitting logs `run_t0`
   (H2). `queue` skips cells already queued or running, no-target and same-as-pinned cells, and queues pinned first.
   A job that ended `infra` with no stored cell shows the cell INFRA (D7).
7. **Nightly (D3), disabled.** `NIGHTLY.enabled` is False and no `schedule.JOBS` job reaches the matrix; the
   Phase 2 register lists `nightly_matrix` under `automation`, so the A7 check fails if it is ever enabled before
   the gate (CR-15): a job of that name, any job whose code reaches `nanosoc_rm.matrix` or the `matrix-cell` job
   kind, or `NIGHTLY.enabled` set True (section 17.6, `phasegates.automation_reached`).
   `nightly_plan` / `nanosoc-rm matrix nightly CONFIG` print what it would queue: the pinned column, every non-head
   cell whose commit moved since its last stored run, and the head column once a week.
- **Observations** (`schemas/matrix.schema.json`, kinds added to observation.schema.json without a bump):
  `matrix_cell` (event, `matrix_cell:<configuration>:<component or ->:<column>:<nonce>`), `matrix_quarantine`
  (event, `matrix_quarantine:<configuration>:<test>`; latest is current), `matrix_view` (derived, never stored).
  The schema refuses a FAIL cell with fewer than two FAIL attempts, a FLAKY cell that is not FAIL-then-green, a cell
  or attempt with a moved pin that is not UNVERIFIED or INFRA, a NOT-RUN cell with an attempt, an INFRA attempt
  under a FAIL or green cell, and a quarantine with fewer than two FLAKY cells.
- **CLI:** `nanosoc-rm matrix CONFIG [--columns a,b] [--component C]... [--run [--wait]] [--json]` (the grid; `--run`
  queues, `--wait` works the queue in this process unless the service's worker holds it); `matrix cell CONFIG
  --column COL [--component C] [--store] [--json]`; `matrix quarantine CONFIG [--owner GATE=LOGIN] [--json]`;
  `matrix nightly CONFIG [--json]`. Exit: view 1 when a shown cell is FAIL or FLAKY; else 2 when a shown cell
  is UNVERIFIED or INFRA (could not answer, section 0.2) or no cell has a green result; else 0 (NOT-RUN cells, which
  have no commit to run, count neither way); cell 0 PASS/WARN/WAIVED, 1 FAIL/FLAKY, 2 UNVERIFIED/INFRA/NOT-RUN; 3 usage (unknown configuration, component
  or column; `--component` with the pinned column).
- **Pages** (plug-in `nanosoc_rm.matrix.web`, navigation "Matrix"): `GET /matrix/`, `GET /matrix/{configuration}`
  (`?component=` repeatable, `&columns=`), `GET /matrix/{configuration}/cell?component=&column=`, `POST
  /matrix/{configuration}/run` (`scope` = cell | row | column | all, `component`, `column`; 303 back to the grid
  with `queued=N`, and N = 0 is shown as "Nothing was queued"). The cell page answers 400 for a non-pinned column
  without `component`, or the pinned column with one (the CLI's exit 3).
  The POST answers 404 unless the app has local routes (section 13a), and then the pages show the CLI command
  instead of buttons. Hooks: `data-matrix`, `data-column`, `data-component`, `data-cell="<component or ->/<column>"`
  with `data-verdict` and `data-status`, `data-matrix-run="<scope>"`, `data-quarantine`, `data-nightly`, and on the
  cell page `data-attempt`, `data-baseline`, `data-change`. Colours: README section 6 with FLAKY amber, always with
  the verdict as text.
8. **Tier cells (CR-WD-matrix-t1; D3, D4, D7).** A view or a cell may add tier t1 (`matrix(..., tiers=["t1"])`,
   `queue(..., tiers=)`, `run_cell(..., tier="t1", force_tier=False)`; package `nanosoc_rm.matrix.tiers`, `TIERS =
   ("t1",)`). A tier cell is a separate stored cell of the same (component, column): `fact.tier` = "t1",
   `fact.force_tier`; T0-only cells carry no `tier` and read as before (`cells.latest_cells(configuration, config,
   tier=None)` returns T0-only cells; `bump_cell` reads only T0-only cells). The run is `analysis.compat(...,
   tier="t1", force_tier=...)` (section 9a "Tiers"): T0, then the registry's tiers.yaml t1 command in a sandbox
   checkout of the same tree; a T0 FAIL skips the tier unless `force_tier` (a person's `--force-tier`; the job param
   `force_tier`; never from the grid's Run), and the cell then stays FAIL. Per attempt: the tier rows join `gates`
   (`tier.t1`, `tier.t1.<gate>`; SKIP read as NOT-RUN), `tier` holds the tier's run (verdict, reason, forced,
   seconds, exit code, killed, over_budget, missing paths, the last 20 log lines), and the attempt verdict is
   compat's `run_verdict`. D7 applies to the tier rows as to the T0 gates.
   - **Preflight** (`tiers.preflight(configuration, tier, *, config, environ=None) -> Ready`), before anything
     runs, in the view too (it reads tiers.yaml and stats paths; it queues nothing): no t1 command, or one
     `tierrun.check_spec` refuses, is NOT-RUN with the reason (a registry fact, not the host's; counted neither
     way); a tool the spec names that this host lacks is INFRA with reason `tool not on this host: ...` and one
     attempt that ran nothing (not even T0): an absolute path in the tier's own `env` that does not exist
     (`tierrun.missing_tools`), or the command's program (argv[0], or a shell string's first plain word that is
     not a shell builtin) not found on the tier's clean PATH or not executable. The view shows such cells with
     status `no-command` or `no-tool` and the reason; `cell_requests`/Run never queue them.
   - **D4 moved pins.** After the command, the tier's working checkout is re-read
     (`provenance.capture_checkout(tree, root_commit, requested)`): per requested tree path the commit checked out
     there (`rev-parse HEAD`); when that equals the request, the parent's index gitlink is compared with the
     gitlink the parent's own commit records, and a differing index entry (re-pointed by the command) is what is
     captured. The superproject is the repo at the tree root. A path with no repo (not laid out: the tier's `pins`
     did not need it) is null, not read. Recorded as `attempt.checkout` and `provenance.checkout`; a moved pin
     from either capture is in `moved` and makes the attempt and the cell UNVERIFIED (the schema rule of item 3).
     Just before the command `tierrun.run_tier` writes `<work>/tree-layout.json` (`tierrun.LAYOUT_RECORD`): the
     tree paths laid out and, per laid-out repo, its tracked changes then (`git diff HEAD`, path -> sha256 of the
     working file). With it the re-read adds `laid_out`, `lost` (laid-out paths, "" = the superproject, that hold
     no readable repo after the command) and `dirty` (per laid-out repo, tracked files the command changed:
     changed against HEAD and not identical to the before-state; the tier's declared `verdicts` and `ip_files`
     excepted). Lost or dirty makes the attempt UNVERIFIED (D4: inputs not captured, or other contents than the
     requested commits read), and so does a run whose command ran with no record (`laid_out` null): without it a
     removed repo cannot be told from one not laid out.
   - **IP hash (D4).** When the tier declares `ip_files` and the run wrote it, `provenance.ip_hash` = sha256 over
     the sorted distinct lines `<sha256 of the file>  <path>` of the listed files. Each listed path is normalised
     first: an absolute path inside the sandbox tree (`$TREE/...`, either spelling of the tree) becomes
     tree-relative, since the sandbox directory differs per run; `./x` is `x`; an absolute path outside the tree is
     kept. `attempt.ip` = {hash, note, files}. Otherwise null with the reason in `ip_hash_note`: no list declared,
     not written by this run, empty, more than `provenance.IP_LIST_MAX` entries, a listed path that is not a
     regular file (device, FIFO, directory: never read), listed files totalling more than
     `provenance.IP_BYTES_MAX` (2 GiB) bytes, or a listed file unreadable (a hash of part of the inputs is not a
     hash of the inputs). A T0-only cell keeps `ip_hash` null ("T0 reads no IP
     file list").
   - **D9.** A tier cell's baseline is the pinned cell of the same tier and the same night.
   - **Job.** Params gain optional `tier` and `force_tier`; the worker runs `matrix cell ... --tier t1
     [--force-tier] --store --json`; ceiling 3600 s + 2 x the tier's kill limit (budget x 1.5).
   - **CLI / pages.** `matrix CONFIG --tiers t1` (a "+ t1" line under each row; the tier's preflight is printed;
     exit codes count tier cells like any other); `matrix cell CONFIG --column COL [--component C] --tier t1
     [--force-tier]` (3 for `--force-tier` without `--tier`). `GET /matrix/{configuration}?tiers=t1` adds a row
     `data-tier-row="t1"` per component with cells `data-cell="<component or ->/<column>@t1"` `data-tier="t1"`
     and a `data-tier-preflight="t1"` banner (`data-ready`); `GET .../cell?...&tier=t1` (400 for another tier);
     `POST .../run` takes `tier` (scope cell) and `filter_tiers` (row, column, all).
   - **Schema:** `matrix.schema.json` `$defs/tier`, `$defs/tier_run`, `$defs/ip_read`; `eligibility.schema.json`
     `tier_command.ip_files`. Examples `matrix.cell-t1.example.json`, invalid
     `matrix.cell-t1-checkout-moved-fails.yaml` ([history](../archive/interfaces-history.md#section-18-compatibility-matrix)).
- **Not built here:** T2 cells (the matrix adds only t1), CI-run cells (needs gate 3).
