# nanoSoC Release Manager: MVP (v0.1.0)

Two jobs, from the command line, with no service, CI project or tokens:

1. **Collapse branches** in a project: `nanosoc-rm branches`.
2. **Check submodule compatibility** of a SoC configuration, optionally with pins changed: `nanosoc-rm compat`,
   optionally followed by the SoC's own lint (T1) or smoke simulation (T2).

The tool only reads repositories. It never pushes, tags or deletes; where a change is due, it prints the command
for a person to run.

## Set up (once, about 5 minutes)

```
git clone <this repo> nanosoc-rm && cd nanosoc-rm
scripts/dev-env.sh                      # Python 3.11 venv in .venv with the nanosoc-rm command
export PATH=$PWD/.venv/bin:$PATH
```

You also need:

- **A config file** (TOML, kept outside this repo), named by `NANOSOC_RM_CONFIG`. Minimal anonymous example:
  ```toml
  [service]
  data_dir = "/path/to/rm-data"                    # mirrors and the local store live here
  [registry]
  path = "/path/to/nanosoc-registry"               # a checkout of the registry repo
  [hosts.github]
  web = "https://github.com"
  api = "https://api.github.com"
  ssh = "git@github.com:"
  org = "SoC-Labs"
  [hosts.gitlab]
  web = "https://<gitlab-host>"
  api = "https://<gitlab-host>/api/v4"
  ssh = "git@<gitlab-host>:"
  group = "soclabs"
  ```
  Every key is listed in `docs/contracts/interfaces.md` section 17.1.
- **A registry checkout**: the catalogue of components, the registered configurations (`eligibility.yaml`), the
  vendor allowlist and, per configuration, `tiers.yaml`. Check it with `nanosoc-rm registry validate`.
- **Mirrors** of the repos you ask about: `nanosoc-rm mirror sync COMPONENT` (heads and tags only, anonymous).

## Job 1: collapse branches

```
nanosoc-rm mirror sync tidelink --all          # refresh both hosts' copies
nanosoc-rm branches tidelink --all             # one row per branch
nanosoc-rm branches tidelink --all --delete-list
```

Each branch gets one verdict:

| Verdict | Meaning | Deletable? |
|---|---|---|
| MERGED | its tip is on the default branch | yes, unless a tag or pin holds it |
| STALE | unmerged, no pin, no tag, no commit for 90 days (`--stale-days`) | yes |
| PINNED | a registered SoC's tree, or a registry citation, needs a commit only this branch holds | **no** |
| PROTECTED | a tag sits on its tip | **no** (the tag keeps the commits; the branch name can go if you wish) |
| ACTIVE | unmerged and recent | no |

`--delete-list` prints `git push <remote> --delete <branch>` lines for MERGED and STALE branches only; nothing
pinned, tagged or cited is ever listed. Review the list, then run the lines yourself. Exit code: 0 nothing to
delete, 1 something to delete, 2 no mirror or a repo could not be judged.

## Job 2: check submodule compatibility

```
nanosoc-rm compat eth-rc4                                      # the registered tree as it is
nanosoc-rm compat eth-main --pin nanosoc-multicore-system/nanosoc_arch_tech/nanosoc_gen=<40-hex sha>
nanosoc-rm compat eth-main --tier t1                           # then the SoC's lint, in a sandbox
nanosoc-rm compat eth-main --tier t2                           # or its smoke simulation
```

`compat` resolves the configuration's whole submodule tree from its own `.gitmodules` and gitlinks, applies each
`--pin TREE_PATH=SHA` override (the tree below an overridden pin is walked from the new commit), and runs the T0
checks locally, with the same gate code CI uses:

| Gate | Checks |
|---|---|
| `t0.infra.probe` | both hosts answer anonymously |
| `t0.pins.fetchable` | every pin can be fetched without credentials |
| `t0.pins.reachable` | every pin is on a branch or tag of its canonical repo |
| `t0.urls.no-ssh-public` | no public parent pins a submodule by ssh URL |
| `t0.manifest.valid` | component manifests validate (when present) |
| `t0.clone.upgrade-path` | an anonymous clone of the previous release upgrades to this one |
| `t0.vendor.no-arm-ip` | no Arm source (the Arm banner, a known vendor file by hash, an unreadable file) outside the allowlist (`allow` = clean, `warn` = amber) |
| `t0.vendor.no-foundry-data` | CR-6: foundry data and site inventory (file types, values, site paths, licence servers, big files; CR-19: release/bundle codes, the toolkit's `ident`) are WARN (amber), FAIL only for a rule the catalogue's `foundry_rules` marks `severity: fail`; NOT-RUN without the toolkit scanner or a private foundry rule |
| `t0.runner.hardened` | NOT-RUN locally (only meaningful in CI) |

Every gate also runs its negative control in the same run; a gate whose control does not fire is UNVERIFIED,
never PASS. A component pinned at two commits in one tree is printed as a split.

**Verdicts**, worst first: FAIL > UNVERIFIED > INFRA > WAIVED (a frozen configuration's known, approved
failures) > WARN (allowlisted vendor findings) > PASS. Exit code 0 for PASS, WAIVED or WARN; 1 for FAIL; 2 for
UNVERIFIED or INFRA; 3 for a usage or config error.

**Tiers** (`--tier t1|t2`) run the command the registry's `tiers.yaml` gives for that configuration, in a fresh
sandbox checkout of the resolved tree (overrides applied), with a clean environment (`set_env.sh` is never
sourced), a time budget (killed at 1.5 times the budget) and one VCS seat. A tier runs only after T0 is not FAIL;
`--force-tier` runs it anyway and marks the result as forced, which never counts for a release. A command that
regenerates `build_soc` needs `NANOSOC_GEN_OK=1`.

What-if runs (with `--pin`) are never stored. `--json` prints one `compat_result` document; `--keep-work DIR`
keeps the sandbox for inspection.

## Worked examples (real data, 2026-10-11)

- `branches tidelink --all`: 29 branches, 9 deletable (8 merged, 1 stale), 5 protected by archive tags.
- `compat eth-rc4` (143 s): fetchable, reachable and no-ssh are WAIVED (rc4's approved waivers for its frozen ssh
  pins and the private ASIC toolkit; the three R1 commits pass since their 2026-10-11 keep tags). Vendor is FAIL on one
  tree, nanosoc-multicore-system; 12 trees are WARN under the allowlist. Correction (CR-6, measured 2026-10-14): the
  FAIL is one Arm banner, `arm.banner` at `src/rtl/vendored/dma_250/cfg_min.yaml:2` (DMA-250 config, not covered by
  the DMA-230 allow entry), not site paths. Site paths and release names appear only with the toolkit scanner; CR-6
  moved site paths to `t0.vendor.no-foundry-data`, amber by default. CR-19 (2026-10-14) moved release names (`ident`)
  there too: amber, not red.
- `compat eth-main --tier t2 --force-tier`: the D2D decode smoke benches PASS in 29 s; the run stays FAIL because
  eth-main pins TideLink by ssh.

## Limits of this MVP

- Nightly runs and CI are the gate-3 live service (`docs/PLAN.md`). The web pages run on one host now, with local
  sign-in and a local job worker: `docs/GUI.md`.
- Local T0 on eth takes about 3 minutes against a 120 s budget.
- The VCS seat lock only covers other `nanosoc-rm` runs on the same host.
- Contracts, requirements and change requests: `docs/REQUIREMENTS.md`, `docs/contracts/`, `docs/CHANGES.md`.
