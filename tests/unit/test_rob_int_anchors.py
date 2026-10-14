"""CR-ROB-int: the Wave E modules (publish, store, the store config) read whole-string ids, versions, digests and
paths with \\Z, as CR-ROB5-ids made every other module do (ROB-coverage-5): with `$`, a value with one trailing
newline passed and reached a store path, a manifest or a page."""
from __future__ import annotations

import pytest

from nanosoc_rm.publish import client as pclient, jobkinds, manifest, web as pweb
from nanosoc_rm.store import client as sclient, index, views

CASES = [
    (pclient.SAFE_SEGMENT, "nanosoc-releases"), (pclient.SAFE_NAME, "eth-rc4"), (pclient._PATH, "eth-rc4/1.0.0/a.zip"),
    (jobkinds._ID, "eth-rc4"), (jobkinds._SEMVER, "1.0.0"), (jobkinds._SHA256, "a" * 64), (jobkinds._LOGIN, "alice"),
    (manifest._STORE_PATH, "nanosoc-releases/eth-rc4/1.0.0"),
    (pweb._ID, "eth-rc4"), (pweb._SEMVER, "1.0.0"), (pweb._SHA256, "a" * 64),
    (sclient._REPO, "nanosoc-releases"), (index._SHA, "a" * 40), (index._MD5, "a" * 32),
    (views._HEX, "abcd"), (views._SHA, "a" * 7), (views._DRAFT_ID, "tapeout-1"),
]


@pytest.mark.parametrize("rx,good", CASES, ids=[c[0].pattern[:40] for c in CASES])
def test_a_trailing_newline_is_never_a_whole_value(rx, good):
    assert rx.match(good)
    assert not rx.match(good + "\n")


def test_a_tapeout_id_or_date_with_a_trailing_newline_is_refused(tmp_path):
    from nanosoc_rm.config import Config
    cfg = Config.__new__(Config)
    with pytest.raises(manifest.PublishUsage):
        manifest.tapeout_draft_path(cfg, "tapeout-1\n")
    assert manifest._date("2026-10-14") == "2026-10-14" and manifest._date("2026-10-14\n") is None
