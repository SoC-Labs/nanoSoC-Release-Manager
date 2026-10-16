# Getting started (day one)

For a team member opening the Release Manager for the first time. About 20 minutes. Words in **bold** are defined
in [concepts.md](concepts.md).

The tool watches the nanoSoC repositories and tells you what needs doing: whether a SoC's submodules still fit
together, which pins lag, which branches can be deleted, and whether taped-out silicon is still protected. It
only reads repositories. It never pushes, tags or deletes; when something should change, it gives you the command
or patch to apply yourself.

## 1. Open it and sign in (5 minutes)

1. Get the address, the login and the certificate fingerprint from the team's Confluence page (they are not
   written in this public repository). The address has the form `https://<host>:<port>/` and needs the
   university network or the VPN.
2. Ask the tool owner (David) for the team password. It is never written anywhere shared.
3. Open the address. Your browser warns about the certificate the first time: the tool uses a self-signed
   certificate on a lab machine. Check that the SHA-256 fingerprint matches the one on the Confluence page, then
   accept it for this address only.
4. Sign in.

If you share the team login, the action log records the team login, not your name, and **Mark all read** on
Notifications clears the items for everyone on that login. Say in the team channel before you start a long run.

## 2. Read Home (5 minutes)

Home (`/`, **Projects** in the header) is one tree: each **project**, its **configurations** (the registered
branches and tags), and their **submodule pins**. Each row shows a freshness state as a word beside a shape, and
how long since the pin last moved.

| You see | It means | What to do |
|---|---|---|
| **Current** | the pin is at the head of its upstream default branch | nothing |
| **Behind** | 1 to 9 commits behind | bump when convenient |
| **Outdated** | 10 or more behind, or behind and untouched for 90 days | open the row; the panel says how far and who moved it last |
| **Unpushed** | nobody can fetch the pinned commit | push that commit, today: nobody else can clone the tree |
| **Not measured** | the tool could not count (often a private repo it cannot read) | the panel gives the reason; nothing for you unless it is your repo |
| **Current except N not measured** | current where measured, N pins unknown | treat as unknown, not as current |
| **Frozen** | taped-out silicon; old on purpose | nothing; it is never counted as outdated |

Click a row to see its details in the right-hand panel. **How is this judged?** in the toolbar repeats the rules.
**Filters** narrows the tree by type and role. Projects with no registered configuration fold into **Not measured
yet**: that means nobody registered one, not that a check failed.

Under the tree, the configuration board shows each configuration's last check verdict (**FAIL**, **UNVERIFIED**,
**INFRA**, **WAIVED**, **WARN**, **PASS**, worst first). Freshness ("do the pins lag?") and the check verdict ("does
it work?") are different questions; a configuration can be Outdated and PASS.

The other header pages: **Matrix** (freshness across all configurations, and the compatibility matrix),
**Releases**, **Silicon** (taped-out commits that need protecting), **Catalogue** (new repositories to classify).
Everything else is under **More**. The bell opens **Notifications**: one line per thing that needs a person.
Press `/` to search for a configuration, a repository or a commit.

## 3. Test a bump (10 minutes)

Today, to see whether moving one submodule would break a configuration:

1. Open the configuration: select it on Home, then **Open the configuration** in the right-hand panel. Go to the
   **What-if** tab.
2. Pick the component and the target (a tag or branch from the mirror).
3. Press **Run what-if**. It queues a job; a T0 what-if took about 35 seconds on eth-main once started. One job runs at a time, so it may wait in the queue first.
4. Read **Now vs After**: one row per gate. A gate that is green now and red after is the problem.

From the command line (see [user-guide.md](user-guide.md) for setting it up):

```
nanosoc-rm compat eth-main --pin tidelink=<40-hex sha>
```

Limits today, so you are not misled:

- Only **T0** (resolve and hygiene) runs on the tool's host. Lint and simulation run on an **executor**, a
  sandboxed agent on a lab EDA server; until one is installed, those jobs wait ("waiting for an executor").
- Most configurations carry a known T0 red (for example ssh submodule URLs); in a what-if a T0 FAIL skips the
  tiers. A what-if whose moved pin was never fetched says **UNVERIFIED**, unless a FAIL was found elsewhere.

**Test this bump** (the button on a pin, or `nanosoc-rm test-bump CONFIG PATH TO`) moves a component everywhere it
is pinned and runs the capabilities the trees declare (`.nanosoc/capabilities.yaml`) before and after, at the same
commit, on an executor. T0 is reported as hygiene beside the functional result, so a known T0 red does not hide it.

## 4. Who to ask

| Question | Ask |
|---|---|
| Password, access, the service is down | the tool owner (David) |
| A verdict looks wrong | the tool owner, with the page URL; every fact has a **src** link showing the command that found it |
| A repository is missing or misclassified | use the Catalogue page to make a patch, then a merge request on the registry |
| Register a new configuration for your project | a merge request adding it to the registry's `eligibility.yaml` |
| What a word means | [concepts.md](concepts.md) |

## Next

- Task-by-task use (is my SoC compatible, can I delete this branch, should I bump, what is red, how to release):
  [user-guide.md](user-guide.md).
- Every command: [cli.md](cli.md). What changed recently: [CHANGELOG.md](../CHANGELOG.md).
