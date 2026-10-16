"""A direct `nanosoc-rm test-bump` on the service host never runs capabilities there (integration review
2026-10-16). With `[executor] host` in the SERVICE config, default_host() used to name a host, so a person's run on
the service host executed tier commands outside any sandbox. A config that lists executor agents is the service's:
its `host` is ignored, and an explicit --host is refused unless the local fallback is on."""
from __future__ import annotations

import pytest

from nanosoc_rm.capabilities import run as R
from nanosoc_rm.config import load_config
from nanosoc_rm.executor import tokens

pytestmark = [pytest.mark.requirement("D1"), pytest.mark.requirement("A6")]


def _cfg(tmp_path, agents: bool):
    p = tmp_path / ("svc.toml" if agents else "agent.toml")
    text = '[service]\ndata_dir = "%s"\n[executor]\nhost = "eda-1"\n' % (tmp_path / "data")
    if agents:
        text += ('[[executor.agents]]\nname = "eda-1"\ntoken_sha256 = "%s"\nlabels = ["vcs"]\n'
                 % tokens.digest(tokens.new_token()))
    p.write_text(text)
    return load_config(p)


def test_a_service_config_host_is_never_the_default(tmp_path):
    assert R.default_host(_cfg(tmp_path, agents=True), {}) is None
    assert R.default_host(_cfg(tmp_path, agents=False), {}) == "eda-1"          # an agent's own rm_config
    assert R.default_host(_cfg(tmp_path, agents=True), {"NANOSOC_RM_EXECUTOR_HOST": "eda-2"}) == "eda-2"


def test_an_explicit_host_on_the_service_config_is_refused(tmp_path):
    svc = _cfg(tmp_path, agents=True)
    with pytest.raises(R.TestBumpUsage, match="service's config"):
        R.test_bump("eth-main", "tidelink", "v1.0", config=svc, host="eda-1", environ={})
    # the local fallback (hermetic tests, a laptop) and the agent's own job (it sets the host variable) pass on
    for env in ({"NANOSOC_RM_LOCAL_TIERS": "1"}, {"NANOSOC_RM_EXECUTOR_HOST": "eda-1"}):
        with pytest.raises(Exception) as e:
            R.test_bump("eth-main", "tidelink", "v1.0", config=svc, host="eda-1", environ=env)
        assert "service's config" not in str(e.value)
