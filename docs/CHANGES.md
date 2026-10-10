# Change requests (PR2)

`REQUIREMENTS.md` v1.0 was frozen on 2026-09-30. From then on a requirement changes only through a numbered
change request, agreed the same way as the requirements (David approves). After gate 2 is approved, the
contracts in `docs/contracts/` and `schemas/` change the same way.

Each CR records: number, date raised, the requirement IDs (or contract sections) it affects, the change, why,
who agreed it and when. A CR that changes a schema also states whether the `schema` number bumps
(`docs/contracts/README.md` section 2).

| CR | Raised | Affects | Change | Why | Agreed |
|---|---|---|---|---|---|
| CR-6a | 2026-10-10 | D2, D11, interfaces §9 §11, catalogue schema | Catalogue `vendor_allowlist` entries with `severity: allow\|warn`; the no-arm-ip gate reports a `warn` item as WARN (amber), never FAIL; `allow` items are clean. First entries: DMA-230/PL230 files = allow; the other Arm-banner trees found 2026-10-02 = warn. | David's U1 decision: DMA-230 is cleared; the rest must be visible but not blocking. | David, 2026-10-10 (in chat) |

## Candidates raised at gate 2 (not CRs until David agrees one)

None open. GATE2 Q9 (decided 2026-10-01) keeps both hosts, so B5's done-when stands as written and the
candidate below is withdrawn.

- *(withdrawn)* **B5 wording.** B5 says "its component's default branch" and its done-when counts "the 11 commits". The 11
  were counted on each pin's pinned host. If B5 is judged on the canonical host only, rc4's list has 12 commits
  (question 9 in `GATE2.md`), and the done-when needs a CR. The gate-2 contract judges both hosts, which keeps
  the done-when as written.
