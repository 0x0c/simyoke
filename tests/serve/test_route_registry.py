"""The declarative serve route registry and its path matcher (BE-0253 Part 1).

These lock the registry's shape and the matcher's precedence so the stdlib handler's
registry-driven dispatch (Part 2) — and, later, the FastAPI generator (Part 3) — stay in
lockstep with the hand-enumerated route table below. The parity set is the guard that the
Part 2 refactor drops or adds nothing.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

from _shared import _serve, project, write_run

from bajutsu import serve as srv
from bajutsu.serve.routes import ROUTES, Route, match_route

# The full route surface both backends serve today, hand-enumerated from handler.py so an
# accidental drop/add during the registry migration fails loudly. Kept as (method, path) pairs
# using the canonical FastAPI-style template spelling stored in the registry.
_EXPECTED: frozenset[tuple[str, str]] = frozenset(
    {
        # --- GET: streaming / binary (off_loop) ---
        ("GET", "/api/jobs/{job_id}/events"),
        ("GET", "/runs/{run_id}/archive.zip"),
        ("GET", "/api/capture/screenshot"),
        ("GET", "/runs/{rel:path}"),
        # --- GET: index (off_loop) ---
        ("GET", "/"),
        ("GET", "/index.html"),
        # --- GET: uniform JSON reads ---
        ("GET", "/api/scenarios"),
        ("GET", "/api/targets"),
        ("GET", "/api/version"),
        ("GET", "/api/version/checkout"),
        ("GET", "/api/config"),
        ("GET", "/api/config/content"),
        ("GET", "/api/server"),
        ("GET", "/api/fs"),
        ("GET", "/api/apikey"),
        ("GET", "/api/claudecodetoken"),
        ("GET", "/api/gitcredential"),
        ("GET", "/api/secrets"),
        ("GET", "/api/provider"),
        ("GET", "/api/themecontract"),
        ("GET", "/api/ant/login"),
        ("GET", "/api/simulators"),
        ("GET", "/api/runs"),
        ("GET", "/api/orgs"),
        ("GET", "/api/metrics/targets"),
        ("GET", "/api/crawl/runs"),
        ("GET", "/api/runs/trash"),
        ("GET", "/api/artifacts/exists"),
        ("GET", "/api/compose/current"),
        ("GET", "/api/scenario"),
        ("GET", "/api/schema"),
        ("GET", "/api/jobs/{job_id}"),
        # --- GET: text responses (content_type) ---
        ("GET", "/metrics"),
        ("GET", "/stats"),
        ("GET", "/flakiness"),
        ("GET", "/usage"),
        ("GET", "/coverage"),
        # --- GET: oauth round-trip (off_loop) ---
        ("GET", "/api/oauth/login"),
        ("GET", "/api/oauth/callback"),
        # --- POST: raw-body uploads (off_loop) ---
        ("POST", "/api/upload"),
        ("POST", "/api/artifacts/config"),
        ("POST", "/api/artifacts/scenarios"),
        ("POST", "/api/artifacts/binary"),
        ("POST", "/api/scenarios/upload"),
        # --- POST: login (off_loop, sets cookie) ---
        ("POST", "/api/login"),
        ("POST", "/api/oidc/exchange"),
        # --- POST: uniform JSON actions ---
        ("POST", "/api/config"),
        ("POST", "/api/apikey"),
        ("POST", "/api/claudecodetoken"),
        ("POST", "/api/gitcredential"),
        ("POST", "/api/secrets"),
        ("POST", "/api/provider"),
        ("POST", "/api/theme"),
        ("POST", "/api/compose"),
        ("POST", "/api/ant/login"),
        ("POST", "/api/run"),
        ("POST", "/api/run-set"),
        ("POST", "/api/config/restore"),
        ("POST", "/api/org"),
        ("POST", "/api/orgs"),
        ("POST", "/api/orgs/{slug}/membership"),
        ("POST", "/api/record"),
        ("POST", "/api/crawl"),
        ("POST", "/api/triage"),
        ("POST", "/api/scenario"),
        ("POST", "/api/lint"),
        ("POST", "/api/scenario/apply-selector"),
        ("POST", "/api/scenario/enrich-apply"),
        ("POST", "/api/audit"),
        ("POST", "/api/codegen"),
        ("POST", "/api/approve"),
        ("POST", "/api/scenario/resolve"),
        ("POST", "/api/enrich"),
        ("POST", "/api/doctor"),
        ("POST", "/api/coverage"),
        ("POST", "/api/capture/start"),
        ("POST", "/api/capture/mark"),
        ("POST", "/api/capture/finish"),
        ("POST", "/api/capture/resolve"),
        ("POST", "/api/capture/close"),
        ("POST", "/api/worker/lease"),
        ("POST", "/api/worker/heartbeat"),
        ("POST", "/api/worker/result"),
        ("POST", "/api/worker/artifact-urls"),
        ("POST", "/api/worker/scenario-url"),
        ("POST", "/api/jobs/{job_id}/cancel"),
        ("POST", "/api/jobs/{job_id}/respond-human"),
        ("POST", "/api/runs/{run_id}/upload-urls"),
        ("POST", "/api/runs/bulk-delete"),
        ("POST", "/api/runs/{run_id}/restore"),
        # --- DELETE ---
        ("DELETE", "/api/crawl/runs/{run_id}"),
        ("DELETE", "/api/runs/{run_id}"),
        ("DELETE", "/api/orgs/{slug}"),
    }
)


def test_registry_covers_exactly_the_expected_surface() -> None:
    got = {(r.method, r.path) for r in ROUTES}
    assert got == _EXPECTED


def test_no_duplicate_method_path() -> None:
    pairs = [(r.method, r.path) for r in ROUTES]
    assert len(pairs) == len(set(pairs))


def test_methods_are_known() -> None:
    assert all(r.method in {"GET", "POST", "DELETE"} for r in ROUTES)


def test_off_loop_routes_have_no_handle_and_others_do() -> None:
    for r in ROUTES:
        if r.off_loop:
            assert r.handle is None, f"{r.method} {r.path} is off_loop but carries a handle"
        else:
            assert r.handle is not None, f"{r.method} {r.path} is uniform but has no handle"


def test_content_type_only_on_the_text_routes() -> None:
    typed = {(r.method, r.path) for r in ROUTES if r.content_type is not None}
    assert typed == {
        ("GET", "/metrics"),
        ("GET", "/stats"),
        ("GET", "/flakiness"),
        ("GET", "/usage"),
        ("GET", "/coverage"),
    }
    # Text routes are driven (uniform tuple), not off_loop.
    for r in ROUTES:
        if r.content_type is not None:
            assert not r.off_loop and r.handle is not None


def test_local_only_is_exactly_the_triaged_set() -> None:
    # PR-C's Part 4 triage: the FastAPI (hosted) generator deliberately skips these. `ant_login`
    # writes a machine-global credential and already 403s when hosted; the capture routes hold an
    # in-process Driver across start/mark/finish/resolve/close (and its screenshot), a single-process
    # model — this includes the live Edit picker's resolve/close (BE-0262). Every other route is
    # served by both backends.
    local = {(r.method, r.path) for r in ROUTES if r.local_only}
    assert local == {
        ("GET", "/api/ant/login"),
        ("POST", "/api/ant/login"),
        ("POST", "/api/capture/start"),
        ("POST", "/api/capture/mark"),
        ("POST", "/api/capture/finish"),
        ("POST", "/api/capture/resolve"),
        ("POST", "/api/capture/close"),
        ("GET", "/api/capture/screenshot"),
    }


def test_paths_are_balanced_templates() -> None:
    # Cheap forward-looking guard: every path must be a valid FastAPI template so PR-C can hand
    # route.path straight to app.<method>.
    for r in ROUTES:
        assert r.path.count("{") == r.path.count("}")
        assert r.path.startswith("/")


# --- matcher ---


def test_match_exact() -> None:
    result = match_route(ROUTES, "GET", "/api/config")
    assert result is not None
    route, params = result
    assert route.path == "/api/config"
    assert params == {}


def test_match_trailing_segment() -> None:
    result = match_route(ROUTES, "GET", "/api/jobs/abc123")
    assert result is not None
    route, params = result
    assert route.path == "/api/jobs/{job_id}"
    assert params == {"job_id": "abc123"}


def test_match_infix_with_suffix() -> None:
    result = match_route(ROUTES, "POST", "/api/orgs/acme/membership")
    assert result is not None
    route, params = result
    assert route.path == "/api/orgs/{slug}/membership"
    assert params == {"slug": "acme"}


def test_match_delete_trailing() -> None:
    result = match_route(ROUTES, "DELETE", "/api/orgs/acme")
    assert result is not None
    route, _ = result
    assert route.path == "/api/orgs/{slug}"


def test_match_respects_method() -> None:
    # /api/config exists as both GET and POST; the method selects which.
    get = match_route(ROUTES, "GET", "/api/config")
    post = match_route(ROUTES, "POST", "/api/config")
    assert get is not None and post is not None
    assert get[0].method == "GET"
    assert post[0].method == "POST"


def test_match_static_wins_over_template() -> None:
    # bulk-delete is a static POST path that must not be captured as a {run_id}.
    result = match_route(ROUTES, "POST", "/api/runs/bulk-delete")
    assert result is not None
    route, params = result
    assert route.path == "/api/runs/bulk-delete"
    assert params == {}


def test_no_match_returns_none() -> None:
    assert match_route(ROUTES, "GET", "/api/does-not-exist") is None
    # A bare-segment template must not match a path carrying an extra suffix.
    assert match_route(ROUTES, "GET", "/api/jobs/abc/extra/depth") is None


def test_match_path_template_is_greedy_remainder() -> None:
    result = match_route(ROUTES, "GET", "/runs/2026/07/report.html")
    assert result is not None
    route, params = result
    assert route.path == "/runs/{rel:path}"
    assert params == {"rel": "2026/07/report.html"}


def test_archive_zip_wins_over_generic_run_file() -> None:
    # The .../archive.zip route is listed before the greedy /runs/{rel:path}, so it matches first.
    result = match_route(ROUTES, "GET", "/runs/some-run/archive.zip")
    assert result is not None
    route, params = result
    assert route.path == "/runs/{run_id}/archive.zip"
    assert params == {"run_id": "some-run"}


def test_route_is_frozen() -> None:
    route = ROUTES[0]
    assert isinstance(route, Route)
    try:
        route.method = "PATCH"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Route should be frozen")


# --- handler coverage: every off_loop route must be intercepted bespoke ---

# Body that _dispatch_registry returns when route.handle is None (off_loop with no bespoke handler).
_REGISTRY_NOT_FOUND = b'{"error": "not found"}'


def _concrete_path(path: str) -> str:
    """Replace template slots with concrete values; use a real run id for /runs/* routes
    so the bespoke handlers return actual content instead of their own artifact-not-found 404."""
    path = re.sub(r"\{run_id\}", "r1", path)
    path = re.sub(r"\{rel:path\}", "r1/report.html", path)
    return re.sub(r"\{[^}]+\}", "no-such-id", path)


def _raw_request(method: str, url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=b"" if method != "GET" else None, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_off_loop_routes_are_intercepted_before_registry(tmp_path: Path) -> None:
    """Guard that every off_loop route in ROUTES has a matching bespoke handler in handler.py.

    off_loop routes carry handle=None; _dispatch_registry returns {"error": "not found"} for
    them. This test exercises each off_loop path over real HTTP and asserts the response is NOT
    that generic 404, confirming the bespoke handler ran. A future off_loop route added to ROUTES
    without a matching bespoke entry in handler.py would produce the registry 404 and fail here.
    """
    scn_dir, cfg, runs = project(tmp_path)
    # Real run dir so /runs/{run_id}/archive.zip and /runs/{rel:path} return content (not 404).
    write_run(runs, "r1", ok=True, scenarios=[("smoke", True)])
    state = srv.ServeState(scenarios_dir=scn_dir, config=cfg, runs_dir=runs, cwd=tmp_path)
    server, port = _serve(state)
    try:
        for route in ROUTES:
            if not route.off_loop:
                continue
            concrete = _concrete_path(route.path)
            _, body = _raw_request(route.method, f"http://127.0.0.1:{port}{concrete}")
            assert body.strip() != _REGISTRY_NOT_FOUND, (
                f"{route.method} {route.path} (→ {concrete}) returned the registry's generic 404 "
                f"— add a bespoke handler for this off_loop route before _dispatch_registry"
            )
    finally:
        server.shutdown()
        server.server_close()
