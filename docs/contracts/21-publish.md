# 21. Publishing to the release store (CR-ART-publish; G8, D12, G3)

Section 21 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

The tool has its own Artifactory token (one year, revocable); a person publishes release and tapeout manifests to
`asic-release` with a GUI button or the CLI. This relaxes "the web app is read-only" for exactly this one write
path. Module `nanosoc_rm.publish`.

## 21.1 Records (schemas `release-manifest`, `tapeout-record`)

- `asic-release/<project>/<version>/release-manifest.json` (+ `.sig`): built from a READY local release draft
  (section 14c) with a complete lock: version, tag, full commit of the superproject and of every pin, the
  configuration, the lock's md5 and sha256, the errata revision and digest, the gate verdicts, the raw GDS and
  netlist hashes of a silicon release, referenced stored artefacts (`--artefact ROLE=REPO/PATH`: path, the store's
  sha256 and md5, the raw md5 joined by the lock or by a raw-md5 property, never by a compressed object's checksum)
  and the draft's shared documents uploaded beside it (lock, ERRATA.md/.html, RELEASE_NOTES.md, GATES.md,
  CHECKLIST.md, build_soc.md5sums.txt; never the private archive copy). `<project>` is `[stores.artifactory]
  projects.<component>`, else the component id.
- `.../tapeout-<id>.json` (+ `.sig`): built from `<registry>/tapeouts/<id>.tapeout.yaml` (`$defs/tapeout_draft`,
  the Phase 3 backfill's shape; private): id, chip, foundry and shuttle, PDK and library versions, GDS raw md5 +
  layout hash + the stored object's sha256 (read from the store and checked against the draft and the store's
  `pnr.raw_md5`/`layout.canonical_hash` properties), submission date, foundry verdict, source commit and pins,
  sign-off reports, errata, documents, status draft/submitted/accepted/fabricated. It is filed in its release's
  folder, `<project>/<release version>/`: the draft's `release.project` (when given) must equal its `project`, a
  registered `configuration` must map (through `projects`) to that same project, and `release.manifest_sha256` is
  the sha256 of the release manifest STORED in that folder (read with GET; a draft naming another sha256, or one
  when none is stored, is refused; null when no release manifest is published there yet). The stored manifest
  must name the same project, version and configuration.
- Bytes are canonical JSON (sorted keys, two-space indent, one newline), with no publish time or publisher, so the
  sha256 a person confirms in the preview is the sha256 published.

## 21.2 Signatures

