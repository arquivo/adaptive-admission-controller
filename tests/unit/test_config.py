"""Unit tests for app.config: AACConfig parsing and failure modes."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AACConfig, AdaptiveBackendConfig, FixedBackendConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_real_backends_yaml_parses_cleanly():
    config = load_config(REPO_ROOT / "config" / "backends.yaml")
    assert len(config.backends) == 6
    assert len(config.scoring.default_penalties.user) == 2


def test_discriminated_union_assigns_correct_backend_types(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    by_name = {b.name: b for b in config.backends}
    assert isinstance(by_name["page-search-api"], AdaptiveBackendConfig)
    assert isinstance(by_name["pywb-framed"], FixedBackendConfig)


def test_adaptive_backend_missing_required_field_fails(base_config_dict):
    del base_config_dict["backends"][0]["target_p95_ms"]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_fixed_backend_missing_concurrency_limit_fails(base_config_dict):
    del base_config_dict["backends"][2]["concurrency_limit"]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_inverted_soft_hard_threshold_fails(base_config_dict):
    base_config_dict["scoring"]["default_penalties"]["ip"][0]["hard_threshold"] = 1
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_inverted_soft_hard_penalty_fails(base_config_dict):
    base_config_dict["scoring"]["default_penalties"]["ip"][0]["hard_penalty"] = 1
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_duplicate_backend_name_fails(base_config_dict):
    base_config_dict["backends"][1]["name"] = "page-search-api"
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_duplicate_path_prefix_fails(base_config_dict):
    base_config_dict["backends"][1]["match"]["path_prefix"] = "/textsearch"
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_unknown_override_backend_name_fails(base_config_dict):
    base_config_dict["scoring"]["overrides"]["does-not-exist"] = {
        "penalties": {"ip": {"soft_threshold": 1}}
    }
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_unknown_override_dimension_fails(base_config_dict):
    base_config_dict["scoring"]["overrides"]["page-search-api"]["penalties"]["bogus"] = {
        "soft_threshold": 1
    }
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_malformed_trusted_proxy_fails(base_config_dict):
    base_config_dict["ingress"]["trusted_proxies"] = ["not-an-ip"]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_invalid_ipv6_prefix_length_fails(base_config_dict):
    base_config_dict["scoring"]["ipv6_prefix_length"] = 64
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_empty_upstreams_list_fails(base_config_dict):
    base_config_dict["backends"][0]["upstreams"] = []
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_duplicate_upstream_url_fails(base_config_dict):
    base_config_dict["backends"][0]["upstreams"] = [
        {"url": "http://page-search-api-1:8080"},
        {"url": "http://page-search-api-1:8080"},
    ]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_duplicate_upstream_url_trailing_slash_fails(base_config_dict):
    base_config_dict["backends"][0]["upstreams"] = [
        {"url": "http://page-search-api-1:8080"},
        {"url": "http://page-search-api-1:8080/"},
    ]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_multiple_upstreams_parse_in_order(base_config_dict):
    base_config_dict["backends"][0]["upstreams"] = [
        {"url": "http://page-search-api-1:8080"},
        {"url": "http://page-search-api-2:8080"},
    ]
    config = AACConfig.model_validate(base_config_dict)
    backend = next(b for b in config.backends if b.name == "page-search-api")
    assert [str(u.url) for u in backend.upstreams] == [
        "http://page-search-api-1:8080/",
        "http://page-search-api-2:8080/",
    ]


def test_backup_upstreams_defaults_to_empty_list(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    assert config.backends[0].backup_upstreams == []


def test_duplicate_url_within_backup_upstreams_fails(base_config_dict):
    base_config_dict["backends"][0]["backup_upstreams"] = [
        {"url": "http://page-search-api-backup:8080"},
        {"url": "http://page-search-api-backup:8080"},
    ]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_url_duplicated_across_upstreams_and_backup_upstreams_fails(base_config_dict):
    base_config_dict["backends"][0]["backup_upstreams"] = [
        {"url": str(base_config_dict["backends"][0]["upstreams"][0]["url"])},
    ]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_url_duplicated_across_upstreams_and_backup_upstreams_trailing_slash_fails(
    base_config_dict,
):
    base_config_dict["backends"][0]["upstreams"] = [{"url": "http://page-search-api-1:8080"}]
    base_config_dict["backends"][0]["backup_upstreams"] = [
        {"url": "http://page-search-api-1:8080/"}
    ]
    with pytest.raises(ValidationError):
        AACConfig.model_validate(base_config_dict)


def test_multiple_backup_upstreams_parse_in_order(base_config_dict):
    base_config_dict["backends"][0]["backup_upstreams"] = [
        {"url": "http://page-search-api-backup-1:8080"},
        {"url": "http://page-search-api-backup-2:8080"},
    ]
    config = AACConfig.model_validate(base_config_dict)
    backend = next(b for b in config.backends if b.name == "page-search-api")
    assert [str(u.url) for u in backend.backup_upstreams] == [
        "http://page-search-api-backup-1:8080/",
        "http://page-search-api-backup-2:8080/",
    ]
