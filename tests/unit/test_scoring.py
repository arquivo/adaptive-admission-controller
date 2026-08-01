"""Unit tests for app.scoring — per-dimension step penalties, multi-window
summing, exempt-country handling, score clamping, and end-to-end
`calculate_score`/`ScoreEngine` (`docs/implementation_plan.md` §4.2-§4.4).
"""

from __future__ import annotations

from app.config import DefaultPenalties, PenaltyConfig, ResolvedScoringConfig, ScoreClamp
from app.interfaces import RequestContext
from app.scoring import ScoreEngine, calculate_score
from tests.unit.fakes import FakePenaltyStore


def _window(**overrides) -> PenaltyConfig:
    defaults = {
        "window_seconds": 60,
        "soft_threshold": 10,
        "hard_threshold": 30,
        "soft_penalty": 10,
        "hard_penalty": 40,
    }
    return PenaltyConfig(**{**defaults, **overrides})


def _resolved_config(**dim_overrides) -> ResolvedScoringConfig:
    dims = {dim: [_window()] for dim in ("ip", "net24", "net6", "asn", "country", "user")}
    dims.update(dim_overrides)
    return ResolvedScoringConfig(
        base_scores={
            "anonymous": 100,
            "researcher": 80,
            "service_account": 90,
            "internal": 100,
            "unknown": 50,
        },
        exempt_countries=["PT"],
        ipv6_prefix_length=56,
        score_clamp=ScoreClamp(min=-100, max=100),
        penalties=DefaultPenalties(**dims),
    )


def _ctx(**overrides) -> RequestContext:
    defaults = dict(
        backend="test-backend",
        path="/x",
        method="GET",
        arrival_time=0.0,
        source_ip="1.2.3.4",
        user_class="anonymous",
        subnet_24="1.2.3.0/24",
        subnet_6=None,
        asn="64500",
        country="US",
        user_id=None,
    )
    return RequestContext(**{**defaults, **overrides})


async def test_below_soft_threshold_no_penalty():
    config = _resolved_config()
    store = FakePenaltyStore()
    ctx = _ctx()

    score = await calculate_score(ctx, store, config)

    assert score == 100  # base=100, no hits yet -> count=1, below soft_threshold=10
    assert ctx.score_breakdown.penalty_ip == 0


async def test_soft_threshold_applies_soft_penalty():
    config = _resolved_config(ip=[_window(soft_threshold=1, hard_threshold=30)])
    store = FakePenaltyStore()
    ctx = _ctx()

    score = await calculate_score(ctx, store, config)

    assert ctx.score_breakdown.penalty_ip == 10
    assert score == 90


async def test_hard_threshold_applies_hard_penalty():
    config = _resolved_config(ip=[_window(soft_threshold=1, hard_threshold=1)])
    store = FakePenaltyStore()
    ctx = _ctx()

    score = await calculate_score(ctx, store, config)

    assert ctx.score_breakdown.penalty_ip == 40
    assert score == 60


async def test_multi_window_penalties_are_summed():
    """`user` has a 60s burst + 3600s sustained window (decision log A7) —
    both windows' step results must be summed, not just the first."""
    windows = [
        _window(window_seconds=60, soft_threshold=1, hard_threshold=1, hard_penalty=40),
        _window(window_seconds=3600, soft_threshold=1, hard_threshold=1, hard_penalty=60),
    ]
    config = _resolved_config(user=windows)
    store = FakePenaltyStore()
    ctx = _ctx(user_id="researcher-1", user_class="researcher")

    score = await calculate_score(ctx, store, config)

    assert ctx.score_breakdown.penalty_user == 100  # 40 + 60
    assert score == -20  # base 80 - 100, not clamped yet (>= -100)


async def test_exempt_country_skips_contribution_but_still_increments_counters():
    config = _resolved_config(
        net24=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
        net6=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
        asn=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
        country=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
    )
    store = FakePenaltyStore()
    ctx = _ctx(country="PT")  # exempt

    score = await calculate_score(ctx, store, config)

    breakdown = ctx.score_breakdown
    assert breakdown.is_exempt is True
    assert score == 100  # net24/net6/asn/country penalties computed but not applied
    # Counters were still incremented for observability, even though exempt.
    assert store.counts[f"rl:net24:{ctx.subnet_24}:{ctx.backend}:60"] == 1
    assert store.counts[f"rl:country:{ctx.country}:{ctx.backend}:60"] == 1


async def test_exempt_country_does_not_skip_ip_or_user_penalty():
    config = _resolved_config(
        ip=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
        user=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)],
    )
    store = FakePenaltyStore()
    ctx = _ctx(country="PT", user_id="u1")

    score = await calculate_score(ctx, store, config)

    assert score == 20  # 100 - 40 (ip) - 40 (user), country exemption doesn't touch these


async def test_score_clamped_to_min():
    config = _resolved_config(ip=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=1000)])
    store = FakePenaltyStore()
    ctx = _ctx()

    score = await calculate_score(ctx, store, config)

    assert score == -100


async def test_score_clamped_to_max():
    config = _resolved_config()
    config.base_scores["anonymous"] = 1000
    store = FakePenaltyStore()
    ctx = _ctx()

    score = await calculate_score(ctx, store, config)

    assert score == 100


async def test_subnet6_penalty_applies_when_ipv4_subnet24_absent():
    """gap #2 from the approved plan: an IPv6 client's `net24` stays `None`
    and contributes 0, while `net6` scores instead."""
    config = _resolved_config(net6=[_window(soft_threshold=1, hard_threshold=1, hard_penalty=40)])
    store = FakePenaltyStore()
    ctx = _ctx(source_ip="2001:db8::1", subnet_24=None, subnet_6="2001:db8::/56")

    score = await calculate_score(ctx, store, config)

    assert ctx.score_breakdown.penalty_net24 == 0
    assert ctx.score_breakdown.penalty_net6 == 40
    assert score == 60


async def test_score_engine_dispatches_by_backend():
    store = FakePenaltyStore()
    engine = ScoreEngine(store, {"test-backend": _resolved_config()})
    ctx = _ctx()

    score = await engine.score(ctx)

    assert score == 100
