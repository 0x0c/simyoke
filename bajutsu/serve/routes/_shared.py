"""The path-template grammar a route's declaration is written in."""

from __future__ import annotations

from bajutsu.serve import operations as ops

from .route import Route

# Content type for the HTML dashboard routes (/stats, /flakiness, /usage, /coverage); /metrics uses the
# Prometheus type. Both drive `_text` instead of the JSON writer.
_HTML = "text/html; charset=utf-8"


# The route table. Order is load-bearing for the greedy `/runs/{rel:path}`, which must come after
# the more specific `/runs/{run_id}/archive.zip` it would otherwise swallow; the exact-segment-count
# matcher keeps every other pair order-independent. `off_loop` entries carry no handle — each
# backend dispatches them bespoke (see the module docstring).
ROUTES: tuple[Route, ...] = (
    # --- GET: streaming / binary (off_loop) ---
    Route("GET", "/api/jobs/{job_id}/events", off_loop=True),
    Route("GET", "/runs/{run_id}/archive.zip", off_loop=True),
    Route("GET", "/api/capture/screenshot", off_loop=True, local_only=True),
    Route("GET", "/runs/{rel:path}", off_loop=True),
    # --- GET: index (off_loop) ---
    Route("GET", "/", off_loop=True),
    Route("GET", "/index.html", off_loop=True),
    # --- GET: uniform JSON reads ---
    Route(
        "GET",
        "/api/scenarios",
        lambda state, ctx: ops.list_scenarios(
            state, ctx.query("target"), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "GET",
        "/api/targets",
        lambda state, ctx: ops.list_targets_payload(
            state, actor=ctx.actor(), session=ctx.session()
        ),
    ),
    # Running-tool identity (BE-0272): version is open; the Git checkout detail is admin-gated
    # (see `authz.required_role`) because a branch name can encode an in-progress topic.
    Route("GET", "/api/version", lambda _state, _ctx: ops.server_version()),
    Route("GET", "/api/version/checkout", lambda _state, _ctx: ops.server_checkout()),
    Route(
        "GET",
        "/api/config",
        lambda state, ctx: ops.config_info(state, actor=ctx.actor(), session=ctx.session()),
    ),
    Route(
        "GET",
        "/api/config/content",
        lambda state, ctx: ops.config_content(state, actor=ctx.actor(), session=ctx.session()),
    ),
    # The running server's resolved configuration + the bundled iOS runner state (BE-0318). Read-only
    # and open like /api/config; the operation withholds host paths when hosted (BE-0108).
    Route(
        "GET",
        "/api/server",
        lambda state, ctx: ops.server_settings(state, actor=ctx.actor(), session=ctx.session()),
    ),
    Route("GET", "/api/fs", lambda state, ctx: ops.browse_fs(state, ctx.query("dir"))),
    Route("GET", "/api/apikey", lambda state, ctx: ops.api_key_info(state, ctx.actor())),
    Route(
        "GET",
        "/api/claudecodetoken",
        lambda state, ctx: ops.claude_code_token_info(state, ctx.actor()),
    ),
    Route(
        "GET", "/api/gitcredential", lambda state, ctx: ops.git_credential_info(state, ctx.actor())
    ),
    # The scenario secrets the bound config declares (BE-0274): describe-only (masked, no value),
    # so — like the three credential reads above — it carries no role gate.
    Route(
        "GET",
        "/api/secrets",
        lambda state, ctx: ops.scenario_secrets_info(state, ctx.actor(), ctx.session()),
    ),
    Route("GET", "/api/provider", lambda state, ctx: ops.provider_info(state, ctx.actor())),
    Route("GET", "/api/themecontract", lambda state, _ctx: ops.get_theme_contract(state)),
    Route(
        "GET",
        "/api/ant/login",
        lambda state, _ctx: ops.ant_login_status(state),
        local_only=True,
    ),
    Route("GET", "/api/simulators", lambda state, _ctx: ops.simulators_payload(state)),
    Route(
        "GET",
        "/api/runs",
        lambda state, ctx: ops.runs_payload(
            state,
            actor=ctx.actor(),
            session=ctx.session(),
            scenario=ctx.query("scenario"),
            target=ctx.query("target"),
            label=ctx.query("label"),
            ran_target=ctx.query("ranTarget"),
        ),
    ),
    # The org roster an admin administers (BE-0375). Admin-gated in `authz.required_role`, which
    # needs its own early case for this path since two of the four routes here aren't POST.
    Route("GET", "/api/orgs", lambda state, ctx: ops.list_orgs_view(state, actor=ctx.actor())),
    Route(
        "GET",
        "/api/metrics/targets",
        lambda state, ctx: ops.target_metrics_view(state, actor=ctx.actor(), session=ctx.session()),
    ),
    Route(
        "GET",
        "/api/crawl/runs",
        lambda state, ctx: ops.crawl_runs_payload(state, actor=ctx.actor()),
    ),
    # Static path; `/api/runs/trash` (4 segments) matches the bare `/api/runs/{run_id}` template's
    # segment count, but match_route filters by HTTP method first and that template is DELETE-only,
    # so it can't shadow this GET route.
    Route(
        "GET",
        "/api/runs/trash",
        lambda state, ctx: ops.trashed_runs_payload(state, actor=ctx.actor()),
    ),
    Route(
        "GET",
        "/api/artifacts/exists",
        lambda state, ctx: ops.artifact_exists(
            state, ctx.query("kind"), ctx.query("sha256"), actor=ctx.actor()
        ),
    ),
    Route(
        "GET",
        "/api/compose/current",
        lambda state, ctx: ops.compose_current(state, actor=ctx.actor(), session=ctx.session()),
    ),
    Route(
        "GET",
        "/api/scenario",
        lambda state, ctx: ops.read_scenario(
            state,
            ctx.query("target"),
            ctx.query("path"),
            actor=ctx.actor(),
            session=ctx.session(),
            run_id=ctx.query("runId"),
            scenario_name=ctx.query("scenario"),
            structure=ctx.query("structure") == "1",
        ),
    ),
    Route("GET", "/api/schema", lambda _state, _ctx: ops.scenario_schema()),
    Route(
        "GET",
        "/api/jobs/{job_id}",
        lambda state, ctx: ops.job_view(state, ctx.path_param("job_id")),
    ),
    # --- GET: text responses (content_type) ---
    Route(
        "GET",
        "/metrics",
        lambda state, _ctx: ops.render_metrics(state),
        content_type=ops.PROMETHEUS_CONTENT_TYPE,
    ),
    Route(
        "GET",
        "/stats",
        lambda state, ctx: ops.stats_html(
            state, actor=ctx.actor(), session=ctx.session(), label=ctx.query("label")
        ),
        content_type=_HTML,
    ),
    Route(
        "GET",
        "/flakiness",
        lambda state, ctx: ops.flakiness_html(
            state, actor=ctx.actor(), session=ctx.session(), label=ctx.query("label")
        ),
        content_type=_HTML,
    ),
    Route(
        "GET",
        "/usage",
        lambda state, ctx: ops.usage_html(state, actor=ctx.actor(), session=ctx.session()),
        content_type=_HTML,
    ),
    # Unlike its three siblings the coverage map needs a target (and, for the evidence dimensions, a
    # run set and a crawl), so this route reads them from the query string — the linkable twin of the
    # view's `POST /api/coverage`.
    Route(
        "GET",
        "/coverage",
        lambda state, ctx: ops.coverage_html(
            state,
            ctx.query("target"),
            ctx.query("runs"),
            ctx.query("crawl"),
            actor=ctx.actor(),
            session=ctx.session(),
        ),
        content_type=_HTML,
    ),
    # --- GET: OAuth round-trip (off_loop) ---
    Route("GET", "/api/oauth/login", off_loop=True),
    Route("GET", "/api/oauth/callback", off_loop=True),
    # --- POST: raw-body uploads (off_loop) ---
    Route("POST", "/api/upload", off_loop=True),
    Route("POST", "/api/artifacts/config", off_loop=True),
    Route("POST", "/api/artifacts/scenarios", off_loop=True),
    Route("POST", "/api/artifacts/binary", off_loop=True),
    # A `.zip` of scenario files added directly to the bound config's target scope (BE-0340) — not
    # the content-addressed artifact leg above, which caches by hash for a compose bind.
    Route("POST", "/api/scenarios/upload", off_loop=True),
    # --- POST: login (off_loop, sets the session cookie) ---
    Route("POST", "/api/login", off_loop=True),
    # A CI job's way in (BE-0414): it presents the OIDC token its platform issued and gets a
    # machine session cookie back, so every later call in the pipeline is an ordinary session
    # request. `off_loop` for the same reason login is — it writes a `Set-Cookie`.
    Route("POST", "/api/oidc/exchange", off_loop=True),
    # --- POST: uniform JSON actions ---
    Route(
        "POST",
        "/api/config",
        # A `git` key selects the from-Git picker (BE-0063); `path` the local browser. Key presence
        # (not truthiness) routes, so an empty `git` still reaches the Git binder's 400.
        lambda state, ctx: (
            ops.bind_git_config(
                state,
                str(ctx.body().get("git") or ""),
                actor=ctx.actor(),
                session=ctx.session(),
            )
            if "git" in ctx.body()
            else ops.bind_config(
                state,
                str(ctx.body().get("path", "") or ""),
                actor=ctx.actor(),
                session=ctx.session(),
            )
        ),
    ),
    Route(
        "POST",
        "/api/apikey",
        lambda state, ctx: ops.set_api_key(
            state, str(ctx.body().get("value", "") or ""), ctx.actor()
        ),
    ),
    Route(
        "POST",
        "/api/claudecodetoken",
        lambda state, ctx: ops.set_claude_code_token(
            state, str(ctx.body().get("value", "") or ""), ctx.actor()
        ),
    ),
    Route(
        "POST",
        "/api/gitcredential",
        lambda state, ctx: ops.set_git_credential(
            state, str(ctx.body().get("value", "") or ""), ctx.actor()
        ),
    ),
    Route(
        "POST", "/api/provider", lambda state, ctx: ops.set_provider(state, ctx.body(), ctx.actor())
    ),
    # Set/clear a scenario-declared secret (BE-0274): admin-gated (see `authz._ADMIN_PATHS`),
    # rejects any name the bound config doesn't declare.
    Route(
        "POST",
        "/api/secrets",
        lambda state, ctx: ops.set_scenario_secret(state, ctx.body(), ctx.actor(), ctx.session()),
    ),
    Route(
        "POST", "/api/theme", lambda state, ctx: ops.upload_theme(state, ctx.body(), ctx.actor())
    ),
    Route(
        "POST",
        "/api/compose",
        lambda state, ctx: ops.bind_composition(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route("POST", "/api/ant/login", lambda state, _ctx: ops.ant_login(state), local_only=True),
    Route(
        "POST",
        "/api/run",
        lambda state, ctx: ops.start_run(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/run-set",
        lambda state, ctx: ops.start_run_set(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    # Rebind the org's remembered config (BE-0404 unit 1) — the recovery path for a replica that
    # never received the upload itself. Admin-gated like every bind: it materializes content the
    # deployment does not own and decides that content's build trust, which is the reason the gate
    # exists — not the pre-BE-0393 fact that a bind moved what the whole server served.
    Route(
        "POST",
        "/api/config/restore",
        lambda state, ctx: ops.restore_org_config(
            state, org=state.org_of(ctx.actor()), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/orgs",
        lambda state, ctx: ops.create_org(state, ctx.body(), actor=ctx.actor()),
    ),
    # Which org the caller acts as. Singular, and deliberately not under `/api/orgs/…`: everything
    # there administers *other* tenants and is admin-only, while this moves the caller between orgs
    # that already admit them and needs no role gate at all (`authz.required_role`).
    Route(
        "POST",
        "/api/org",
        lambda state, ctx: ops.set_active_org(state, ctx.body(), actor=ctx.actor()),
    ),
    # Replacing an org's membership is a whole-value write, which REST would spell PUT; it is a POST
    # because that is the only body-carrying verb both transports implement (BE-0375), and every
    # other whole-value write in `serve` — `/api/provider`, `/api/config` — is
    # already one. Widening the transports to a fourth verb is a cross-cutting change this item has
    # no other need for.
    Route(
        "POST",
        "/api/orgs/{slug}/membership",
        lambda state, ctx: ops.update_org_membership(
            state, ctx.path_param("slug"), ctx.body(), actor=ctx.actor()
        ),
    ),
    Route(
        "POST",
        "/api/record",
        lambda state, ctx: ops.start_record(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/crawl",
        lambda state, ctx: ops.start_crawl(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/triage",
        lambda state, ctx: ops.start_triage(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/scenario",
        lambda state, ctx: ops.save_scenario(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route("POST", "/api/lint", lambda _state, ctx: ops.lint_scenario(ctx.body())),
    Route(
        "POST",
        "/api/scenario/apply-selector",
        lambda _state, ctx: ops.apply_selector_edit(ctx.body()),
    ),
    Route(
        "POST",
        "/api/scenario/enrich-apply",
        lambda _state, ctx: ops.apply_enrichment_edit(ctx.body()),
    ),
    Route(
        "POST",
        "/api/audit",
        lambda state, ctx: ops.audit_scenario(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/codegen",
        lambda state, ctx: ops.generate_codegen(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/approve",
        lambda state, ctx: ops.approve_baseline(state, ctx.body(), actor=ctx.actor()),
    ),
    Route(
        "POST",
        "/api/scenario/resolve",
        lambda state, ctx: ops.resolve_scenario_pick(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/enrich",
        lambda state, ctx: ops.start_enrich(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/doctor",
        lambda state, ctx: ops.doctor_check(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/coverage",
        lambda state, ctx: ops.coverage_view(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
    ),
    Route(
        "POST",
        "/api/capture/start",
        lambda state, ctx: ops.start_capture(
            state, ctx.body(), actor=ctx.actor(), session=ctx.session()
        ),
        local_only=True,
    ),
    Route(
        "POST",
        "/api/capture/mark",
        lambda state, ctx: ops.mark_capture(state, ctx.body(), actor=ctx.actor()),
        local_only=True,
    ),
    Route(
        "POST",
        "/api/capture/finish",
        lambda state, ctx: ops.finish_capture(state, ctx.body(), actor=ctx.actor()),
        local_only=True,
    ),
    # Live step-picking for the Edit editor (BE-0262): resolve reuses the capture session's live
    # tree without actuating (pure authoring assist), close ends it without saving a scenario.
    Route(
        "POST",
        "/api/capture/resolve",
        lambda state, ctx: ops.resolve_capture_pick(state, ctx.body(), actor=ctx.actor()),
        local_only=True,
    ),
    Route(
        "POST",
        "/api/capture/close",
        lambda state, ctx: ops.close_capture(state, ctx.body(), actor=ctx.actor()),
        local_only=True,
    ),
    Route(
        "POST",
        "/api/worker/lease",
        lambda state, ctx: ops.worker_lease(
            state, ctx.body().get("worker_id", ""), ctx.body().get("capabilities")
        ),
    ),
    Route(
        "POST",
        "/api/worker/heartbeat",
        lambda state, ctx: ops.worker_heartbeat(
            state, ctx.body().get("worker_id", ""), ctx.body().get("job_id", "")
        ),
    ),
    Route("POST", "/api/worker/result", lambda state, ctx: ops.worker_result(state, ctx.body())),
    Route(
        "POST",
        "/api/worker/artifact-urls",
        lambda state, ctx: ops.worker_artifact_urls(state, ctx.body()),
    ),
    Route(
        "POST",
        "/api/worker/scenario-url",
        lambda state, ctx: ops.worker_scenario_url(state, ctx.body()),
    ),
    Route(
        "POST",
        "/api/jobs/{job_id}/cancel",
        lambda state, ctx: ops.cancel_job(state, ctx.path_param("job_id")),
    ),
    Route(
        "POST",
        "/api/jobs/{job_id}/respond-human",
        lambda state, ctx: ops.respond_human(state, ctx.path_param("job_id"), ctx.body()),
    ),
    Route(
        "POST",
        "/api/runs/{run_id}/upload-urls",
        lambda state, ctx: ops.generate_upload_urls(state, ctx.path_param("run_id"), ctx.body()),
    ),
    # Static path; today's exact-segment-count matcher can't confuse it with the `{run_id}`
    # templates below (4 segments vs. 5), but it's kept first for readability and in case a
    # same-length template is ever added here.
    Route(
        "POST",
        "/api/runs/bulk-delete",
        lambda state, ctx: ops.bulk_delete_runs(state, ctx.body(), actor=ctx.actor()),
    ),
    Route(
        "POST",
        "/api/runs/{run_id}/restore",
        lambda state, ctx: ops.restore_run(state, ctx.path_param("run_id"), actor=ctx.actor()),
    ),
    # --- DELETE ---
    Route(
        "DELETE",
        "/api/crawl/runs/{run_id}",
        lambda state, ctx: ops.delete_run(
            state,
            ctx.path_param("run_id"),
            purge=ctx.query("purge") == "true",
            actor=ctx.actor(),
        ),
    ),
    Route(
        "DELETE",
        "/api/runs/{run_id}",
        lambda state, ctx: ops.delete_run(
            state,
            ctx.path_param("run_id"),
            purge=ctx.query("purge") == "true",
            actor=ctx.actor(),
        ),
    ),
    Route(
        "DELETE",
        "/api/orgs/{slug}",
        lambda state, ctx: ops.delete_org(state, ctx.path_param("slug"), actor=ctx.actor()),
    ),
)
