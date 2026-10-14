"""Catalogue characterisation (interfaces.md section 23; CR-WF-catalogue, David 2026-10-14: front page approved).

    nanosoc_rm.catalogue.model     characterise / characterise_all / socs_without_forkable_project (pure)
    nanosoc_rm.catalogue.orgscan   org_scan(): anonymous org and group listing -> repo_seen observations
    nanosoc_rm.catalogue.preview   preview(registry, choices) -> the catalogue.yaml patch a person applies
    nanosoc_rm.catalogue.jobkind   the `org-scan` local job kind
    nanosoc_rm.catalogue.commands  `nanosoc-rm catalogue show|org-scan|preview`

This package never writes the registry. Its __init__ imports nothing, so the registry loader's rule 15 can import
the model without a cycle.

Requirements: A1, B1, H1.
"""
