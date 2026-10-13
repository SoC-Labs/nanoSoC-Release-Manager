# Versions, change classes and tool releases

Phase 3 capabilities (C1-C12, F6, G1, G5, G6), built and usable locally under CR-15. Nothing is scheduled and
nothing writes to a repository, a host or the registry: every write is a file, a patch or a command for a person.
Contract: `docs/contracts/interfaces.md` section 14b and `schemas/tool-release.schema.json`.

```
nanosoc-rm changelog lint tidelink                 # C2: format, classes, Unreleased, links, tags
nanosoc-rm version next tidelink                   # C1, C3: current tag, Unreleased by class, signals, next version
nanosoc-rm version tags tidelink                   # C10: every tag by kind; moved release tags
nanosoc-rm manifest init tidelink --patch m.diff   # C11: a proposed nanosoc-component.yaml, as a patch
nanosoc-rm version refresh                         # lint + next + tags for every component, stored for /versions
nanosoc-rm version draft nanosoc_gen               # G1: a release draft folder (nothing tagged or pushed)
nanosoc-rm version approve <draft> --step tool-final-breaking --login <login>      # G5
nanosoc-rm version yank nanosoc_gen 0.2.0 --reason "..." --by <login> --patch y.diff   # G6: registry patch
nanosoc-rm version fixed-in nanosoc_gen --patch f.diff                             # F6: registry patch
```

Every command takes `--json` (one observation per line) and, except `draft`, `--path DIR` for a local checkout.
Exit 0 answered (PASS, or WARN with its reason), 1 finding or FAIL, 2 could not answer, 3 usage. GUI: `/versions/`.

## The changelog format (C2)

`CHANGELOG.md` at the repository root. Every entry is a list item under a class subsection.

```markdown
# Changelog

## [Unreleased]
### breaking
- Renamed port HSEL_O to HSEL (consumers must reconnect it).
### fix
- Fix the flist order for the fpga variant.

## [0.2.0] - 2026-10-01
### feature
- Add the DMA-250 window.

[Unreleased]: https://github.com/SoC-Labs/<repo>/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/SoC-Labs/<repo>/compare/v0.1.0...v0.2.0
```

| Rule | Checks |
|---|---|
| CL1 | CHANGELOG.md exists |
| CL2 | first heading `# Changelog` |
| CL3 | one `## [Unreleased]`, first |
| CL4 | `## [X.Y.Z] - YYYY-MM-DD` (optionally `[YANKED]`) |
| CL5, CL6 | newest first; each version once |
| CL7 | a subsection is `breaking`, `feature`, `fix` or `none` (`### Added` is suggested a class) |
| CL8 | every entry is under a class (C2 done-when: an entry without a class is rejected) |
| CL9 | no empty class subsection or release (warning) |
| CL10 | a release's classes match its bump under C1 |
| CL11 | link definitions for each heading, https, inside the canonical repo (warning) |
| CL12 | each release section has its tag and each non-grandfathered release tag has a section |
| CL13 | C5: an entry that adds a failing check or turns a warning into a failure is `breaking` |

## Next version (C1, C3)

- **Baseline:** the highest `vX.Y.Z` tag on the canonical host. Without one, the highest grandfathered
  version-like tag (`v1.0`, `v0.90`), with a note when it is not the newest by date (a person confirms).
- **Bump (C1):** at 1.0.0 and above, breaking bumps the major, feature the minor, fix the patch. Below 1.0.0,
  breaking bumps the middle number, feature and fix the last (the Cargo pre-1.0 rule). `none` makes no release.
  With no baseline, the first release is the version the repo declares (manifest or `pyproject.toml`), else 0.1.0.
- **Class (C3):** the most severe of: `declared` (the Unreleased entries), `api` (API.md diff since the baseline:
  a removed or changed line is breaking, only additions feature), `consumers` (every registered consumer's
  regenerated output diffed: ports, instances, address and IRQ lines), plus `reexports` (C6) and `build` (C7).
  The consumer regeneration needs the generator: it is a pluggable `Regenerator`
  (`nanosoc_rm.versioning.classify`). The default `NoGenerator` measures nothing and says so, so today every
  class is `partial` and its plan is WARN. A commit-message hint is shown and never used.
- **Verdict:** PASS (complete class), WARN (partial class), UNVERIFIED (no class at all: no classed changelog),
  FAIL (a finding: C9 refuses a 1.0.0, or the next version is already tagged or yanked).

## Decision table per repo class (C4, C5)

The class of a change is the most severe row that applies. Worked examples are real commits; the classes in the
last column are this table's answer, for review (C3 done-when replays them once the generator is plugged in).

