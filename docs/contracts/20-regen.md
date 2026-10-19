# 20. The regenerator: C3 consumer regeneration and C7 (built per CR-15)

Section 20 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

Requirements: C3, C7 (Phase 3; built and used locally, nothing scheduled). Package `nanosoc_rm.regen`.

A configuration opts in with a `regen` entry in the registry's `tiers.yaml` (`eligibility#/$defs/regen_command`):
a tier command plus `output`, the tree-relative directory it writes (build_soc), and optional `include`/`exclude`
globs over it. Example: `schemas/examples/eligibility.tiers.example.yaml` (`multicore-master`).

## 20.1 A render (`regen.render`, `nanosoc-rm regen run`)

1. **Request.** The configuration's commit is resolved once (`ci.trigger.build_request`); every tree path where the
   stored walk pins the component (`regen.tree_paths`; a B7 split moves together) is overridden to the commit
   (`pin_overrides`, CR-11). Both renders of a measurement share that root commit.
2. **Tree.** The overridden tree is walked anonymously in a fresh sandbox under `[regen] work_dir`
   (`t0.gates.Context.for_local`: empty HOME, no credential, no user git config) and the spec's `pins` are checked
   out (`tierrun.materialise`). An override the walk did not reach: not run, UNVERIFIED. A pin not laid out or a
   tool absent on this host (`tierrun.missing_tools`): not run, INFRA.
3. **Run.** `tierrun.run_tier` (clean environment, PATH=/usr/bin:/bin, budget, kill at 1.5 x), plus:
   PYTHONDONTWRITEBYTECODE=1 always; `nice -n <[regen] nice>` (default 15); the `output` directory deleted first.
   Refused before any work (RegenUsage, exit 3): no `regen` entry; a set_env.sh sourced; `env` naming
   ARM_IP_LIBRARY_PATH or SOCLABS_DMA250_DIR (the registry schema refuses them too, so a registry naming one does
   not load); any path under a `[regen] read_only_roots` entry (symlinks resolved) in the command, the env or the
   work directory. Without the vendor library the generator skips its vendor-rendered parts (bus matrices,
   DMA-250); its own "skipping ..." lines are recorded as `coverage`. The configuration and component must be ids
   and the commit a full sha; `--out` under a read-only root is refused before any directory is made. A run killed
   at budget x 1.5, or one that fails on this host's environment (a missing Python module, `command not found`,
   exit 126/127), is status `not-run`, verdict INFRA: no measurement.
4. **Collect.** Every file under `output` (text, at most 4 MiB each; others counted as `binary`), normalised: the
   sandbox's paths become `$TREE`/`$WORK`, a line holding a date-time stamp is dropped. `flist/*` files are the C7
   file lists, one per variant. sha256 `digest` over the result: two renders of one commit give one digest.
5. **Cache.** `<work_dir>/cache/<configuration>/<component>/<commit>/<root commit[:12]>-<spec digest>-<tree paths
   digest>-<code version>/` (a partial `--at` render is never served as the full B7 move; a render made by other
   render code is a miss); a cached render is used only when its digest still matches its files (`--fresh` ignores
   it; `--out` never reads it). Failed renders are not cached.

## 20.2 The class measurement (`regen.measure_class`, `nanosoc-rm regen class`, job `regen-class`)

The class is `versioning.classify.diff_outputs` over the two renders (docs/versioning.md decision table): a removed
file or a removed/changed port, instance, address or IRQ line is breaking; an added file or shape line feature;
any other text change fix; identical output none. C7: build-affecting when a `flist/*` list differs. A generator
that renders the configuration at `from` and fails for it at `to` is breaking (C5: a consumer that built no longer
builds; `files` and `build_affecting` null); a `to` run killed or failing on the host's environment is INFRA, not
C5 (20.1 step 3). Verdict: PASS (class measured), FAIL (the generator failed at `from`:
no baseline), INFRA (not run here), UNVERIFIED (override not reached). Exit 0, 1, 2, 2; usage 3.

**Observation kind** `regen_class` (event, no snapshot), subject
`regen_class:<component>:<from>:<to>:<configuration>`, fact `regen.schema.json#/$defs/regen_class`. Examples:
`regen.class.example.json`, `observation.regen.example.jsonl` (REAL: multicore-master, 2026-10-14); invalid:
`regen.class-pass-without-class.yaml`, `eligibility.tiers-regen-vendor-env.yaml`.

## 20.3 Wiring into the classifier and the bumps class

- `versioning.consumers.regen_signals(component, base, head, config, regenerator=None)` gives the C3 consumers and
  C7 build signals: a live `Regenerator` when one is passed (`regen.SandboxRegenerator(config)` conforms), else the
  stored `regen_class` measurements of exactly that move (`regen.stored_signals`), else unmeasured, naming the
  `regen class` command that would measure it. A measurement that is not PASS leaves the signal unmeasured with its
  reason; one consumer unmeasured makes the signal unmeasured (C3 complete needs every consumer). Every
  configuration that declares a `regen` entry and pins the component is a consumer: one with no stored
  measurement leaves the stored signals unmeasured, naming the `regen class` command (as the live path, which
  regenerates each).
- `version next` (`versioning.plan`) uses it for the baseline-to-head move; `classify_bump` for a pin move, so the
  bump engine's `change.class` (`class_source: classifier`) carries the measured class, and its sources name each
  stored measurement (`stored regen_class:...`).
- `versioning.classify.consumer_signal`/`build_signal` no longer raise when a live render fails: the consumer is
  unmeasured with the reason, or breaking when only the head fails (C5, as 20.2).
- `nanosoc-rm regen pending [--run]` lists the moves a stored version plan or bump proposal would class that no
  stored measurement covers, per configuration with a `regen` entry pinning the component; `--run` measures them.

`[regen]` service config (all optional): `work_dir` (default `<data_dir>/regen`), `read_only_roots` (list),
`nice` (int, default 15). No page in this wave: the measurements reach the GUI through the version plans and bump
proposals that cite them. Phase 3 surfaces: the `regen` CLI group (`phasegates.LATER_SURFACES`); a scheduler job
named with `regen` is Phase 3 automation (`LATER_AUTOMATION`).
