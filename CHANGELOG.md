# Changelog

What changed for people who use or operate the nanoSoC Release Manager, newest first. Each entry has a change
class (breaking, feature, fix or none; the format and its lint are in `docs/versioning.md`) and ends with the
change requests behind it, which are rows in `docs/CHANGES.md`. Check this file with
`nanosoc-rm changelog lint --path .`.

## [Unreleased]

### breaking
- Home (`/`) is now the Projects tree: projects, their configurations and their submodule pins with a freshness
  state each, and a detail panel. The inbox moved to Notifications behind the bell (`/notifications`; the full cards
  stay at `/inbox`). Bookmarks of the old Inbox home now open Projects. (CR-WF-gui, CR-WF-catalogue)
- The separate Freshness page is retired: `/freshness/` redirects to Home, or to that configuration's row on Home.
  Scanning stays (Scan now on a configuration, the scheduler, `nanosoc-rm freshness`). (CR-1015-retire)
- Signing out is a POST with the page's CSRF token; a plain GET of `/auth/logout` shows a sign-out button and signs
  nobody out. (CR-ROB-auth)
- In local sign-in mode, `serve` on a non-loopback address needs `--tls-cert` and `--tls-key`, or an explicit
  `--allow-plain-http`; without either it refuses to start. (CR-20)
- The store moves to schema version 3 on the first start (`rm.sqlite`; `jobs.sqlite` version 1). Do not run an
  older install against the same data directory afterwards. (CR-ROB-store, CR-1015-retention)
- `release list`, `bumps queue` and `matrix quarantine` with a configuration nobody registered or stored exit 3
  (usage) instead of printing an empty list. (CR-ROB-cli)
- Tier jobs (`compat --tier`, a matrix tier cell, `test-bump`) queued from the pages no longer run on the service
  host: they wait for an executor, a sandboxed agent on the EDA host. `matrix --run --wait` runs only the cells it
  queued. (CR-WG-capabilities, CR-WG-executor, CR-WG-integration)
- A bump proposal reads `would: hold` until a passing `test-bump` of exactly that move exists, so every current
  proposal holds at first. (CR-WG-capabilities)
- A what-if whose moved pin was walked but never fetched (an ssh or private URL) reads UNVERIFIED (exit 2), not
  FAIL, unless the gates judged a FAIL elsewhere. (CR-1016-quickfix, CR-WG-integration)
- Opening a Quality page never starts an analysis: a page with nothing stored offers an **Analyse** button.
  (CR-WG-integration)

### feature
- Web pages for a team on one host: local sign-in with named users (`nanosoc-rm user add`), HTTPS with a
  certificate of your own, and a local job worker so **Run** checks on the host when no CI is configured. (CR-16,
  CR-17, CR-20)
- Configuration workspace (Overview, Submodule tree, What-if with Now vs After per gate, Matrix, Silicon, History),
  component workspace (Branches with a delete list, Quality, Versions, Review) and a command bar search (`/`).
  (CR-W1, CR-W2, CR-W3)
- Submodule freshness: how far each pin lags its upstream default branch and when it last moved, on Home and the
  Matrix's Freshness tab; `nanosoc-rm freshness scan|show`. A configuration with a pin nobody could measure reads
  "Current except N not measured", never Current. (CR-WF-fresh, CR-1015-unmeasured)
- Catalogue page: groups, types and roles for every repository, a nightly org scan with **Scan now**, and the
  `catalogue.yaml` patch for your choices; `nanosoc-rm catalogue show|org-scan|preview`. (CR-WF-catalogue,
  CR-1015-orgscan)
- Compatibility matrix: each component at its pin, latest tag in range, latest tag and default-branch head, with
  re-runs for flaky cells, quarantine and optional lint (t1) cells; `nanosoc-rm matrix`. (CR-18-matrix,
  CR-WD-matrix-t1)
- Code and documentation quality: comment density, comment findings with suggested edits, README accuracy and doc
  conciseness, each fix a patch; a review queue with a patch basket. (CR-14, CR-WD-Q1, CR-WD-Q2, CR-WD-Q3,
  CR-WD-Q5)
- Versions and change classes: `changelog lint`, `version next|tags|draft`, `manifest init`, and the Versions
  pages. (CR-VER)
- Sandbox regeneration of generated RTL to class a generator move; `nanosoc-rm regen run|class|pending`.
  (CR-WD-regen)
- Bump proposals in shadow mode: what a bot would propose, with changelog, class, defects, matrix cell and a
  patch for a person; `nanosoc-rm bumps`. (CR-WB-bumps)
- Local release drafts with the release lock, errata document, notes, a checklist of tag commands and a gate
  ledger; fork listing of the example projects; `nanosoc-rm release draft|verify|list`, `nanosoc-rm forks`.
  (CR-R1)
- Read-only index of the artefact store on `/store/` and the Silicon, History, Versions and Releases tabs;
  `nanosoc-rm store index|search|findings`. (CR-WE-index, CR-WE-B10)
