# 22. The artefact store index (CR-WE-index; B10, G8, D12)

Section 22 of the [module interfaces](interfaces.md): its conventions (section 0) and names (section 17)
apply here. Sections 18 to 27 each have a file of their own, listed there.

The tool reads what the artefact store (Artifactory, `[stores.artifactory]`) holds and stores it as one fact;
pages show that fact. Read-only: the store client admits GET and HEAD on any path except the Access and
security APIs, and one POST, `api/search/aql` (WRITE_CALLS `read-post`). The allow-list tests the
percent-decoded path and refuses an empty segment, a `;` (Tomcat's `..;`), `.`, `..` and a `%` left after one decode. A PUT is admitted only to the publish
service (`check_request(..., publish_repo=<release repo>)`, inside the release repository); the index never makes
one. Bearer token only (`[stores.artifactory] token_file`); never Basic auth, never a netrc, never a redirect.

## 22.1 Config (`[stores.artifactory]`)

`url` (the API base, `http(s)://host[:port]/artifactory`, no user or token in it), `release_repo` (or the gate-2
`repo`; default `asic-release`), `token_file` (a 0600 file holding one access token; the CR-PR1 pattern, NOT a fifth
`[credentials]` name: AT-A2's credential set is unchanged), `token_expires` (YYYY-MM-DD, shown with the index),
`repos` (the repositories AQL reads; default all), `projects` (`[stores.artifactory.projects] <component> = "<store
project>"`, shared with the publish unit: a person's statement of which store project folder a component's uploads
are in; a configuration's project is its component's; nothing else maps a store path to a configuration). The
index also reads the publish unit's trust keys: `allowed_signers` (else the `.pub` beside `signing_key`),
`signer_identity` (default `nanosoc-rm`), `ssh_keygen` and `shared_logins`. `publishers` is the publish unit's
alone: Artifactory's `created_by` on a published manifest is the tool token's user, never the person, so it is not
compared with it.

## 22.2 One run (`store.index.build_index`, `nanosoc-rm store index [--store] [--no-cache]`, job `store-index`)

Each server feature is probed before it is used; a feature the server lacks is a probe row NOT-RUN with its
reason, and the sections that need it are NOT-RUN too: `system.ping`, `system.version`, `aql.items` (every file
with repo, path, name, size, created, created_by, md5, sha1 and properties), `aql.properties`, `build.list` and
`build.info` (every build record, a number filed twice read once per record with `?started=`; the vcs block is not
AQL-queryable, so it is fetched per build and joined here), `storage.release_repo`, `download.manifest` (each
`*.json` in the release repository, at most 1 MiB, and its `.sig`). Each manifest's `signature`: `verified` (an
SSHSIG, namespace `nanosoc-rm`, over its bytes by an allowed signer; `publish.signing.verify`), `invalid` (a `.sig`
that does not verify), `unverified` (no allowed signers, a signature that could not be read, a format the tool does
not check) or `absent`; `trusted` is `verified` and not uploaded by a `shared_logins` login. A manifest or signature
that could not be read makes the section INFRA. Index verdict: INFRA when the store did not answer, refused the
token or returned a cut answer; UNVERIFIED when the file listing could not run (AQL absent); else PASS.

The fact keeps the rows a page needs: every stream (GDS), source archive, manifest, signature and document as a
row; one row per foundry-return folder; every other file only in the per-repository counts by role. Join keys:
the RAW md5 (`rm.raw_md5`, `submitted.raw_md5`, `pnr.raw_md5`, `file.raw_md5`), never the stored object's checksum
(a `.zst` describes compressed bytes). Git identity is what the store records (`rm.repo`, `rm.commit`,
`release.commit`, any `*.commit`, short ids kept as given); a row without one says "no git identity recorded".
Documents up to 16 MiB that are not marked confidential are downloaded into `<data_dir>/store-cache/<sha1>` and kept
only when their sha1 matches the store's; the GUI serves only those copies (`GET /store/file/{sha1}`).

**Observation kind** `store_index` (event, no snapshot), subject `store_index:artifactory`, fact
`store.schema.json#/$defs/store_index`, sources of kind `artifactory_api` (`METHOD path`, no host, no token).
Examples: `store.index.example.json`, `observation.store.example.jsonl` (synthetic); invalid:
`store.index-pass-without-items.yaml`, `observation.store-index-from-git.yaml`. Exit codes: 0 PASS, 2 UNVERIFIED or
INFRA, 3 no `[stores.artifactory]` or no readable token file.

## 22.3 What pages and commands derive (`store.views`, stored facts only)

- Configuration > Silicon, **Taped out** (a frozen configuration, or any with a tapeout manifest): each tapeout
  manifest naming it (or in its component's store project): foundry, shuttle, date, GDS raw md5 IN STORE / NOT IN
  STORE / MISMATCH (against the stream at the manifest's store path), against the release lock's `outputs.gds.md5`
  MATCH / MISMATCH / NO LOCK, and the commit linked on its host (`hosts.<host>.web`, https only). The streams of the
  mapped store projects follow, each with its git identity or "no git identity recorded". The registry's
  unpublished tapeout drafts naming the configuration (`<registry>/tapeouts/<id>.tapeout.yaml`, a local read) are
  listed under it with a link to `/publish/tapeout/<id>` (a draft that does not meet `$defs/tapeout_draft` says
  why); with drafts the panel shows even without a store or an index (CR-WE-int).
- Configuration > History, **Timeline**: release, candidate and silicon tags (stored `tag_check`), release drafts,
  manifests, and uploads in its component's store project, newest first.
- Component > Versions (`/versions/{component}`), **Tags, commits and chips**: per tag its commit link and the
  silicon configurations (stored trees, or their root) and tapeout manifests that pin it.
- Releases: **Published** and **Store** columns per draft (a manifest for that configuration and version; its GDS
  raw md5 in the store), and the documents with a download link when a verified copy is held.
- Trust: only a trusted manifest is green. Where a verdict would be IN STORE, MATCH or MATCH (lock) for an
  untrusted manifest, the page shows UNVERIFIED (amber) with the reason; the Taped out panel shows each record's
  signature state.
- Inbox cards (category silicon): `store_mismatch` (a manifest names a store path or raw md5 the store does not
  hold, disagrees with the release lock, or is not trusted), `release_not_archived` (a silicon configuration with
  no trusted manifest in the release repository; raised only when the manifests section is PASS),
  `tapeout_unregistered` (streams the store marks shipped that no trusted tapeout manifest of a registered silicon
  configuration covers, one card per store project). `nanosoc-rm store findings` prints them.
- Search (the command bar and `nanosoc-rm store search`): artefact path, name or run tag; a hex prefix of a commit,
  a build revision, a raw md5 or a stored md5.

## 22.4 Pages and schedule

Plug-in `nanosoc_rm.store.web`, navigation "Store": `GET /store/` (`?q=`), `GET /store/file/{sha1}`, and in a local
deployment only `POST /store/index` (queues one `store-index` job; a second press while one is queued or running
reuses it). No GET calls the store or queues work. Scheduler job `store_index` (nightly, v1, enabled): queues one
`store-index` job when `[stores.artifactory]` is configured and none is queued or running; the worker runs it as a
child process.
