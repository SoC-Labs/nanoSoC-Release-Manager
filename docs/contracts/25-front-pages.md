# 25. The front pages: Projects, Matrix, Catalogue, Notifications (CR-WF-gui; H1, A3, A4, A5, PR4)

Section 25 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Requirements: H1, A3, A4, A5, PR4. `/` is the projects home and the inbox is Notifications behind the bell
([history](../archive/interfaces-history.md#section-25-the-front-pages)). Section 13's `GET /`
hooks (AT-H1: `data-configuration`, `data-item`, `data-verdict`, `data-run-t0`) stay on `/`, in the configuration
board closed under the tree. Every page reads stored facts only: a GET never walks, scans, fetches, queues a job or
creates gui.sqlite. Every page keeps one data-age element; a state is always a word beside a shape.

| Route | Shows or does | Module |
|---|---|---|
| `GET /` | the header and toolbar, the project -> configuration -> pin tree, the detail panel, the board (closed). Query (all optional; every row and control is a link carrying it): `v=1` (an explicit view: no default opening), `o` (repeatable; an open row key), `sel` (the selected row key), `f` (a freshness state), `lv` (project, soc, subsystem), `ty` (chip, chiplet), `ro` (implementation, forkable, example), `gap=1` (No forkable project), `filters=1` (the Filters disclosure open), `tl=on\|off` (the timeline for this view only) | `web.home` (`routers.DELEGATES["dashboard"]`) |
| `GET /matrix/` | tab **Freshness** (default): one row per repository, one column per stored tree; query `group` (`all`, project, toolkit, flow, component, generator, arch, lib, infra, legacy, ext), `rows=all` (also rows with no lag), `sort=behind\|age`, `comp` + `cfg` (the cell whose detail line is open). `?tab=compat`: the section 18 configuration list, unchanged; `/matrix/{configuration}` and every other matrix route are unchanged | `matrix.web`, `web.fmatrix` |
| `GET /catalogue/` | the new-repo queue (current `repo_seen` snapshots), "needs your call" (entries with a `question`), every group, the ignored list, "What Save writes" | `catalogue.web` (plug-in) |
| `GET /notifications` | one line per item (`?show=all`: read items too), "notify me about" | `web.notifications` |
| `GET /inbox` | the inbox cards (`web.inbox`, CR-W1, CR-W3) with evidence, snooze and undo; `?cat=` filters by category | `web.inbox` |
| `POST /home/timeline` | field `timeline` (`on`\|`off`), `return` (a `/` or `/?...` URL); saves the login's `timeline` preference; 303 back | `web.home` |
| `POST /notifications/read` | fields `item`, `fp` (the item's fingerprint), `read` (`0` = mark unread); 303 to `/notifications` | `web.notifications` |
| `POST /notifications/read-all` | repeated `item` + `fp` pairs: exactly the items the page showed (400 when none); a later item stays unread | `web.notifications` |
| `POST /notifications/prefs` | repeated `listed` (each preference the form showed) and `on` (the ticked ones); an unticked listed name is saved `off`, a ticked one removed; 400 for a name `uistate.PREF_NAME` refuses | `web.notifications` |
| `POST /catalogue/scan` | **Scan now** (CR-1015-orgscan): no fields but the CSRF token; queues one `org-scan` job (section 23.2; read-only, anonymous listing) as the signed-in login, or finds the one queued or running, and 303s to `/catalogue/?job=N#new`; 400 when no org or group is configured. The page reads the job records only (`catalogue.web.scan_status`: the `?job=N` org-scan job, else the latest; hooks `data-org-scan`, `data-org-scan-job`, `data-job-state`); the button is a `form="catalogue-scan"` button whose form sits outside the patch form | `catalogue.web` |
| `POST /catalogue/patch` | the page's choices (`new.<i>.repo`, `new.<i>.group`, `new.<i>.other`, `new.<i>.type`, `new.<i>.role`, `ask.<id>`, `confirm.<id>`) -> `catalogue.preview` (section 23.3). A new-repo choice applies to the repo its row's hidden `new.<i>.repo` (`<host>:<path>`) names, never to whatever repo now sits at row `i`; a repo no longer in the queue (or a row without the key) is skipped and listed as not used. Every new repo starts on Decide later: the suggested group is a hint (`data-suggest`), so an untouched Save changes nothing; `do=preview` shows the patch, its changes and whether it would load; `do=download` returns `catalogue.patch` (`text/x-diff`; 400 when nothing changes). Writes nothing (no registry, no store, no gui.sqlite) | `catalogue.web` |

The six POSTs are local-only (`web.plugins.local_routes`; `web.routers.WAVE_F_POSTS` lists the core four) and
CSRF-checked by the section 14 guard; a GitHub + CI deployment keeps section 13's two Run routes as its only writes
and its pages show no button for a missing route (the timeline toggle is then a link changing `tl` in the URL).

- **The tree.** Projects are the catalogue's ladder entries (group project, soc, subsystem; `catalogue.model`, from
  the stored `registry_record` component records) plus any configuration root the catalogue does not characterise.
  A configuration row is its `freshness.view.trees` summary (its verdict = worst DIRECT pin; a frozen tree says
  Frozen and "not counted"); a pin row's state is its `rollup` (the worst live state at or beneath it). Siblings
  sort by `home.sibling_rank`: SoC, custom peripherals (a direct component or subsystem of the configuration
  root), subsystems and components, arch tech, generator, libraries and infra, toolkits and flows; then worst state
  first. A project with no measured configuration folds into one of two groups by why: "Registered, not scanned
  yet" (`grp:unk`: a registered configuration with no stored scan) and "No configuration registered" (`grp:none`;
  its detail says how to register one).
- **Header** (CR-WH-usability; `web.page`). Main navigation: Projects, Matrix, Releases, Catalogue (`TOP_NAV`; an
  entry whose plug-in is not installed is left out) and the bell. Every other page is under **More**, in the groups
  of `MORE_GROUPS` (Check, Repos, Release; a plug-in page not listed goes under Other), each `role="group"` with its
  label; More closes on Escape and on a click outside it. A navigation label is its page's title. The data-age
  element keeps `data-age-seconds`/`data-oldest-observed-at` (the OLDEST fact, section 13) and reads "Updated <the
  newest fact's age> ago · oldest fact <age>", the age linking to the oldest fact's own sources page
  (`data-oldest-fact`). A signed-in page GET of an address no route serves is the styled Not found page with a next
  step (`data-next`); `/api` keeps a JSON 404. Each workspace tab's browser title names the tab ("Submodule tree ·
  eth-rc4"); the heading stays the configuration or component.
- **What needs me?** (`web.needs.items`; hook `data-needs`, each line `data-need="<key>"`): at most three one-line
  actions under the toolbar, each with one link, from stored facts only, in this order: `unreleased` (a
  configuration with a `silicon` record and no `release_publication` of kind release), `held` (bump proposals in
  state blocked that no person has marked), `private` (open alerts in the `private-*` cause groups), `catalogue`
  (current `repo_seen` snapshots plus catalogue entries with a `question`). Hidden when empty; a source that raises
  gives no line.
- **Pills** count live configurations by tree verdict and frozen ones as Frozen; the Not measured pill adds the
  projects with no stored scan to the scanned live trees that read Not measured, so every configuration is in one
  pill, and its filter keeps both. CR-1015-unmeasured: a tree reading `current_except` is in its own pill
  ("Current except not measured", `f=current_except`; shown, like Unpushed, only when a tree reads so); its row says
  "Current except N not measured" in amber beside a half-filled dot (`data-state="current_except"`, CSS key `cxn`), and
  a project whose worst live tree reads so says it with N summed over those trees. `data-state` is a
  `freshness.rules.TREE_STATES` value. The Matrix column head prints the tree word; a repository row with a live cell
  nobody could measure says "current except N not measured" (or "not measured") and stays under Lagging only.
- **No forkable project** chip (`gap=1`): counts the SoCs no forkable project wraps
  (`catalogue.model.socs_without_forkable_project`). While `catalogue.model.gap_unverified` is non-empty (a soc_ref
  with no stated group defaults to project, so the check cannot see it) the chip adds "· unverified"
  (`data-gap-unverified`, its title naming those entries): a count of 0 then measured nothing, as `catalogue show
  --gaps` exits 2.
- **Default view** (no `v=1`): the most lagging project with an Outdated or Unpushed live configuration is open,
  with its first live configuration; everything else (frozen trees included) is closed. **Timeline**: a bar from each row's newest bump to today on one axis (`_TL_MONTHS` whole months before
  today's month to `_TL_AFTER` days after today), the today line, and a dashed line at each frozen configuration's
  root commit date (its tapeout).
- **Page hooks** (tests read pages only through these): `data-row` (row key: `p:<project>`, `c:<configuration>`,
  `n:<configuration>:<tree path>`, `grp:unk`, `grp:none`) and `data-state` (a `freshness.rules.TREE_STATES` value) on each tree
  row, with `data-subject`/`data-observed-at` when the row is a stored fact; `data-fresh-pill` (state) on each pill;
  `data-timeline` (`on`\|`off`); `data-gap-filter`; `data-bell` (unread count, empty when unknown) on the bell;
  `data-matrix-row` (component), `data-column` (configuration), `data-cell-state` and `data-detail` on the matrix;
  `data-new-repo` (`<host>:<path>`), `data-ask` (component id), `data-group` (group), `data-pending`,
  `data-preview-valid` on the Catalogue page; `data-item` (item id) and `data-read` (`true`\|`false`) on each
  notification.
- **Repository, line and submodule pin** (CR-WG-subrepo-ui; `web.lines`). One vocabulary on Home, the
  configuration pages, the component workspace, the Matrix and search. A configuration row is a **line** of its
  repository: its name is `lines.line(record, root commit)["text"]` ("branch main @ 8e6d043", "tag v1.0.0 @
  ab73e40"; no " @ sha" when no commit is known), its configuration id beside it (`fp-alias`). A pin row is a
  **submodule pin**: "path @ sha", the path relative to the gitlink holding it, the component beside it only when it
  differs from the path's last segment. A repository pinned at N > 1 different commits in one tree carries
  "PINNED ×N" on each of its rows (`data-pinned-again`; `lines.pinned_again`), so two gitlinks of one repo never read
  the same. A branch of a pinned repository prints as `<repo>:<branch>` (`lines.qualified`). Each row kind has an
  inline-SVG icon (`_ui.html` `glyph(kind)`, kinds `lines.KINDS`: repo, branch, tag, pin, again; aria-hidden, hook
  `data-glyph`); "How is this judged?" prints the legend (`data-legend`, `lines.LEGEND`). The Matrix groups the
  columns of one repository under a spanning head (`data-column-repo`), each column head is the line (its id and
  verdict under it), and a repository's cell in its own root's column reads "root @ sha" (`data-cell-root`); the
  Compatibility tab's configuration list has a Line column (`data-ref`). The component Overview lists the
  repository's own lines (`data-lines`, `data-line` = configuration id). The rail groups configurations under their
  repository (`data-rail-repo`). Search names a configuration "line of <repo> · branch main". The word SPLIT is
  retired from the GUI: a pin split reads PINNED ×N (PINNED AGAIN when the walk holds one of the commits), a host
  copy that differs reads "HOSTS DIFFER: <host> <relation> N". Spec codes print as words with the code in a
  tooltip (`web.terms`: `plain` Jinja filter, applied to every `_ui.html` chip; host states such as ON_DEFAULT ->
  "on default branch", tiers T0-T6 -> "resolve check" .. "ASIC flow", requirement IDs dropped from headings into a
  `title`).
- **Notifications.** Items are `inbox.cards` (one per card; kind = the card's category) plus `catalogue:new-repos`
  (kind `catalogue`) when the current `repo_seen` snapshots hold any repo. Read state is the inbox's done state
  (`uistate.set_card` with the item's fingerprint): an item read here is done in the card view, and it comes back
  unread when its facts change. The bell counts unread items of kinds and projects the login has not muted.
  An item's projects (`Item.about`) are those of the configuration or project its id names and of every
  configuration its facts or evidence name (an erratum's exposures, `never_checked:all`); muting projects hides an
  item only when all of its projects are muted, and an item about no project is muted only by its kind.
- **gui.sqlite** (`web.uistate`, `user_version` 2): table `pref (login, name, value, at)`; names `timeline`
  (`on`\|`off`; absent = off), `notify.kind.<kind>` and `notify.project.<id>` (`off` mutes). `prefs()` never
  creates the file; a version-1 file gains the table the next time it is opened, other tables untouched.
