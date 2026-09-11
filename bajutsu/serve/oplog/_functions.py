"""Configure the operational log's channels, and bind a request's correlation ids."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import IO, Any

from bajutsu.common.evidence.redaction import Redactor

from ._context_filter import _ContextFilter
from ._json_formatter import _JsonFormatter
from ._name_mask_filter import _NameMaskFilter
from ._shared import _CONTEXT_KEYS, _RESERVED, _actor, _job_id, _org, _request_id, _run_id
from ._text_formatter import _TextFormatter

# Stable event names so an SRE can grep/alert on `event=`. Keep additions deliberate.
EVENTS: frozenset[str] = frozenset(
    {
        "run.dispatched",
        "run.recorded",
        "run.soft_deleted",
        "run.restored",
        "run.purged",
        "oauth.login",
        "oauth.denied",
        # The OIDC exchange (BE-0414): a CI job traded its token for a machine session, or was
        # refused. The refusal is the one that matters operationally — every cause answers the
        # caller identically, so this log is the only place the actual reason is recorded, and a
        # run of them is how an operator sees a misconfigured `aud` or an unlisted repository.
        "oidc.exchange",
        "oidc.denied",
        # An org's membership is seeded from `orgs:` once and then owned by the database (BE-0375);
        # this reports a config entry whose membership fields are consequently no longer read. It
        # fires at a config rebind as well as at boot, so it can't ride on `server.startup_warning`.
        "org.membership.ignored",
        # The org backfill from a bound config could not reach the database; retried at the next
        # startup or rebind (BE-0375). Its own name, not the one above: an operator alerting on a
        # stale config entry must not also be paged by a transient database blip.
        "org.seed.failed",
        # A sign-in answered 503 because the org database could not be read (BE-0375). Its own name,
        # not `oauth.denied`: that event means a login was turned away, and an operator alerting on
        # its WARNING is watching for a total admin lockout, not for a transient store outage.
        "oauth.store_unavailable",
        # A user moved themselves between two orgs their memberships admit them to. Its own name
        # rather than a second `oauth.login`: no authentication happened, and an operator
        # reconstructing which tenant an actor was acting as needs the moves as well as the sign-ins.
        "org.switch",
        "server.startup_warning",
        "quota.rejected",
        "worker.job.started",
        "worker.job.finished",
        "artifact.upload.failed",
    }
)

# The two output formats the operator may select via BAJUTSU_LOG_FORMAT.
_FORMATS: frozenset[str] = frozenset({"json", "text"})

# Sensitive structured-field *names*, masked by name regardless of value (substring, case-folded).
_SENSITIVE_KEYS: tuple[str, ...] = (
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "api_key",
)
_run_redactor: ContextVar[Redactor | None] = ContextVar("bajutsu_run_redactor", default=None)


_SCHEMA_KEYS: frozenset[str] = _RESERVED | {key for key, _ in _CONTEXT_KEYS}


def new_request_id() -> str:
    """A short, unique id to mint at a request boundary."""
    return uuid.uuid4().hex


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return any(word in low for word in _SENSITIVE_KEYS)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """The structured fields a caller attached via ``extra=`` (excluding the schema's own keys)."""
    return {
        k: v for k, v in record.__dict__.items() if k not in _SCHEMA_KEYS and not k.startswith("_")
    }


def _mask_values(static: Redactor, text: str) -> str:
    """Mask known secret *values* in a fully rendered line: process-static, then run-scoped."""
    text = static.redact_text(text)
    run = _run_redactor.get()
    if run is not None:
        text = run.redact_text(text)
    return text


def make_handler(
    *, stream: IO[str] | None = None, fmt: str = "json", secrets: tuple[str, ...] = ()
) -> logging.Handler:
    """A stdout (or *stream*) handler wired with the correlation + redaction filters.

    Args:
        stream: Sink; defaults to ``sys.stdout`` (the 12-factor contract).
        fmt: ``json`` for the structured serve channel, ``text`` for the human-readable CLI.
        secrets: Process-lifetime secret values to mask wherever they appear.
    """
    normalized = fmt.lower()
    if normalized not in _FORMATS:
        raise ValueError(f"BAJUTSU_LOG_FORMAT must be one of json/text, got {fmt!r}")
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    static = Redactor(None, values=list(secrets))
    handler.setFormatter(_JsonFormatter(static) if normalized == "json" else _TextFormatter(static))
    handler.addFilter(_ContextFilter())
    handler.addFilter(_NameMaskFilter())
    # A namespaced marker on a stdlib Handler, not a reach into someone's private state: the
    # underscore keeps it from colliding with logging's own attributes (SLF001).
    handler._bajutsu_oplog = True  # type: ignore[attr-defined]  # noqa: SLF001  # reset() finds it
    return handler


def configure(
    *,
    fmt: str = "json",
    level: str = "INFO",
    secrets: tuple[str, ...] = (),
    stream: IO[str] | None = None,
) -> None:
    """Install the operational-logging handler as the root logger's sole sink (serve startup only).

    Takes over the root logger: any handler already installed is removed first, so no sibling
    handler can write an unredacted line past this one. A child logger that has its own
    non-propagating handler is that handler's concern, not this sink's.
    """
    if level.upper() not in logging.getLevelNamesMapping():
        raise ValueError(
            "BAJUTSU_LOG_LEVEL must be a standard level name "
            f"(DEBUG/INFO/WARNING/ERROR/CRITICAL), got {level!r}"
        )
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level.upper())
    root.addHandler(make_handler(stream=stream, fmt=fmt, secrets=secrets))