OpenSSH SSHSIG, namespace `nanosoc-rm`, `.sig` beside the record: `ssh-keygen -Y sign -f KEY -n nanosoc-rm FILE`
when `[stores.artifactory] ssh_keygen` (default: `ssh-keygen` on PATH) is OpenSSH 8.1 or later; otherwise, for an
RSA key, the module's own SSHSIG writer (rsa-sha2-512; standard library only), whose output `ssh-keygen -Y verify`
reads. RHEL 8 ships OpenSSH 8.0 (no `-Y`), so the tool host signs with an RSA key. An encrypted key is refused.
Verification reads `allowed_signers` (default: the key's `.pub`) and accepts only a key listed for
`signer_identity` (as `ssh-keygen -Y verify -I <signer_identity>` does), both before upload and on read-back.

## 21.3 Publishing (`publish.service`)

`preview(kind, key)` builds the record, reads the store with GET and HEAD only (each target path: absent, same or
differs; the build record; referenced artefacts) and writes `<data_dir>/publish/previews/<kind>/<key>.json`.
`publish(kind, key, confirm=SHA256, by=LOGIN)`:
1. readiness (url, a 0600 token file, a signing key this host can use, a public key to verify with, the token not
   expired) and `by` a listed publisher that is not a shared login, or the CLI's own `cli:<user>` (the shell user;
   any other `cli:` value is refused). A `by` given with `--by` is recorded with the account that ran the publish
   (`run_by`, `cli:<user>`: the GUI's job runs as the service user);
2. rebuild; refuse unless the sha256 equals `confirm`;
3. HEAD every target: one holding other bytes refuses the whole publish before any upload (never overwrite; OSS
   cannot forbid an overwrite, so the tool does); one holding the same bytes is a retry and is skipped;
4. sign, and verify the signature against the allowed keys BEFORE the first upload (the store is never
   overwritten, so a `.sig` left there must already verify); upload documents, record, signature, each with deploy-time properties (`rm.project`, `rm.repo`,
   `rm.commit`, `rm.dirty`, `rm.pins_md5`, `rm.config`, `rm.role`, `rm.raw_md5`, `rm.confidentiality`,
   `rm.release_version`, `rm.tag`, `rm.tag_commit`, `rm.manifest_sha256`; a tapeout adds `rm.tapeout_id`,
   `rm.subject_md5`) and checksum headers;
5. read back every path's sha256, the record and signature bytes, the signature, the record's property;
6. build-info `rm-release-<project>`/`<version>` or `rm-tapeout-<project>`/`<tapeout id>` (vcs: the superproject and
   every pin) and a status-only promotion RELEASED, or TAPED-OUT for a tapeout at submitted or later;
7. ledger: observation `release_publication` (event, subject `release_publication:<project>:<version>:<name>`), a
   local copy under `<data_dir>/publish/published/`, an `actions` row (`publish_release` / `register_tapeout`,
   refusals too) with the login.

## 21.4 Store client (`publish.client.Store`)

Bearer token only (`[stores.artifactory] token_file`, read per call, never logged; the CR-PR1 pattern, not a fifth
`[credentials]` entry). The base URL must end in `/artifactory` and carry no `user@` credentials (httpx would send
them as Basic auth), and no path has a `..` segment, so no call can reach the Access API, where a Basic-auth
failure locks the admin out. Allow-list checked before any I/O: GET/HEAD under the base; PUT only a new file
directly under `<release_repo>/<project>/<version>/` (never a checksum name) or `api/build` with a build named
`rm-release-<project>` or `rm-tapeout-<project>` (`check()` takes the build name, so only `put_build` reaches it); POST only `api/build/promote/<name>/<number>` for
those names (never a producer's build), with a body of the allow-listed keys `status`, `comment`, `ciUser`,
`timestamp`, `dryRun`, `copy`, `artifacts`, `dependencies`, `failFast` (no `targetRepo`, no `properties`, and
`copy`, `artifacts`, `dependencies` false). No DELETE, PATCH or property set exists.
`refuse_writes_here()` (called by `nanosoc-rm serve`) refuses every PUT and POST in the web process.

## 21.5 CLI, jobs, pages

- `nanosoc-rm publish-release CONFIGURATION/VERSION (--dry-run | --confirm SHA256) [--artefact ROLE=REPO/PATH]...
  [--by LOGIN] [--json]`; `nanosoc-rm register-tapeout ID (--dry-run | --confirm SHA256) [--draft FILE] [--by LOGIN]
  [--json]`. Exit 0 previewed or published, 1 refused, 2 could not answer, 3 usage (also: neither or both modes).
- Job kinds `publish-release`, `register-tapeout` (params: the key, `confirm` or none for a preview, `by`).
- Plug-in `nanosoc_rm.publish.web`, navigation "Publish": GET `/publish/`, `/publish/release/{configuration}/{version}`,
  `/publish/tapeout/{tapeout_id}` (stored facts only: the ledger, the preview file); local-only POST `.../preview`
  and `.../confirm` (a job each). Confirm needs the stored preview's sha256, a preview without problems and the
  ticked box; 403 for a login not in `publishers` or in `shared_logins`; 409 when readiness fails. The button is
  shown disabled with the reasons. In the web process readiness only stats the token and key files
  (`probe_signer=False`): it never parses the private key nor starts ssh-keygen; the job does both. The release draft page links to its Publish page.
- Phase 5 surfaces (`phasegates.LATER_SURFACES`): `/publish`, `publish-release`, `register-tapeout`. A scheduler job
  that reaches `nanosoc_rm.publish` or queues its job kinds is the `soc_release` automation (`AUTOMATION_CODE`).
