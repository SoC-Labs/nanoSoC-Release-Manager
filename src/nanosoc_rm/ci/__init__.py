"""CI trigger, fetch and verify (interfaces.md section 10; A2, A4, D2, D5, D7, D10).

    client.py   CIClient (the protocol), GitLabCI (httpx, the real one), FakeCI (the test seam)
    trigger.py  trigger(): build RM_REQUEST, store the run, POST trigger/pipeline (the one write of v1)
    fetch.py    fetch_pending(): read pending runs back from the CI API, verify, roll up, store; coverage()
    verify.py   RunRecord, Verdict, verify(): the seven checks of section 10, in order
    runners.py  runner_settings(): the hardened runner's settings against ci.runner_expected (D10)
    configs.py  the registered configurations, read from the registry checkout
    commands.py `nanosoc-rm t0 trigger|fetch|runs|items|coverage` and `nanosoc-rm runner`

The service never runs T0 itself (A4): nothing here imports nanosoc_rm.t0.runner. The roll-up comes from
nanosoc_rm.t0.gates and the budget from nanosoc_rm.t0.tiers, both imported only when a bundle is accepted.
"""
