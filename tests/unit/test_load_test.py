"""Unit tests for the pure-function pieces of scripts/load_test.py — no real
network involved."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "load_test", REPO_ROOT / "scripts" / "load_test.py"
)
load_test = importlib.util.module_from_spec(_spec)
sys.modules["load_test"] = load_test
_spec.loader.exec_module(load_test)


def test_distribute_urls_single_url_repeats_it():
    assert load_test._distribute_urls(["http://a"], 5) == ["http://a"] * 5


def test_distribute_urls_round_robins_across_multiple():
    assert load_test._distribute_urls(["http://a", "http://b", "http://c"], 7) == [
        "http://a",
        "http://b",
        "http://c",
        "http://a",
        "http://b",
        "http://c",
        "http://a",
    ]


def test_distribute_urls_empty_total_requests():
    assert load_test._distribute_urls(["http://a", "http://b"], 0) == []


def test_ramp_capacity_no_ramp_returns_full_concurrency_immediately():
    assert load_test._ramp_capacity(concurrency=50, ramp_seconds=0, elapsed_seconds=0) == 50


def test_ramp_capacity_at_start_of_ramp_is_at_least_one():
    assert load_test._ramp_capacity(concurrency=50, ramp_seconds=10, elapsed_seconds=0) == 1


def test_ramp_capacity_midway_through_ramp_is_proportional():
    assert load_test._ramp_capacity(concurrency=100, ramp_seconds=10, elapsed_seconds=5) == 50


def test_ramp_capacity_after_ramp_completes_caps_at_full_concurrency():
    assert load_test._ramp_capacity(concurrency=50, ramp_seconds=10, elapsed_seconds=999) == 50


def test_ramp_capacity_never_exceeds_concurrency_past_full_elapsed():
    assert load_test._ramp_capacity(concurrency=10, ramp_seconds=10, elapsed_seconds=10) == 10
