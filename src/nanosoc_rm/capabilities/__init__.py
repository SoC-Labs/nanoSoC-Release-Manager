"""Test capabilities declared in each tree, composition across a superproject, and `test-bump`.

(1) Tier runs execute on an executor (a pull agent on the EDA host), never on the service host. (2) Each tree
declares what it can test IN THE REPO, `.nanosoc/capabilities.yaml`, read at the commit under test from the mirror;
the registry's tiers.yaml keeps only per-host site data (`hosts`). (3) Hooks warn; CI and the release manager's bump
gate block.

    manifest   parse and check one file; Reader: the file AT A COMMIT, from the mirror (never a working tree)
    plan       composition (compose: submodule capabilities by path), the run set of a bump, which host can run what
    run        `nanosoc-rm test-bump CONFIG PATH TO`: Now and After in one job, T0 as hygiene, one row per capability
    gate       the bump gate: no proposal opens a pull request without a passing test-bump of exactly that move
    view       the per-tree Capabilities page and `capabilities show`
    jobkind    the `test-bump` job kind (an executor kind: the service host never runs it)
    web        /capabilities pages and the "Test this bump" POST

Contracts: schemas/capabilities.schema.json, schemas/test-bump.schema.json, eligibility#/$defs/tiers_file `hosts`;
interfaces.md section 26.
"""
