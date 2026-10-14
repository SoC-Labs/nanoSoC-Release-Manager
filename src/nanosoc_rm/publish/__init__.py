"""Publishing release manifests and tapeout records to the release store (CR-ART-publish; G8, D12, G3).

David's decision 2026-10-14: the release manager writes signed manifests to `asic-release` through ONE path, a
person's confirmed Publish (GUI button or `nanosoc-rm publish-release` / `register-tapeout`), with its own
Artifactory token and its own method allow-list. Nothing schedules it (Phase 5 automation `soc_release` stays behind
its gate). Modules: `core` (settings, readiness), `client` (the allow-listed store client), `signing` (SSHSIG),
`manifest` (the records), `service` (preview and publish), `commands`, `jobkinds`, `web`.
"""
# CR-WE-int: this package's __init__ imports nothing, so the store index can import `signing` (to verify a
# manifest's signature) without loading the publish service; phasegates.AUTOMATION_CODE `reads` relies on it.
