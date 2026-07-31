"""Process settings (env-driven) and the YAML backend-policy configuration schema.

Two distinct layers, intentionally not conflated:
  - `Settings` (pydantic-settings, `AAC_`-prefixed env vars): where to find
    things (config file path, Redis URL).
  - `AACConfig` (plain Pydantic `BaseModel`): the parsed content of
    `config/backends.yaml`, re-validated fresh on every `load_config()` call.
"""

from __future__ import annotations

import copy
import ipaddress
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    UrlConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Process settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AAC_")

    config_path: Path = Path("config/backends.yaml")
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# YAML policy schema
# ---------------------------------------------------------------------------

HttpUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["http", "https"])]


class IngressConfig(BaseModel):
    trusted_proxies: list[str]
    xff_trusted_hops: int = Field(default=1, ge=1)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_networks(cls, value: list[str]) -> list[str]:
        for entry in value:
            ipaddress.ip_network(entry, strict=False)  # raises ValueError on typos
        return value


class GeoIPConfig(BaseModel):
    db_path: Path


class DebugHeadersConfig(BaseModel):
    enabled: bool = False


class ObservabilityConfig(BaseModel):
    debug_headers: DebugHeadersConfig = DebugHeadersConfig()


class PenaltyConfig(BaseModel):
    window_seconds: int = Field(gt=0)
    soft_threshold: int = Field(ge=0)
    hard_threshold: int = Field(ge=0)
    soft_penalty: int = Field(ge=0)
    hard_penalty: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_ordering(self) -> PenaltyConfig:
        if self.hard_threshold < self.soft_threshold:
            raise ValueError("hard_threshold must be >= soft_threshold")
        if self.hard_penalty < self.soft_penalty:
            raise ValueError("hard_penalty must be >= soft_penalty")
        return self


_PENALTY_DIMENSIONS = ("ip", "net24", "net6", "asn", "country", "user")


class DefaultPenalties(BaseModel):
    ip: list[PenaltyConfig] = Field(min_length=1)
    net24: list[PenaltyConfig] = Field(min_length=1)
    net6: list[PenaltyConfig] = Field(min_length=1)
    asn: list[PenaltyConfig] = Field(min_length=1)
    country: list[PenaltyConfig] = Field(min_length=1)
    user: list[PenaltyConfig] = Field(min_length=1)


class ScoreClamp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    min_score: int = Field(alias="min")
    max_score: int = Field(alias="max")


class BackendOverride(BaseModel):
    penalties: dict[str, dict[str, int]] = Field(default_factory=dict)
    base_scores: dict[str, int] = Field(default_factory=dict)

    @field_validator("penalties")
    @classmethod
    def _validate_dimensions(cls, value: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        for dim in value:
            if dim not in _PENALTY_DIMENSIONS:
                raise ValueError(f"unknown penalty dimension override: {dim!r}")
        return value


class ScoringConfig(BaseModel):
    exempt_countries: list[str] = Field(default_factory=list)
    ipv6_prefix_length: Literal[48, 56] = 56
    base_scores: dict[str, int]
    score_clamp: ScoreClamp
    default_penalties: DefaultPenalties
    overrides: dict[str, BackendOverride] = Field(default_factory=dict)


class ResolvedScoringConfig(BaseModel):
    """A single backend's scoring config after `scoring.overrides.<backend>`
    has been deep-merged into `scoring.default_penalties`/`base_scores`.

    Computed once at startup (see `resolve_scoring_config` below) and cached
    on the owning `BackendPolicy` — never recomputed on the request path.
    """

    base_scores: dict[str, int]
    exempt_countries: list[str]
    score_clamp: ScoreClamp
    penalties: DefaultPenalties


class MatchConfig(BaseModel):
    path_prefix: str


class _BackendCommon(BaseModel):
    name: str
    upstream_url: HttpUrl
    match: MatchConfig
    connect_timeout_seconds: float = Field(gt=0)
    backend_timeout_seconds: float = Field(gt=0)
    queue_max_size: int = Field(gt=0)
    queue_timeout_seconds: float = Field(gt=0)


class FixedBackendConfig(_BackendCommon):
    controller: Literal["fixed"]
    concurrency_limit: int = Field(gt=0)


class AdaptiveBackendConfig(_BackendCommon):
    controller: Literal["adaptive"]
    min_concurrency: int = Field(gt=0)
    initial_concurrency: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    target_p95_ms: float = Field(gt=0)
    timeout_rate_threshold: float = Field(gt=0, le=1)
    error_rate_threshold: float = Field(gt=0, le=1)


BackendConfig = Annotated[
    FixedBackendConfig | AdaptiveBackendConfig, Field(discriminator="controller")
]


class AACConfig(BaseModel):
    ingress: IngressConfig
    geoip: GeoIPConfig
    observability: ObservabilityConfig = ObservabilityConfig()
    scoring: ScoringConfig
    backends: list[BackendConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_backends(self) -> AACConfig:
        names = [b.name for b in self.backends]
        if len(names) != len(set(names)):
            raise ValueError("duplicate backend name in `backends`")

        prefixes = [b.match.path_prefix for b in self.backends]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("duplicate `match.path_prefix` in `backends`")

        name_set = set(names)
        for override_name in self.scoring.overrides:
            if override_name not in name_set:
                raise ValueError(
                    f"scoring.overrides references unknown backend: {override_name!r}"
                )
        return self


def load_config(path: Path) -> AACConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AACConfig.model_validate(raw)


def resolve_scoring_config(scoring: ScoringConfig, backend_name: str) -> ResolvedScoringConfig:
    """Deep-merge `scoring.overrides.<backend_name>` into
    `scoring.default_penalties`/`base_scores`. A backend override touching
    only one dimension/field leaves everything else — other dimensions,
    other backends — unchanged.

    `copy.deepcopy` is load-bearing here: without it, an override merged
    into a shared nested dict/list would alias back into
    `scoring.default_penalties`, silently corrupting every other backend
    resolved afterwards.
    """
    override = scoring.overrides.get(backend_name)

    defaults_dump = scoring.default_penalties.model_dump()
    penalties: dict[str, list[PenaltyConfig]] = {}
    for dim in _PENALTY_DIMENSIONS:
        windows = copy.deepcopy(defaults_dump[dim])
        override_window = override.penalties.get(dim) if override else None
        if override_window:
            windows[0] = {**windows[0], **override_window}
        penalties[dim] = [PenaltyConfig(**w) for w in windows]

    base_scores = {**scoring.base_scores, **(override.base_scores if override else {})}

    return ResolvedScoringConfig(
        base_scores=base_scores,
        exempt_countries=list(scoring.exempt_countries),
        score_clamp=scoring.score_clamp,
        penalties=DefaultPenalties(**penalties),
    )
