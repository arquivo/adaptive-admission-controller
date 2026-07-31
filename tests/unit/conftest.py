"""Shared fixtures for config unit tests."""

import copy

import pytest

_BASE_CONFIG: dict = {
    "ingress": {
        "trusted_proxies": ["127.0.0.1", "::1"],
        "xff_trusted_hops": 1,
    },
    "geoip": {"db_path": "/var/lib/aac/GeoLite2-City.mmdb"},
    "observability": {"debug_headers": {"enabled": False}},
    "scoring": {
        "exempt_countries": ["PT"],
        "ipv6_prefix_length": 56,
        "base_scores": {
            "anonymous": 100,
            "researcher": 80,
            "service_account": 90,
            "internal": 100,
            "unknown": 50,
        },
        "score_clamp": {"min": -100, "max": 100},
        "default_penalties": {
            "ip": [
                {
                    "window_seconds": 10,
                    "soft_threshold": 10,
                    "hard_threshold": 30,
                    "soft_penalty": 10,
                    "hard_penalty": 40,
                }
            ],
            "net24": [
                {
                    "window_seconds": 60,
                    "soft_threshold": 50,
                    "hard_threshold": 200,
                    "soft_penalty": 10,
                    "hard_penalty": 40,
                }
            ],
            "net6": [
                {
                    "window_seconds": 60,
                    "soft_threshold": 50,
                    "hard_threshold": 200,
                    "soft_penalty": 10,
                    "hard_penalty": 40,
                }
            ],
            "asn": [
                {
                    "window_seconds": 60,
                    "soft_threshold": 200,
                    "hard_threshold": 1000,
                    "soft_penalty": 20,
                    "hard_penalty": 70,
                }
            ],
            "country": [
                {
                    "window_seconds": 300,
                    "soft_threshold": 500,
                    "hard_threshold": 2000,
                    "soft_penalty": 5,
                    "hard_penalty": 30,
                }
            ],
            "user": [
                {
                    "window_seconds": 60,
                    "soft_threshold": 50,
                    "hard_threshold": 200,
                    "soft_penalty": 5,
                    "hard_penalty": 40,
                },
                {
                    "window_seconds": 3600,
                    "soft_threshold": 500,
                    "hard_threshold": 2000,
                    "soft_penalty": 10,
                    "hard_penalty": 60,
                },
            ],
        },
        "overrides": {
            "page-search-api": {
                "penalties": {
                    "ip": {
                        "soft_threshold": 5,
                        "hard_threshold": 15,
                        "soft_penalty": 15,
                        "hard_penalty": 50,
                    }
                }
            },
            "image-search-api": {
                "penalties": {
                    "ip": {
                        "soft_threshold": 5,
                        "hard_threshold": 15,
                        "soft_penalty": 15,
                        "hard_penalty": 50,
                    }
                }
            },
        },
    },
    "backends": [
        {
            "name": "page-search-api",
            "upstream_url": "http://page-search-api:8080",
            "match": {"path_prefix": "/textsearch"},
            "controller": "adaptive",
            "min_concurrency": 20,
            "initial_concurrency": 100,
            "max_concurrency": 500,
            "target_p95_ms": 100,
            "timeout_rate_threshold": 0.05,
            "error_rate_threshold": 0.10,
            "connect_timeout_seconds": 5,
            "backend_timeout_seconds": 60,
            "queue_max_size": 5000,
            "queue_timeout_seconds": 300,
        },
        {
            "name": "image-search-api",
            "upstream_url": "http://image-search-api:8080",
            "match": {"path_prefix": "/imagesearch"},
            "controller": "adaptive",
            "min_concurrency": 20,
            "initial_concurrency": 100,
            "max_concurrency": 500,
            "target_p95_ms": 100,
            "timeout_rate_threshold": 0.05,
            "error_rate_threshold": 0.10,
            "connect_timeout_seconds": 5,
            "backend_timeout_seconds": 60,
            "queue_max_size": 5000,
            "queue_timeout_seconds": 300,
        },
        {
            "name": "pywb-framed",
            "upstream_url": "http://pywb-framed:8080",
            "match": {"path_prefix": "/wayback"},
            "controller": "fixed",
            "concurrency_limit": 100,
            "connect_timeout_seconds": 5,
            "backend_timeout_seconds": 60,
            "queue_max_size": 2000,
            "queue_timeout_seconds": 300,
        },
        {
            "name": "pywb-noframe",
            "upstream_url": "http://pywb-noframe:8081",
            "match": {"path_prefix": "/noFrame/replay"},
            "controller": "fixed",
            "concurrency_limit": 100,
            "connect_timeout_seconds": 5,
            "backend_timeout_seconds": 60,
            "queue_max_size": 2000,
            "queue_timeout_seconds": 300,
        },
        {
            "name": "pywb-patching",
            "upstream_url": "http://pywb-patching:8082",
            "match": {"path_prefix": "/noFrame/patching"},
            "controller": "fixed",
            "concurrency_limit": 10,
            "connect_timeout_seconds": 5,
            "backend_timeout_seconds": 60,
            "queue_max_size": 100,
            "queue_timeout_seconds": 300,
        },
        {
            "name": "pywb-archivepagenow",
            "upstream_url": "http://pywb-archivepagenow:8083",
            "match": {"path_prefix": "/save"},
            "controller": "fixed",
            "concurrency_limit": 5,
            "connect_timeout_seconds": 10,
            "backend_timeout_seconds": 120,
            "queue_max_size": 50,
            "queue_timeout_seconds": 300,
        },
    ],
}


@pytest.fixture
def base_config_dict() -> dict:
    return copy.deepcopy(_BASE_CONFIG)
