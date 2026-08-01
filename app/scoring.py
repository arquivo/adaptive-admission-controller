"""Redis-backed scoring engine (`docs/implementation_plan.md` §4.2-§4.4).

Turns a classified `RequestContext` into the priority score
`PriorityScheduler.enqueue()` reads at enqueue time (`app/scheduler.py`) —
scoring must therefore run *before* enqueue, never lazily inside the
scheduler/worker.

Redis key schema (§4.3) only shows a `:{window_seconds}` suffix on the `user`
dimension, since it's the only one with more than one configured window by
default. But `DefaultPenalties` (`app.config`) allows *any* dimension to have
more than one window, so every dimension's key includes the suffix here —
otherwise two windows on the same dimension would collide on one Redis key
and stomp each other's count/TTL. This generalizes the `user` dimension's own
documented format to the other five rather than inventing a new one; with
today's single-window config for `ip`/`net24`/`net6`/`asn`/`country` the
resulting keys are one segment longer than §4.3's literal text but behave
identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import PenaltyConfig, ResolvedScoringConfig
    from app.interfaces import PenaltyStore, RequestContext

logger = logging.getLogger(__name__)


@dataclass
class ScoreBreakdown:
    base: int
    penalty_ip: int
    penalty_net24: int
    penalty_net6: int
    penalty_asn: int
    penalty_country: int
    penalty_user: int
    is_exempt: bool
    final: int


def _step_penalty(count: int, window: PenaltyConfig) -> int:
    if count >= window.hard_threshold:
        return window.hard_penalty
    if count >= window.soft_threshold:
        return window.soft_penalty
    return 0


async def _dimension_penalty(
    dim: str,
    value: str | None,
    backend: str,
    store: PenaltyStore,
    windows: list[PenaltyConfig],
) -> int:
    if value is None:
        return 0
    total = 0
    for window in windows:
        key = f"rl:{dim}:{value}:{backend}:{window.window_seconds}"
        count = await store.increment_and_get(key, window.window_seconds)
        total += _step_penalty(count, window)
    return total


async def ip_penalty(
    ip: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("ip", ip, backend, store, windows)


async def net24_penalty(
    subnet_24: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("net24", subnet_24, backend, store, windows)


async def net6_penalty(
    subnet_6: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("net6", subnet_6, backend, store, windows)


async def asn_penalty(
    asn: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("asn", asn, backend, store, windows)


async def country_penalty(
    country: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("country", country, backend, store, windows)


async def user_penalty(
    user_id: str | None, backend: str, store: PenaltyStore, windows: list[PenaltyConfig]
) -> int:
    return await _dimension_penalty("user", user_id, backend, store, windows)


def _clamp(value: int, min_score: int, max_score: int) -> int:
    return max(min_score, min(max_score, value))


async def calculate_score(
    ctx: RequestContext, store: PenaltyStore, config: ResolvedScoringConfig
) -> int:
    """§4.2, extended with the `net6` dimension the pseudocode omits (the
    approved plan's gap #2 — `RequestContext.subnet_6`/`net6_penalty` exist
    precisely so this can score IPv6 clients the same way `net24` scores
    IPv4 ones).

    Exempt countries (`config.exempt_countries`) skip the `net24`/`net6`/
    `asn`/`country` penalties' *contribution* to the score, but the
    underlying Redis counters still increment (for observability) — `ip` and
    `user` penalties always apply regardless of exemption.
    """
    base = config.base_scores[ctx.user_class]
    is_exempt = ctx.country in config.exempt_countries

    penalty_ip = await ip_penalty(ctx.source_ip, ctx.backend, store, config.penalties.ip)
    penalty_net24 = await net24_penalty(ctx.subnet_24, ctx.backend, store, config.penalties.net24)
    penalty_net6 = await net6_penalty(ctx.subnet_6, ctx.backend, store, config.penalties.net6)
    penalty_asn = await asn_penalty(ctx.asn, ctx.backend, store, config.penalties.asn)
    penalty_country = await country_penalty(
        ctx.country, ctx.backend, store, config.penalties.country
    )
    penalty_user = await user_penalty(ctx.user_id, ctx.backend, store, config.penalties.user)

    penalty = penalty_ip + penalty_user
    if not is_exempt:
        penalty += penalty_net24 + penalty_net6 + penalty_asn + penalty_country

    final = _clamp(base - penalty, config.score_clamp.min_score, config.score_clamp.max_score)

    ctx.score_breakdown = ScoreBreakdown(
        base=base,
        penalty_ip=penalty_ip,
        penalty_net24=penalty_net24,
        penalty_net6=penalty_net6,
        penalty_asn=penalty_asn,
        penalty_country=penalty_country,
        penalty_user=penalty_user,
        is_exempt=is_exempt,
        final=final,
    )
    logger.info(
        "score_breakdown",
        extra={
            "backend": ctx.backend,
            "user_class": ctx.user_class,
            "base": base,
            "penalty_ip": penalty_ip,
            "penalty_net24": penalty_net24,
            "penalty_net6": penalty_net6,
            "penalty_asn": penalty_asn,
            "penalty_country": penalty_country,
            "penalty_user": penalty_user,
            "country_exempt": is_exempt,
            "final": final,
        },
    )
    return final


class ScoreEngine:
    """Owns the one `PenaltyStore` + per-backend resolved scoring config map
    built once at startup; `app/main.py` holds a single instance
    (`app.state.score_engine`) and calls `score()` per request, before
    `scheduler.enqueue(...)`.
    """

    def __init__(self, store: PenaltyStore, configs: dict[str, ResolvedScoringConfig]) -> None:
        self._store = store
        self._configs = configs

    async def score(self, ctx: RequestContext) -> int:
        config = self._configs[ctx.backend]
        try:
            return await calculate_score(ctx, self._store, config)
        except Exception:
            # Redis-down fallback (`docs/decision_log.md` A5): admission must
            # keep working with the in-process capacity limiters unaffected,
            # so scoring degrades to "base score, zero penalties" rather than
            # propagating the failure into a 500. `/readyz` is the intended
            # signal for this condition (`app/health.py`), not the request
            # path — so this only logs, it never raises further.
            logger.warning(
                "scoring_redis_unavailable",
                extra={"backend": ctx.backend, "user_class": ctx.user_class},
                exc_info=True,
            )
            return _fail_open_score(ctx, config)


def _fail_open_score(ctx: RequestContext, config: ResolvedScoringConfig) -> int:
    base = config.base_scores[ctx.user_class]
    is_exempt = ctx.country in config.exempt_countries
    final = _clamp(base, config.score_clamp.min_score, config.score_clamp.max_score)
    ctx.score_breakdown = ScoreBreakdown(
        base=base,
        penalty_ip=0,
        penalty_net24=0,
        penalty_net6=0,
        penalty_asn=0,
        penalty_country=0,
        penalty_user=0,
        is_exempt=is_exempt,
        final=final,
    )
    return final