| Repo class | breaking | feature | fix | Worked example |
|---|---|---|---|---|
| generator (nanosoc_gen) | a port, instance or file name it emits changes; a memory/IRQ map changes; a new check fails by default (C5) | a new option, off by default; new output files | output byte-identical except the fixed defect | `354faa9` "Updating port name script" (toplevel.py port naming): **breaking**, every consumer's port names move |
| arch_tech | a sys_desc change alters a generated port, instance or window; what it re-exports changes breaking (C6) | a new subsystem or option, off by default | a fix that leaves every consumer's output unchanged | `f8cf164` "fix(sys_desc): opt DEBUG and SOC_PERIPHERAL into INST_PORT_PREFIXED": **breaking** despite its `fix` message (11 + 13 ports change name) |
| IP tech | a port, parameter default or register map changes | a new port with a safe default, a new register | RTL fix, no interface change | (illustrative) an RTL fix with no port, parameter or register change: **fix** |
| toolkit | a gate fails that used to pass (C5); a target or flag removed | a new target or gate, warning by default | a false red removed | HSEL lint made fail-by-default: **breaking** (C5, David's decision) |
| SoC reference | a different module behind an address window; the pin set or file list changes for silicon (C7 build-affecting) | a new peripheral in a free window | a pin bump to a fix release | the PL230 → DMA-250 swap: **breaking** (the module behind the DMA window changes) |
| example project | the fork contract's user paths move (I2) | a new example flow | docs and script fixes | (illustrative) a renamed user directory: **breaking** for every fork |

A host move (C8) is a fix while the old host still serves the same commits; freezing or removing the old host is
breaking (`classify.host_move_class`).

## 1.0.0 (C9) and the manifest (C11, C12)

A first 1.0.0 is refused (FAIL) unless the repo has API.md, deterministic output, golden and consumer test sets
(manifest tests beyond t0), a changelog that lints clean and a SoC release using it. Unmeasurable items are shown
as unmeasured and still refuse. `manifest init` proposes `nanosoc-component.yaml` from the catalogue (id, class,
canonical https URL), the tree's submodules (a `^X.Y` range when the pin has a release tag) and the baseline;
an existing manifest's answers are kept; what only the owner knows is a `TODO` that still validates.

## Tags (C10)

Releases `vX.Y.Z` (annotated, canonical host only, never moved), candidates `vX.Y.Z-rc.N`, backport branches
`release/X.Y`, silicon `silicon/<chip>-rNpM`, keep tags `keep/<config>/<component>`. Existing tags are
grandfathered: the registry's optional `versioning.yaml` sets `tag_scheme_from`; before it is set (Phase 3's gate
is not met) every existing tag is grandfathered and the check is advisory (WARN). A moved release tag (two
commits in the mirror's reflog) is always a finding and, with `--alerts`, raises `tag_moved`. The walker's
`tag_moved` alert for a frozen configuration's tag is unchanged (interfaces.md section 4).

## Tool releases (G1, G5, G6)

`version draft` writes a folder for a person: `README.md` (the release PR description: classed changelog,
gates, approvals, submodules, defects), `release.json` (schema `tool-release`), `version.patch` (CHANGELOG
Unreleased to the version, manifest and pyproject versions), `CHANGELOG.release.md`, `commands.txt` (tag and gate
commands, never run). Gates: T0, T1, T2 on a SoC that uses the tool, T3 while build-affecting is unmeasured.

| Step (G5) | Approved by |
|---|---|
| tool candidate | the bot |
| tool final, fix or feature | the bot once T3 passes on the candidate |
| tool final, breaking | an owner (`owner:<component>`) and David (`release-manager`) |
| SoC release or train | David |
| tapeout SoC release or errata revision | David and a second reviewer (`reviewer`) |

Roles come from the service config `[roles]` and are enforced by `version approve`. The bot is automation: its
steps stay `automation-disabled` until the Phase 3 gate (CR-15), and a person cannot approve in its place.

A yank (G6) is a patch to the registry's `versioning.yaml`; the tag is kept; `version next` and the bump engine
(`versioning.allowed`, `versioning.is_yanked`) never target a yanked version. A silicon release (a `silicon/*` tag
on the commit, or a silicon configuration pinning it) is never yanked.

## Fixed-in from trailers (F6)

`Defect: SL-DEF-NNNN [form A,B]` or `Fixes: <sha of the defect's origin>` on the default branch proposes a
`fixed_in` entry. The patch edits `defects/SL-DEF-NNNN.yaml` text (comments kept) and is checked to validate. An
ambiguous form, an unknown defect or a `Fixes:` sha no defect cites is reported unresolved, never guessed.
