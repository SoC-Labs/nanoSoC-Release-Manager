# 23. Catalogue characterisation (CR-WF-catalogue; A1, B1, H1)

Section 23 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

The catalogue as a ladder: a **project** (the top-level repo that builds a chip or a chiplet) wraps a **SoC**, which
integrates **subsystems** and **components**, built from **arch** tech units and **generators**; **toolkits**,
**flows**, technology **libs**, **infra** and **legacy** sit beside it ([history](../archive/interfaces-history.md#section-23-catalogue-characterisation)). The data model and the two read paths
the GUI builds on. Nothing here writes the registry. Package `nanosoc_rm.catalogue`.

## 23.1 The model (`catalogue.model`, `Registry.characterisations()`)

Catalogue schema (additive, `schema` stays 1): each component may carry

| field | values | where |
|---|---|---|
| `group` | project, soc, subsystem, component, toolkit, flow, generator, arch, lib, infra, legacy | any entry; `legacy` exactly when `class: legacy` (rule 15) |
| `type` | chip, chiplet (project); soc (soc); subsystem (subsystem) | required on the three ladder groups, refused elsewhere |
| `role` | implementation, forkable, example | required on the ladder groups, refused elsewhere |
| `wraps` | component ids | project (a soc or subsystem) and soc (a subsystem or component) only (rule 15) |
| `guessed` | subset of group, type, role, wraps, inside | fields still a judgement; `inside` = which project or SoC wraps it |
| `question` | text | the open question to the owner; needs `guessed` |

`ignored` entries keep their `reason` (unchanged). Defaults for a catalogue without these fields (every old
catalogue loads unchanged): group from class (`DEFAULT_GROUP`: soc_ref and example -> project, generator ->
generator, arch_tech -> arch, ip_tech -> component, toolkit -> toolkit, infra -> infra, legacy -> legacy), role
from class (`DEFAULT_ROLE`: soc_ref -> implementation, example -> example), type unknown (None) unless the
group fixes it; each field filled this way is listed in `derived`.

```python
@dataclass(frozen=True) class Characterisation:
    id: str; cls: str; group: str; type: str | None; role: str | None
    wraps: tuple[str, ...]; wrapped_by: tuple[str, ...]     # wrapped_by: the entries whose wraps name it
    guessed: tuple[str, ...]; derived: tuple[str, ...]; question: str | None
    top_level: bool                                         # group == "project"
    unconfirmed -> tuple[str, ...]                          # guessed + derived, in GUESSABLE order
    def as_doc(self) -> dict                                # the fields above plus unconfirmed, JSON-ready
Registry.characterisations() -> dict[str, Characterisation]   # catalogue order, wrapped_by filled
model.characterise(component_dict) -> Characterisation        # one entry, wrapped_by empty
model.characterise_all(component_dicts) -> dict[str, Characterisation]
model.socs_without_forkable_project(chars) -> list[str]       # group soc, no `forkable` project in wrapped_by
model.gap_unverified(chars) -> list[str]   # soc_ref entries with a derived group: while non-empty, an empty gap
                                           # list measured nothing (an uncharacterised SoC reads as a project)
model.effective_group(component_dict) -> str | None; model.class_for(group, role=None) -> str
model.class_agrees(group, cls) -> bool                        # cls in GROUP_CLASSES[group]
constants: GROUPS, LADDER_GROUPS, TYPES, ROLES, GUESSABLE, WRAP_TARGETS, DEFAULT_GROUP, DEFAULT_ROLE,
           CLASS_FOR_GROUP, GROUP_CLASSES, GROUP_LABELS, TYPE_LABELS, ROLE_LABELS (display words: "Forkable template", ...)
```

CLI: `nanosoc-rm catalogue show [PATH] [--group G]... [--gaps] [--unconfirmed] [--json]`, one row (or
`as_doc()` plus `gap: bool`) per component. Exit 0; with `--gaps`, 1 when any SoC has no forkable project,
2 (UNVERIFIED, stderr names them) when `gap_unverified` is non-empty. A page shows the gap check as unverified in
that case, never as "no gaps". WRAP_TARGETS: a project wraps soc, subsystem or component (its own custom
peripherals and accelerators); a soc wraps subsystem or component.
Examples: `component-catalogue.example.yaml` (8 entries characterised, the rest derived); invalid
`component-catalogue.type-on-toolkit.yaml`, `.project-without-role.yaml`, `.question-without-guessed.yaml`,
`.soc-typed-chip.yaml`.

## 23.2 The org scan (`catalogue.orgscan.org_scan`, job `org-scan`)

`org_scan(config=None, hosts=None, store=False) -> ScanResult(docs, errors, listed)`: lists the configured GitHub
org and GitLab group (with subgroups) through `mirror.inventory.list_host(..., anonymous=True)`: GET only, every
page, **no credential** even when the service has read tokens (`mirror.layout.api_get(..., anonymous=True)` sends
no Authorization header). Each listed repo the catalogue does not hold (not a component's canonical, copy or
alias, not external, not ignored) becomes a `repo_seen` observation (set kind, scope `repo_seen:<host>`, one
complete snapshot per host per scan; empty when nothing is new, so a repo catalogued or ignored since drops out).
Fact (`observation.schema.json#/$defs/repo_seen`): `repo`, `visibility`, `default_branch`, `created_on` (date or
null), `description` (whitespace-collapsed, at most 300 characters, or null), `mentions_nanosoc` (name or
description matches nano-soc: a hint, not a judgement), `fork` (the listing's own flag: GitHub `fork`, GitLab
`forked_from_project` present), `first_seen` (carried from the current stored fact),
`listing: anonymous`. A host whose listing fails stores nothing; `errors[host]` says why; the other host is still
scanned. Limit: an anonymous listing cannot see a private repo; `nanosoc-rm inventory` (with the read token) stays
the A1 census.

CLI: `nanosoc-rm catalogue org-scan [--host github|gitlab]... [--store] [--json]`; exit 0 nothing new, 1 new repos
seen, 2 a host could not be listed; the last stderr line is the summary (`org-scan: N new repo(s) ...`, with
`could not list <host>` on a failure). Job kind `org-scan` (params `{host?}`; runs
`catalogue org-scan [--host H] --store --json`; verdict PASS when every host was listed, UNVERIFIED otherwise;
result `{new, repos}`). A page reads `db.latest("repo_seen")`; it queues the job only from a POST, never from a
GET. The job name holds no word a later phase's gate governs (phasegates rule 5a). CR-1015-orgscan: the scheduler
job `org_scan` (nightly, at or after `schedule.nightly_utc`;
v1, enabled, not a CI job) queues one `org-scan` job with no params unless one is queued or running, and only when a
GitHub org or GitLab group is configured (`orgscan.scanned_hosts`); the Catalogue page's **Scan now** is
`POST /catalogue/scan` (section 25), which queues the same job.
Examples: `observation.catalogue.example.jsonl`; invalid `observation.repo-seen-with-credentials.yaml`.

## 23.3 The catalogue-change preview (`catalogue.preview.preview`)

`preview(registry_or_path, choices) -> Preview(patch, changes, problems, catalogue)`; `Preview.valid` is
`not problems`; `Preview.as_doc()` gives `{patch, changes, problems, valid}`. Like the review basket, the tool
never writes the registry: `patch` is a unified diff of `catalogue.yaml` (`a/`, `b/` prefixes, path relative to
the registry repo's top, so `git apply` works there) that a person reviews and applies by merge request.
`problems` is what the loader would say about the patched catalogue against the registry's other files (schema,
then rules 1 to 15), formatted `file:pointer: message [rule]`; an empty list means the patch loads. Each
component entry is one flow-mapping line and only its mapping is rewritten (comment kept; characterisation fields
right after `class`); an entry written over several lines is a problem, not an edit. `PreviewUsage` (CLI exit 3)
for a choice it cannot read. Choices, applied in order:

```
{"op": "set",     "id": ID, "fields": {group|type|role|wraps|question: VALUE or null}}   # confirms what it sets
{"op": "confirm", "id": ID, "fields": [group|type|role|wraps|inside, ...]}           # drops them from guessed
{"op": "add",     "repo": {"host", "path"}, "group": G, "id"?, "type"?, "role"?, "wraps"?}  # class = class_for
{"op": "ignore",  "repo": {"host", "path"}, "reason": TEXT}                            # "Not nanoSoC"
```

`set` of group soc/subsystem fills the matching type; a group off the ladder drops type, role and their guesses;
off project/soc it drops wraps. A new group that may not carry the entry's class (`GROUP_CLASSES`) sets the
class to `class_for(group, role)` and the change line says `(class OLD -> NEW)`; so moving into or out of
legacy is one choice. `question` goes once `group` is no longer guessed. `confirm` of a derived field
writes the derived value out. CLI: `nanosoc-rm catalogue preview CHOICES|- [--registry PATH] [--out FILE]
[--json]`: the patch on stdout (or the `as_doc()` JSON); exit 0 valid, 1 the patched catalogue would be
refused, 3 a bad choice or an `--out` inside the registry.
