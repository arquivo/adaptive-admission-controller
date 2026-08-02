"""Unit tests for app.load_balancer.LeastLoadedLoadBalancer."""

from __future__ import annotations

import asyncio
import socket

from app.load_balancer import LeastLoadedLoadBalancer


def _ctx(source_ip: str | None = None):
    from app.interfaces import RequestContext

    return RequestContext(
        backend="test", path="/x", method="GET", arrival_time=0.0, source_ip=source_ip
    )


async def test_single_instance_degenerates_to_no_op():
    lb = LeastLoadedLoadBalancer(["http://a:8080"])
    instance = await lb.select(_ctx())
    assert instance.url == "http://a:8080"
    snapshot = lb.snapshot()
    assert snapshot == [type(snapshot[0])(url="http://a:8080", healthy=True, in_flight=1)]


async def test_selects_least_loaded_instance():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"])
    first = await lb.select(_ctx())
    second = await lb.select(_ctx())
    # Two selects with no release must land on different instances —
    # in_flight is incremented before releasing the lock.
    assert {first.url, second.url} == {"http://a:8080", "http://b:8080"}


async def test_release_decrements_in_flight():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"])
    instance = await lb.select(_ctx())
    await lb.release(instance, connect_failed=False)
    snapshot = {s.url: s.in_flight for s in lb.snapshot()}
    assert snapshot[instance.url] == 0


async def test_release_floors_at_zero_on_double_release(caplog):
    lb = LeastLoadedLoadBalancer(["http://a:8080"])
    instance = await lb.select(_ctx())
    await lb.release(instance, connect_failed=False)
    await lb.release(instance, connect_failed=False)  # double release — must not go negative
    snapshot = {s.url: s.in_flight for s in lb.snapshot()}
    assert snapshot[instance.url] == 0


async def test_concurrent_selects_spread_evenly_not_pile_onto_one():
    """Regression test for the atomicity requirement: concurrent selects
    with no intervening release must not all observe the same "least
    loaded" instance — select-then-increment must be one atomic step."""
    urls = ["http://a:8080", "http://b:8080", "http://c:8080", "http://d:8080"]
    lb = LeastLoadedLoadBalancer(urls)
    results = await asyncio.gather(*(lb.select(_ctx()) for _ in range(len(urls))))
    counts: dict[str, int] = {}
    for instance in results:
        counts[instance.url] = counts.get(instance.url, 0) + 1
    assert counts == dict.fromkeys(urls, 1)


async def test_connect_failed_release_marks_instance_down():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"])
    instance = await lb.select(_ctx())
    await lb.release(instance, connect_failed=True)
    snapshot = {s.url: s.healthy for s in lb.snapshot()}
    assert snapshot[instance.url] is False


async def test_unhealthy_instance_excluded_from_selection():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"])
    first = await lb.select(_ctx())
    await lb.release(first, connect_failed=True)
    # Every subsequent select must land on the still-healthy instance,
    # regardless of in-flight counts.
    for _ in range(5):
        selected = await lb.select(_ctx())
        assert selected.url != first.url


async def test_fails_open_when_every_instance_down():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"])
    instance_a = await lb.select(_ctx())
    await lb.release(instance_a, connect_failed=True)
    instance_b = await lb.select(_ctx())
    await lb.release(instance_b, connect_failed=True)
    # Both instances are now unhealthy — select() must still return one
    # rather than raising or blocking forever.
    selected = await lb.select(_ctx())
    assert selected.url in {"http://a:8080", "http://b:8080"}


async def test_probe_returns_false_on_connection_refused():
    # Bind and immediately close a socket to get a port nothing is
    # listening on, guaranteeing ECONNREFUSED rather than a real service.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    sock.close()

    lb = LeastLoadedLoadBalancer([f"http://127.0.0.1:{port}"], connect_timeout_seconds=1.0)
    assert await lb._probe(f"http://127.0.0.1:{port}") is False


async def test_health_check_loop_recovers_instance_via_real_socket():
    # A bound, listening (but never accepted) socket is enough for a TCP
    # handshake to complete purely at the kernel level — no asyncio accept
    # loop needed on the server side for `_probe`'s connect to succeed.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    url = f"http://{host}:{port}"
    try:
        lb = LeastLoadedLoadBalancer([url], connect_timeout_seconds=1.0)
        instance = await lb.select(_ctx())
        await lb.release(instance, connect_failed=True)
        assert lb.snapshot()[0].healthy is False

        await lb._check_down_instances()

        assert lb.snapshot()[0].healthy is True
    finally:
        srv.close()


