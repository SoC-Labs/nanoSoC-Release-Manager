"""The Wave C page shell (spec section 1, 7.1 unit S): rail, command-bar search, breadcrumbs, the visual tokens'
contrast, the core-router seam and its POST census, and the plug-in pages inside the new shell."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.responses import HTMLResponse, PlainTextResponse

from nanosoc_rm import auth
from nanosoc_rm.web import create_app, plugins, routers, shell
from webfix import make_registry, seed, write_config

CSS = Path(shell.__file__).resolve().parent / "static" / "rm.css"
PLUGIN_PAGES = ("/quality/", "/matrix/", "/versions/", "/bumps/", "/releases/")


@pytest.fixture
def site(service, registry_spy, tmp_path):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    client = TestClient(create_app(cfg, auth=auth.FakeMembership(login="alice")))
    client.get("/")                                     # reads the registry records into the store
    return dict(service, cfg=cfg, registry=reg, client=client)


def _load_config(site):
    from nanosoc_rm.config import load_config
    return load_config(site["cfg"])


# ----- rail -----------------------------------------------------------------------------------------------
def test_the_rail_lists_every_registered_configuration_with_a_text_chip(site):
    text = site["client"].get("/").text
    rows = re.findall(r'<a class="rail-item[^"]*" href="([^"]+)"[^>]*data-rail-configuration="([^"]+)">(.*?)</a>',
                      text, re.S)
    assert [r[1] for r in rows] == ["eth-rc4", "eth-main", "multicore-master"]
    for href, cid, inner in rows:
        assert href == "/configurations/%s" % cid
        chip = re.search(r'<span class="chip (c-[a-z]+)">([^<]+)</span>', inner)
        assert chip and chip.group(2).strip(), (cid, inner)                     # colour never alone
        assert 'class="dot' in inner                                            # and a dot beside it
    assert "never checked · " in text                                         # no compat result stored yet


def test_the_rail_shows_the_deciding_gate_once_a_result_is_stored(site):
    seed(site["cfg"], "observation.compat-result.example.jsonl")
    text = site["client"].get("/configurations/eth-rc4").text
    row = re.search(r'data-rail-configuration="eth-rc4">(.*?)</a>', text, re.S).group(1)
    assert '<span class="chip c-fail">FAIL</span>' in row
    assert "pins not reachable" in row or "Arm source in" in row or "pinned by ssh URL" in row, row
    assert re.search(r'class="rail-item is-cur" href="/configurations/eth-rc4" aria-current="page"', text)


def test_component_flags_come_from_stored_facts(site):
    cfg = _load_config(site)
    flags = shell.component_flags(cfg)
    assert ("SSH PIN", "c-fail") in flags["tidelink"]
    assert ("SPLIT", "c-split") in flags["nanosoc_gen"]
    assert all(t for _, t in sum(flags.values(), []))


# ----- search ---------------------------------------------------------------------------------------------
def test_search_finds_configurations_pins_and_nothing_for_nonsense(site):
    cfg = _load_config(site)
    hits = shell.search(cfg, "eth-")
    assert [h["label"] for h in hits if h["kind"] == "config"] == ["eth-rc4", "eth-main"]
    pin = [h for h in shell.search(cfg, "5e8") if h["kind"] == "commit"]
    assert pin and pin[0]["label"] == "5e8bdb5" and "tidelink in eth-rc4" in pin[0]["sub"]
    assert shell.search(cfg, "zz") == [] and shell.search(cfg, "e") == []
    comps = shell.search(cfg, "tide", scope="component")
    assert comps and {h["kind"] for h in comps} == {"component"}
    assert len(shell.search(cfg, "a", limit=3)) == 0 and len(shell.search(cfg, "nanosoc", limit=2)) <= 2


def test_search_is_a_get_page_and_an_htmx_fragment(site):
    c = site["client"]
    full = c.get("/search", params={"q": "eth-"})
    assert full.status_code == 200 and full.text.count("<html") == 1 and 'href="/configurations/eth-main"' in full.text
    frag = c.get("/search", params={"q": "eth-"}, headers={"HX-Request": "true"})
    assert frag.status_code == 200 and "<html" not in frag.text and 'role="listbox"' in frag.text
    none = c.get("/search", params={"q": "zzzz"}, headers={"HX-Request": "true"}).text
    assert 'Nothing matches "zzzz". The command bar also searches commits.' in none
    assert "/search" in {p for p, m, _ in routers.route_table(c.app) if "GET" in m}
    assert not {("POST", "/search")} & routers.writes(c.app)


def test_search_needs_a_member(service, tmp_path):
    reg = make_registry(tmp_path / "registry")
    cfg = write_config(service["cfg"], data=tmp_path / "data", github_url=service["github"].url, registry=reg)
    anon = TestClient(create_app(cfg))
    r = anon.get("/search?q=eth", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    cfg2 = write_config(tmp_path / "rm2.toml", data=tmp_path / "data", github_url=service["github"].url, registry=reg,
                        roles={"alice": ["member"]})
    no_role = TestClient(create_app(cfg2, auth=auth.FakeMembership(login="bob")))
    assert no_role.get("/search?q=eth").status_code == 403


def test_redact_turns_urls_into_host_names(site):
    cfg = _load_config(site)
    gh = site["github"].url
    msg = "fatal: could not read Username for '%s/SoC-Labs/x.git': terminal prompts disabled; git@gitlab.example.invalid:a/b" % gh
    out = shell.redact(msg, cfg)
    assert "<github>" in out and "<url>" in out and gh not in out and "example.invalid" not in out
    assert shell.redact("no url here", cfg) == "no url here"


def test_uniform_decides_a_hideable_column():
    assert shell.uniform([{"a": 1}, {"a": 1}], "a") and not shell.uniform([{"a": 1}, {"a": 2}], "a")
    assert not shell.uniform([], "a")


# ----- every page: one data age, breadcrumbs, the rail; plug-in pages too ---------------------------------------
def _html_get_routes(app):
    for path, methods, r in routers.route_table(app):
        if "GET" not in methods or getattr(getattr(r, "app", None), "directory", None):
            continue
        if path.startswith(("/api/", "/auth", "/jobs/")) or "{" in path:
            continue
        yield path


def test_every_page_has_one_data_age_breadcrumbs_and_the_rail(site):
    c = site["client"]
    seen = 0
    for path in _html_get_routes(c.app):
        r = c.get(path)
        if not r.headers.get("content-type", "").startswith("text/html") or r.status_code in (301, 302, 303, 307, 308):
            continue
        assert r.text.count("data-age-seconds=") == 1, path
        assert r.text.count('<nav class="crumbs" aria-label="Breadcrumb">') == 1, path
        assert "data-rail" in r.text, path
        seen += 1
    assert seen >= 15


def test_plugin_pages_render_inside_the_new_shell(site):
    c = site["client"]
    for url in PLUGIN_PAGES:
        r = c.get(url)
        assert r.status_code in (200, 503), (url, r.status_code)          # 503: this site has no stored walk
        assert r.text.count("<html") == 1 and "data-rail" in r.text, url
        crumbs = re.search(r'<nav class="crumbs" aria-label="Breadcrumb"><ol>(.*?)</ol></nav>', r.text, re.S).group(1)
        assert '<a href="/">Inbox</a>' in crumbs and 'aria-current="page"' in crumbs, (url, crumbs)


# ----- the core-router seam -----------------------------------------------------------------------------
def _fake_workspace(monkeypatch, name="inbox", answer="inbox here"):
    mod = types.ModuleType("nanosoc_rm.web." + name)

    def router(app):
        r = APIRouter()
        r.add_api_route("/configurations/{id}/tree", lambda id: PlainTextResponse("tree of " + id), methods=["GET"])
        if plugins.local_routes(app):
            r.add_api_route("/inbox/done", lambda: PlainTextResponse("done"), methods=["POST"])
        return r
    mod.router = router
    mod.dashboard = (lambda request: HTMLResponse(answer)) if answer else (lambda request: None)
    monkeypatch.setitem(sys.modules, "nanosoc_rm.web." + name, mod)
    return mod


def test_a_workspace_router_mounts_before_the_app_routes_and_takes_over_its_page(site, monkeypatch):
    _fake_workspace(monkeypatch)
    c = TestClient(create_app(site["cfg"], auth=auth.FakeMembership(login="alice")))
    assert c.get("/").text == "inbox here"                                     # the delegation seam
    assert c.get("/configurations/eth-rc4/tree").text == "tree of eth-rc4"     # matched before /configurations/{id}
    assert c.get("/configurations/eth-rc4").status_code == 200
    _fake_workspace(monkeypatch, answer=None)                                  # a module that declines
    c = TestClient(create_app(site["cfg"], auth=auth.FakeMembership(login="alice")))
    assert "data-configuration" in c.get("/").text                             # the older page answers


def test_no_wave_c_post_exists_in_a_github_ci_deployment(site, monkeypatch):
    _fake_workspace(monkeypatch)
    app = create_app(site["cfg"], auth=auth.FakeMembership(login="alice"))
    assert not app.state.rm.local_routes
    assert routers.WAVE_C_POSTS & routers.writes(app) == set()
    assert routers.writes(app) == {("POST", "/configurations/{id}/runs"), ("POST", "/api/v1/configurations/{id}/runs"),
                                   ("POST", "/auth/logout")}           # CR-ROB-auth: sign-out, accepts no result


def test_the_census_sees_a_workspace_post_in_a_local_deployment(site, monkeypatch, tmp_path):
    _fake_workspace(monkeypatch)
    app = create_app(site["cfg"], auth=auth.FakeMembership(login="alice"))
    app2 = create_app(site["cfg"], auth=auth.FakeMembership(login="alice"))
    app2.state.rm.local_routes = True
    routers.mount(app2)                                                        # a local deployment's mount
    assert ("POST", "/inbox/done") in routers.writes(app2) and ("POST", "/inbox/done") not in routers.writes(app)


# ----- the visual tokens: AA contrast in both themes --------------------------------------------------------
def _block(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert m, selector
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", m.group(1)))


def _themes():
    css = CSS.read_text()
    light = _block(css, ":root")
    dark_forced = _block(css, ':root[data-theme="dark"]')
    media = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\n  \}",
                      css, re.S)
    assert media, "the system-dark block is missing"
    dark_media = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", media.group(1)))
    assert dark_media == dark_forced, "the two dark blocks differ"

    def resolve(tokens):
        out = {}
        for k, v in tokens.items():
            while v.startswith("var("):
                v = tokens[v[4:-1].strip()]
            out[k] = v.strip()
        return out
    return css, {"light": resolve(light), "dark": resolve(dict(light, **dark_forced))}


def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _rule(css, cls):
    m = re.search(r"(^|\n)[^{}\n]*\.%s\b[^{}]*\{([^}]*)\}" % re.escape(cls), css)
    assert m, cls
    props = dict(re.findall(r"([a-z-]+):\s*([^;]+);", m.group(2)))
    return props


TONES = ("c-fail", "c-warn", "c-pass", "c-waived", "c-notrun", "c-infra", "c-ink", "c-split", "c-neutral", "c-run")


def test_every_tone_has_aa_text_contrast_in_light_and_dark():
    css, themes = _themes()
    measured = {}
    for theme, t in themes.items():
        surfaces = [t["--panel"], t["--panel-2"], t["--bg"]]
        for cls in TONES:
            p = _rule(css, cls)
            fg = t[re.match(r"var\((--[a-z0-9-]+)\)", p["color"]).group(1)]
            bg_val = p.get("background", "transparent")
            bgs = surfaces if bg_val == "transparent" else [t[re.match(r"var\((--[a-z0-9-]+)\)", bg_val).group(1)]]
            worst = min(ratio(fg, bg) for bg in bgs)
            measured[(theme, cls)] = round(worst, 2)
            assert worst >= 4.5, (theme, cls, fg, bgs, worst)
        for fg, bg in (("--fg", "--panel"), ("--fg", "--bg"), ("--muted", "--panel"), ("--muted", "--panel-2"),
                       ("--muted", "--bg"), ("--link", "--panel"), ("--link", "--bg"), ("--accent-fg", "--accent"),
                       ("--ink-fg", "--ink"), ("--ink-muted", "--ink"), ("--ink-fg", "--ink-2"), ("--code-fg", "--code-bg"),
                       ("--red", "--red-bg"), ("--amber", "--amber-bg"), ("--green", "--green-bg"),
                       ("--diff-del-fg", "--diff-del-bg"), ("--diff-ins-fg", "--diff-ins-bg"), ("--hl-fg", "--hl-bg"),
                       ("--warn-fg", "--warn-bg"), ("--info-fg", "--info-bg")):
            assert ratio(t[fg], t[bg]) >= 4.5, (theme, fg, bg, ratio(t[fg], t[bg]))
        for dot in ("--fail-dot", "--pass-dot", "--notrun-line", "--split-fg", "--focus", "--control-line"):
            for bg in ("--panel", "--panel-2"):
                assert ratio(t[dot], t[bg]) >= 3, (theme, dot, bg, ratio(t[dot], t[bg]))
        assert ratio(t["--focus-on-ink"], t["--ink"]) >= 3, theme
    assert len(measured) == 2 * len(TONES)


def test_every_old_class_is_still_styled_for_the_plugin_pages():
    css = CSS.read_text()
    for cls in ("state-red", "state-amber", "state-green", "state-grey", "state-info", "pill", "badge", "banner",
                "empty", "note", "scroll", "prim", "ghost", "seg", "chip", "dot", "rail-item", "hit", "card", "stub",
                "trow", "is-deciding", "diffline", "excerpt", "sr", "cmdbox", "code"):
        assert re.search(r"\.%s\b" % re.escape(cls), css), cls
    assert ":focus-visible" in css and "min-height: 44px" in css


def test_the_fonts_are_served_locally_with_their_licence():
    fonts = CSS.parent / "fonts"
    css = CSS.read_text()
    for name in re.findall(r'url\("fonts/([^"]+)"\)', css):
        assert (fonts / name).is_file() and (fonts / name).read_bytes()[:4] == b"wOF2", name
    assert "SIL Open Font License" in (fonts / "OFL.txt").read_text()
    assert "googleapis" not in css and "@import" not in css