def reset() -> None:
    """Remove our handler(s) from the root logger — idempotent reconfigure / test teardown."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_bajutsu_oplog", False):
            root.removeHandler(handler)


def log_event(
    logger: logging.Logger, event: str, msg: str = "", *, level: int = logging.INFO, **fields: Any
) -> None:
    """Emit *msg* tagged with a stable ``event`` name (and any extra structured *fields*).

    The event must be registered in ``EVENTS`` — a typo at a call site is a programming error, not
    a new event, so it fails loudly here rather than silently fragmenting an SRE's grep/alerts.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown operational event {event!r}; add it to oplog.EVENTS first")
    logger.log(level, msg, extra={"event": event, **fields})


def bind_request(request_id: str) -> None:
    """Bind *request_id* at a request boundary without scoping it.

    Sound for a server that gives each request its own context: the stdlib ``ThreadingHTTPServer``
    (one thread per request) and an ASGI server (one asyncio ``Task`` per request) both bind on a
    per-request context copy. The binding is overwritten at the next request's entry rather than
    explicitly reset, which is why every ``do_GET`` / ``do_POST`` and the middleware rebind first
    thing. Use ``request_context`` where one context is reused across calls and a reset is needed.
    """
    _request_id.set(request_id)


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """Bind *request_id* for the duration of one request."""
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


@contextmanager
def job_context(*, job_id: str, org: str | None = None, actor: str | None = None) -> Iterator[None]:
    """Bind a worker job's ids while it runs."""
    job_tok, org_tok, actor_tok = _job_id.set(job_id), _org.set(org), _actor.set(actor)
    try:
        yield
    finally:
        _job_id.reset(job_tok)
        _org.reset(org_tok)
        _actor.reset(actor_tok)


@contextmanager
def run_context(run_id: str, *, secrets: tuple[str, ...] = ()) -> Iterator[None]:
    """Bind a run's id and, while it runs, a redactor seeded with its resolved ``${secrets.X}``."""
    id_token = _run_id.set(run_id)
    red_token = _run_redactor.set(Redactor(None, values=list(secrets)) if secrets else None)
    try:
        yield
    finally:
        _run_id.reset(id_token)
        _run_redactor.reset(red_token)
