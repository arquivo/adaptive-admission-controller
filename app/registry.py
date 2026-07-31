"""BackendPolicyRegistry — resolves an incoming request path to a configured
backend using longest-prefix-wins matching (FR-011a).
"""

from __future__ import annotations

from app.classifier import classify
from app.config import AACConfig, BackendConfig, ResolvedScoringConfig, resolve_scoring_config
from app.interfaces import BackendPolicy, RequestContext


class DefaultBackendPolicy(BackendPolicy):
    def __init__(self, config: BackendConfig, resolved_scoring: ResolvedScoringConfig):
        self.config = config
        self.resolved_scoring = resolved_scoring

    def classify(self, request) -> RequestContext:
        return classify(request, self)

    def estimate_cost(self, ctx: RequestContext) -> int:
        return 1  # uniform cost for all request types (docs/decision_log.md A3)


def _prefix_matches(path: str, prefix: str) -> bool:
    """True if `path` falls under `prefix`, respecting path-segment
    boundaries — so `/textsearchEVIL` does NOT match prefix `/textsearch`,
    while `/textsearch/foo` does.
    """
    if path == prefix:
        return True
    boundary = prefix if prefix.endswith("/") else prefix + "/"
    return path.startswith(boundary)


class BackendPolicyRegistry:
    """Owns one `DefaultBackendPolicy` per configured backend, with its
    scoring config deep-merged once at construction time (§2.3 "Scoring
    config resolution") — never recomputed per request.
    """

    def __init__(self, config: AACConfig):
        self._policies: dict[str, DefaultBackendPolicy] = {
            backend.name: DefaultBackendPolicy(
                backend, resolve_scoring_config(config.scoring, backend.name)
            )
            for backend in config.backends
        }
        # Longest prefix first, so the first match found is the most specific
        # one — e.g. `/noFrame/patching` wins over a hypothetical bare
        # `/noFrame` (FR-011a).
        self._prefixes: list[tuple[str, str]] = sorted(
            ((backend.match.path_prefix, backend.name) for backend in config.backends),
            key=lambda entry: -len(entry[0]),
        )

    def match(self, path: str) -> DefaultBackendPolicy | None:
        for prefix, name in self._prefixes:
            if _prefix_matches(path, prefix):
                return self._policies[name]
        return None

    def all_policies(self) -> dict[str, DefaultBackendPolicy]:
        return dict(self._policies)