- Publish a signed release or tapeout record to the release store, as a person's confirmed step after a preview;
  `nanosoc-rm publish-release`, `register-tapeout` and the Publish page. (CR-ART-publish, CR-ART-gui-publish)
- Optional read-only access to private repos for the mirrors, per host, by ssh key or read-scope token. (CR-PR1)
- The vendor gate is split: Arm source stays red; foundry data, site inventory and release codes are amber unless
  the catalogue marks a rule blocking. (CR-6, CR-6b, CR-19)
- Store growth: an unchanged poll is recorded as a confirmation, not a copy; `nanosoc-rm store-maint compact`
  removes the old copies. (CR-1015-retention)
- Each repository declares what it can test in `.nanosoc/capabilities.yaml`, read at the commit under test;
  `nanosoc-rm capabilities validate|show` and the Capabilities pages show what each tree runs and where.
  (CR-WG-capabilities)
- **Test this bump**: `nanosoc-rm test-bump CONFIG PATH TO` and a button on a pin run the declared capabilities
  before and after a move at the same root commit, with T0 reported as hygiene beside them. (CR-WG-capabilities)
- A remote tier executor: a pull agent on the EDA host claims tier work over `/executor/v1/`, runs it under
  bubblewrap and posts the result back; `nanosoc-rm executor run|check|token|list`. (CR-WG-executor,
  CR-WG-integration)
- `GET /api/v1/bump-check` answers a repository's CI from the bump gate's evidence, with a reader token
  (`executor token new NAME --reader`). (CR-WG-integration)
- Repository, line and submodule pin read differently on every page (`branch main @ sha`, `path @ sha`, PINNED ×N,
  HOSTS DIFFER, Tracks), and requirement codes read as words with the code in a tooltip. (CR-WG-subrepo-ui)

### fix
- A rate-limited or unreadable host answer is "could not answer", never stored as a red pin state; a tree that
  cannot be read leaves the last good walk current. (CR-ROB-soak-1, CR-ROB-faults-9)
- A compat job's verdict agrees with its exit code; a matrix comparison with no answer reads `unanswered`, not
  `fixed`. (CR-ROB-faults-1, CR-ROB-coverage-6)
- Malformed job ids, versions and shas are refused cleanly (404, not a server error); `.gitmodules`, relative
  submodule URLs, CRLF files and pre-release ordering are read as git and SemVer read them. (CR-ROB5-ids,
  CR-ROB5-input, CR-ROB5-git, CR-ROB5-text)
- Sign-in throttling, session revocation and CSRF on sign-out hardened. (CR-ROB-auth)
- An untrusted release manifest is never shown green. (CR-WE-trust, CR-WE-trust2)
- Faster pages on a large store; an identical queued compat request reuses the queued job. (CR-ROB-store,
  CR-ROB-store-2, CR-ROB-store-3, CR-ROB-jobs)
- Fewer false comment findings (unknown names, commented-out code, README checks). (CR-WD-Q4)
- History and Store job links that answered HTTP 500 render; any other page fault is the service's own error page.
  Security headers on every answer, a per-address sign-in limit, private store files, a fail-closed publish
  registry read. (CR-1016-quickfix)
- A page never fetches from a git host to read a capabilities file; a failed claim can no longer leave a tier job
  running for ever; an agent granted one job kind claims only that kind; a tier command in an executor job cannot
  reach the job's own output. (CR-WG-integration)

### none
- New documentation: a day-one guide (`docs/getting-started.md`), a task-led user guide (`docs/user-guide.md`,
  replacing `docs/MVP.md`), one glossary (`docs/concepts.md`), a command reference generated from `--help`
  (`docs/cli.md`, kept in sync by a test) and this changelog. (CR-WG-docs)
- For plug-in authors: a plug-in page takes its template environment, page renderer, config, login, local-mode and
  job-card helpers from `nanosoc_rm.web.plugins` instead of copying them; the app's own routes moved out of
  `web/app.py` into `web/routes/`. Pages, URLs and behaviour are unchanged. (CR-WH-refactor-web)

## [0.1.0] - 2026-10-12

### feature
- `nanosoc-rm branches`: every branch of a repository with a verdict (merged, stale, pinned, protected, active)
  and a delete list for a person to run. (CR-8)
- `nanosoc-rm compat`: resolve a configuration's whole submodule tree, optionally with pins moved, and run the T0
  gates locally with their negative controls; `--tier t1|t2` then runs the configuration's lint or smoke
  simulation in a sandbox. (CR-11, CR-12)
- Phase 1 commands: walk, pins, contains, hosts, silicon-protection, mirror, registry, inventory, alerts,
  credentials, detect, scan and the T0 runner. (REQUIREMENTS.md v1.0)
- A WARN verdict (amber) for allowlisted vendor findings, below WAIVED. (CR-6a, CR-9)

[Unreleased]: https://github.com/SoC-Labs/nanoSoC-Release-Manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SoC-Labs/nanoSoC-Release-Manager/releases/tag/v0.1.0