async def test_sticky_session_pins_repeat_client_to_same_instance():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"], capacity_hint=lambda: 100)
    ctx = _ctx(source_ip="1.2.3.4")
    first = await lb.select(ctx)
    await lb.release(first, connect_failed=False)
    for _ in range(5):
        selected = await lb.select(ctx)
        await lb.release(selected, connect_failed=False)
        assert selected.url == first.url


async def test_sticky_disabled_falls_back_to_pure_least_loaded():
    lb = LeastLoadedLoadBalancer(
        ["http://a:8080", "http://b:8080"], sticky_enabled=False, capacity_hint=lambda: 100
    )
    ctx = _ctx(source_ip="1.2.3.4")
    first = await lb.select(ctx)
    second = await lb.select(ctx)
    # No sticky pin, so with no release in between, the second select must
    # land on the other, still-least-loaded instance.
    assert {first.url, second.url} == {"http://a:8080", "http://b:8080"}


async def test_sticky_pin_evicted_when_instance_becomes_unhealthy():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"], capacity_hint=lambda: 100)
    ctx = _ctx(source_ip="1.2.3.4")
    first = await lb.select(ctx)
    await lb.release(first, connect_failed=True)  # marks `first` down

    second = await lb.select(ctx)
    assert second.url != first.url
    await lb.release(second, connect_failed=False)

    # The new instance is now the pin for subsequent requests from this client.
    third = await lb.select(ctx)
    assert third.url == second.url


async def test_sticky_pin_expires_after_ttl_and_reroutes_away_from_stale_pin():
    clock = {"now": 0.0}
    lb = LeastLoadedLoadBalancer(
        ["http://a:8080", "http://b:8080"],
        sticky_ttl_seconds=10.0,
        capacity_hint=lambda: 100,
        now=lambda: clock["now"],
    )
    ctx = _ctx(source_ip="1.2.3.4")
    first = await lb.select(ctx)  # ties broken to "a"; a.in_flight -> 1
    assert first.url == "http://a:8080"

    # Anonymous filler traffic (no source_ip) never touches the sticky map.
    # Two fillers bring the state to a=2, b=1 (b=0 -> 1 on the first, then
    # the a/b=1/1 tie breaks to "a" -> 2 on the second), leaving "a" clearly
    # busier so the effect of TTL expiry below is observable.
    await lb.select(_ctx())  # a=1,b=0 -> picks b; b -> 1
    await lb.select(_ctx())  # a=1,b=1 tie -> picks a; a -> 2

    clock["now"] = 20.0  # past the 10s TTL
    second = await lb.select(ctx)
    # The stale pin (now expired) is dropped, so selection falls through to
    # plain least-loaded — "b" (1 in-flight) rather than the busier "a" (2).
    assert second.url == "http://b:8080"


async def test_sticky_pin_kept_when_all_instances_equally_at_fair_share():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"], capacity_hint=lambda: 4)
    ctx = _ctx(source_ip="1.2.3.4")
    pinned = await lb.select(ctx)  # ties broken to "a"; a.in_flight -> 1
    assert pinned.url == "http://a:8080"

    filler = _ctx()
    await lb.select(filler)  # a=1,b=0 -> picks b; b -> 1
    await lb.select(filler)  # a=1,b=1 tie -> picks a; a -> 2
    await lb.select(filler)  # a=2,b=1 -> picks b; b -> 2

    # fair_share = ceil(capacity_hint() / healthy_count) = ceil(4/2) = 2.
    # Both instances are now equally at fair share — "all servers at
    # capacity" per the spec — so the pin must NOT be evicted for no reason.
    selected = await lb.select(ctx)
    assert selected.url == "http://a:8080"


async def test_sticky_pin_evicted_and_replaced_by_less_loaded_alternative():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"], capacity_hint=lambda: 4)
    ctx = _ctx(source_ip="1.2.3.4")
    pinned = await lb.select(ctx)  # ties broken to "a"; a.in_flight -> 1
    assert pinned.url == "http://a:8080"

    filler = _ctx()
    await lb.select(filler)  # a=1,b=0 -> picks b; b -> 1
    await lb.select(filler)  # a=1,b=1 tie -> picks a; a -> 2

    # fair_share = ceil(4/2) = 2. "a" (the pin) is at fair share while "b"
    # (1) is strictly below it — the pin must be evicted and rerouted.
    rerouted = await lb.select(ctx)
    assert rerouted.url == "http://b:8080"

    # The new instance becomes this client's pin for subsequent requests.
    again = await lb.select(ctx)
    assert again.url == "http://b:8080"


