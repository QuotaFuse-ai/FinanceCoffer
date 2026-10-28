"""Dependency probes rendered by ``GET /health``.

Every probe here is bounded and never raises: a dependency that is down must
not take the readiness endpoint down with it, because orchestration uses that
endpoint to decide whether to route traffic to this process.

All probes return the same shape -- an object with a ``status`` drawn from the
closed vocabulary below, ``elapsed_ms`` when a measurement was actually taken,
and whatever fields are specific to that probe (``etl`` carries
``lag_seconds`` / ``pending``). The aggregate ``status`` rule lives in the
route, so it can be revisited without touching a probe; the readiness table is
documented in ``docs/specs/financecoffer.md`` §6.2.1.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from financecoffer.core.config import Config

#: Budget for one socket operation (connect, then read). A ``PING`` performs at
#: most both, so a black-holed Redis is abandoned inside ~1 second.
REDIS_OP_TIMEOUT = 0.5

#: Budget for one MinIO socket operation (connect, then read). A
#: ``HeadBucket`` performs at most both, so a black-holed object store is
#: abandoned inside ~1 second -- the same arithmetic as ``redis_check``.
MINIO_OP_TIMEOUT = 0.5

#: Lag, in seconds, above which an ETL backlog counts as ``degraded``. It is the
#: threshold of the ``FinanceCofferEtlConsumeLagHigh`` alert
#: (``deploy/prometheus/rules.yml``: ``fc_etl_consume_lag_seconds > 60``) on
#: purpose: the readiness page and the alerting page must not disagree about
#: the same backlog. ``tests/unit/test_api.py`` keeps the two in sync.
ETL_LAG_DEGRADED_SECONDS = 60.0

#: The closed vocabulary every probe draws its ``status`` from.
OK = "ok"
DEGRADED = "degraded"
UNREACHABLE = "unreachable"
NOT_CONFIGURED = "not_configured"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _secret_text(value: Any) -> str:
    """Read a ``SecretStr`` (or plain string) as stripped text, ``""`` if unset."""

    if value is None:
        return ""
    get_secret = getattr(value, "get_secret_value", None)
    return str(get_secret() if callable(get_secret) else value).strip()


def database_check(service: Any) -> dict[str, Any]:
    """Probe PostgreSQL with the service's own ``SELECT 1`` (``ping``).

    ``status`` is ``ok`` when the warehouse answered, ``unreachable`` when a
    configured warehouse did not, and ``not_configured`` when no DSN was
    supplied at all -- a deployment fact, not an outage.
    """

    if not (getattr(service, "dsn", None) or service.config.db_url):
        return {"status": NOT_CONFIGURED}
    started = time.perf_counter()
    ok = service.ping()
    return {"status": OK if ok else UNREACHABLE, "elapsed_ms": _elapsed_ms(started)}


def embedding_check(service: Any) -> dict[str, Any]:
    """Probe the embedding endpoint through the service's readiness check.

    ``not_configured`` when ``FC_EMBEDDING_URL`` carries no scheme or host (the
    same condition :meth:`QueryService.embedding_ready` refuses to dial),
    ``unreachable`` when the endpoint did not pass its probe, otherwise ``ok``.
    """

    parts = urlsplit(str(service.config.embedding_url or ""))
    if not parts.scheme or not parts.netloc:
        return {"status": NOT_CONFIGURED}
    started = time.perf_counter()
    ok = service.embedding_ready()
    return {"status": OK if ok else UNREACHABLE, "elapsed_ms": _elapsed_ms(started)}


def redis_check(config: Config, *, timeout: float = REDIS_OP_TIMEOUT) -> dict[str, Any]:
    """``PING`` Redis once over ``config.redis_url`` and report the outcome.

    ``status`` is ``ok`` when the server answered, ``unreachable`` on any
    transport or protocol error, and ``not_configured`` when the optional
    ``redis`` client (``financecoffer[etl]``) is not installed: a base install
    never dials, so there is nothing to be unreachable.
    """

    started = time.perf_counter()
    status = OK
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:  # pragma: no cover - depends on install extras
        status = NOT_CONFIGURED
    else:
        client: Any = None
        try:
            client = redis.Redis.from_url(
                config.redis_url,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
                # redis-py retries ten times by default; a readiness probe must
                # issue exactly one PING to keep its own deadline.
                retry=Retry(NoBackoff(), 0),
            )
            client.ping()
        except Exception:  # noqa: BLE001 - redis-py raises several transport errors
            status = UNREACHABLE
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()
    return {"status": status, "elapsed_ms": _elapsed_ms(started)}


def minio_check(config: Config, *, timeout: float = MINIO_OP_TIMEOUT) -> dict[str, Any]:
    """``HeadBucket`` MinIO once over ``config.minio_endpoint`` and report it.

    MinIO is the cold archive's object store, and the archive is optional: the
    read path never touches it. So this probe answers "is the archive working",
    not "should traffic be routed here", and its verdict never moves the
    aggregate ``status``.

    ``status`` is ``ok`` when the bucket answered, ``unreachable`` on any
    transport, credential or ``HeadBucket`` error (a configured store we cannot
    use is not healthy), and ``not_configured`` when no credentials are set --
    ``FC_MINIO_ACCESS_KEY`` / ``FC_MINIO_SECRET_KEY`` ship empty (see
    ``.env.example``) and the adapter cannot sign a request, so there is
    nothing to dial. A base install without the ``s3`` extra is
    ``not_configured`` too, exactly like a missing redis client in
    :func:`redis_check`: the probe never dials, so nothing can be unreachable.
    """

    access = _secret_text(config.minio_access_key)
    secret = _secret_text(config.minio_secret_key)
    if not (access and secret):
        return {"status": NOT_CONFIGURED}
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:  # pragma: no cover - depends on install extras
        return {"status": NOT_CONFIGURED}

    endpoint = str(config.minio_endpoint or "").strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"

    started = time.perf_counter()
    status = OK
    client: Any = None
    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=timeout,
                read_timeout=timeout,
                # botocore retries five times by default; a readiness probe
                # must issue exactly one HeadBucket to keep its own deadline.
                retries={"max_attempts": 0},
            ),
        )
        client.head_bucket(Bucket=config.minio_bucket)
    except Exception:  # noqa: BLE001 - botocore raises transport and HTTP errors
        status = UNREACHABLE
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()
    return {"status": status, "elapsed_ms": _elapsed_ms(started)}


def etl_check(service: Any) -> dict[str, Any]:
    """Report the ETL queue depth and lag: ``{status, lag_seconds, pending}``.

    The numbers come from :meth:`QueryService.etl_queue`, which shares
    :func:`financecoffer.query.service.etl_queue_state` with
    ``fc_etl_consume_lag_seconds`` on ``/metrics`` -- one definition of
    consumption lag for both surfaces.

    The verdict: an empty queue is ``ok``; a backlog whose oldest waiting event
    is older than :data:`ETL_LAG_DEGRADED_SECONDS` is ``degraded``; a sample
    that could not be taken -- and a backlog whose rows carry no
    ``received_at``, so its age is unknown -- is ``unreachable`` / ``degraded``
    respectively. A backlog below the alert threshold is ``ok``: it is normal
    consumption in progress, and the alert itself does not fire there.

    There is no ``elapsed_ms``: the number this probe exists to report is the
    queue lag in seconds, and a socket round-trip for a single aggregate
    ``SELECT`` would not add anything an operator can act on.
    """

    try:
        snapshot = service.etl_queue()
    except Exception:  # noqa: BLE001 - a probe must never crash /health
        snapshot = None
    if snapshot is None:
        return {"status": UNREACHABLE, "lag_seconds": None, "pending": None}
    pending, lag = snapshot
    pending = int(pending)
    lag_seconds = None if lag is None else round(float(lag), 3)
    if pending <= 0:
        status = OK
    elif lag_seconds is None or lag_seconds > ETL_LAG_DEGRADED_SECONDS:
        status = DEGRADED
    else:
        status = OK
    return {"status": status, "lag_seconds": lag_seconds, "pending": pending}
