"""Unit tests for app.registry — longest-prefix-wins backend matching (FR-011a)."""

from app.auth import JWTVerifier
from app.config import AACConfig
from app.geoip import GeoIPLookup
from app.registry import BackendPolicyRegistry, _prefix_matches


def _registry(config: AACConfig) -> BackendPolicyRegistry:
    geoip = GeoIPLookup(config.geoip.db_path)
    return BackendPolicyRegistry(config, geoip, JWTVerifier(config.auth))


def test_exact_prefix_matches_correct_backend(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    registry = _registry(config)

    policy = registry.match("/textsearch")
    assert policy is not None
    assert policy.config.name == "page-search-api"


def test_subpath_matches_correct_backend(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    registry = _registry(config)

    policy = registry.match("/wayback/20200101000000/http://example.com")
    assert policy is not None
    assert policy.config.name == "pywb-framed"


def test_longest_prefix_wins_over_shorter_overlapping_prefix(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    registry = _registry(config)

    # /noFrame/patching and /noFrame/replay both live under /noFrame; a
    # request to /noFrame/patching must resolve to pywb-patching, not
    # accidentally fall through to a shorter overlapping prefix.
    policy = registry.match("/noFrame/patching/20200101000000/http://example.com")
    assert policy is not None
    assert policy.config.name == "pywb-patching"

    policy = registry.match("/noFrame/replay/20200101000000/http://example.com")
    assert policy is not None
    assert policy.config.name == "pywb-noframe"


def test_unmatched_path_returns_none(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    registry = _registry(config)

    assert registry.match("/does-not-exist") is None


def test_similar_prefix_without_boundary_does_not_match():
    assert _prefix_matches("/textsearch", "/textsearch") is True
    assert _prefix_matches("/textsearch/foo", "/textsearch") is True
    assert _prefix_matches("/textsearchEVIL", "/textsearch") is False


def test_registry_caches_resolved_scoring_config_per_backend(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    registry = _registry(config)

    policy = registry.all_policies()["page-search-api"]
    assert policy.resolved_scoring.penalties.ip[0].soft_threshold == 5  # overridden value