async def test_sweep_removes_expired_sticky_entries():
    clock = {"now": 0.0}
    lb = LeastLoadedLoadBalancer(
        ["http://a:8080"], sticky_ttl_seconds=5.0, now=lambda: clock["now"]
    )
    ctx = _ctx(source_ip="1.2.3.4")
    instance = await lb.select(ctx)
    await lb.release(instance, connect_failed=False)
    assert len(lb._sticky) == 1

    clock["now"] = 100.0
    await lb._sweep_expired_sticky_entries(clock["now"])
    assert len(lb._sticky) == 0


async def test_snapshot_reports_sticky_count_per_instance():
    lb = LeastLoadedLoadBalancer(["http://a:8080", "http://b:8080"], capacity_hint=lambda: 100)
    await lb.select(_ctx(source_ip="1.1.1.1"))
    await lb.select(_ctx(source_ip="2.2.2.2"))
    counts = {s.url: s.sticky_count for s in lb.snapshot()}
    assert sum(counts.values()) == 2


async def test_backup_gets_no_traffic_while_primary_healthy_even_when_saturated():
    lb = LeastLoadedLoadBalancer(["http://a:8080"], backup_urls=["http://backup:8080"])
    # The lone primary is healthy but increasingly saturated — backups must
    # still not be touched, confirming the health-only (not capacity
    # overflow) trigger.
    for _ in range(5):
        selected = await lb.select(_ctx())
        assert selected.url == "http://a:8080"


async def test_all_primaries_down_routes_to_least_loaded_healthy_backup():
    lb = LeastLoadedLoadBalancer(
        ["http://a:8080"], backup_urls=["http://backup1:8080", "http://backup2:8080"]
    )
    primary = await lb.select(_ctx())
    await lb.release(primary, connect_failed=True)  # marks the only primary down

    first = await lb.select(_ctx())
    assert first.url in {"http://backup1:8080", "http://backup2:8080"}
    second = await lb.select(_ctx())
    assert second.url in {"http://backup1:8080", "http://backup2:8080"}
    # Least-loaded selection still applies across backups.
    assert first.url != second.url


async def test_fails_open_across_combined_set_when_primaries_and_backups_down():
    lb = LeastLoadedLoadBalancer(["http://a:8080"], backup_urls=["http://backup:8080"])
    primary = await lb.select(_ctx())
    await lb.release(primary, connect_failed=True)
    backup = await lb.select(_ctx())
    await lb.release(backup, connect_failed=True)

    selected = await lb.select(_ctx())
    assert selected.url in {"http://a:8080", "http://backup:8080"}


async def test_sticky_pin_on_backup_auto_migrates_back_to_primary_on_recovery():
    lb = LeastLoadedLoadBalancer(
        ["http://a:8080"], backup_urls=["http://backup:8080"], capacity_hint=lambda: 100
    )
    ctx = _ctx(source_ip="1.2.3.4")
    primary = await lb.select(ctx)
    assert primary.url == "http://a:8080"
    await lb.release(primary, connect_failed=True)  # fails the only primary over

    pinned_to_backup = await lb.select(ctx)
    assert pinned_to_backup.url == "http://backup:8080"
    await lb.release(pinned_to_backup, connect_failed=False)

    async with lb._lock:
        lb._healthy["http://a:8080"] = True  # simulate the primary recovering

    # The client's sticky pin on the backup is now stale — `_active_pool()`
    # returns primaries again, so the pin falls outside `candidates` and
    # selection reroutes back to the recovered primary automatically.
    migrated = await lb.select(ctx)
    assert migrated.url == "http://a:8080"


async def test_snapshot_reports_is_backup_flag_per_instance():
    lb = LeastLoadedLoadBalancer(["http://a:8080"], backup_urls=["http://backup:8080"])
    by_url = {s.url: s.is_backup for s in lb.snapshot()}
    assert by_url == {"http://a:8080": False, "http://backup:8080": True}
