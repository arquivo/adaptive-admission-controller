"""Unit tests for app.config.resolve_scoring_config — the per-backend
scoring deep-merge (docs/implementation_plan.md §2.3)."""

from app.config import AACConfig, resolve_scoring_config


def _backend(config, name):
    return next(b for b in config.backends if b.name == name)


def test_override_backend_gets_merged_ip_thresholds_unchanged_elsewhere(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    resolved = resolve_scoring_config(config.scoring, _backend(config, "page-search-api"))

    assert resolved.penalties.ip[0].soft_threshold == 5
    assert resolved.penalties.ip[0].hard_threshold == 15
    assert resolved.penalties.ip[0].soft_penalty == 15
    assert resolved.penalties.ip[0].hard_penalty == 50

    # Everything else is inherited unmodified from scoring.default_penalties.
    assert resolved.penalties.net24[0].soft_threshold == 50
    assert resolved.penalties.asn[0].soft_threshold == 200
    assert resolved.penalties.country[0].soft_threshold == 500
    assert len(resolved.penalties.user) == 2
    assert resolved.penalties.user[0].soft_threshold == 50
    assert resolved.penalties.user[1].soft_threshold == 500


def test_backend_with_no_override_matches_global_defaults_exactly(base_config_dict):
    config = AACConfig.model_validate(base_config_dict)
    resolved = resolve_scoring_config(config.scoring, _backend(config, "pywb-framed"))

    defaults = config.scoring.default_penalties
    assert resolved.penalties.ip[0].model_dump() == defaults.ip[0].model_dump()
    assert resolved.penalties.net24[0].model_dump() == defaults.net24[0].model_dump()
    assert resolved.base_scores == config.scoring.base_scores


def test_resolving_overriding_backend_does_not_mutate_shared_defaults(base_config_dict):
    """Regression test for the deep-merge aliasing trap: resolving a backend
    that overrides `ip` must not mutate `scoring.default_penalties.ip`
    itself, or every backend resolved afterwards would silently inherit the
    override too.
    """
    config = AACConfig.model_validate(base_config_dict)

    before = config.scoring.default_penalties.ip[0].model_dump()
    resolve_scoring_config(config.scoring, _backend(config, "page-search-api"))
    after = config.scoring.default_penalties.ip[0].model_dump()
    assert before == after

    # A backend resolved *after* the overriding one must still see the
    # original, un-corrupted defaults.
    resolved_other = resolve_scoring_config(config.scoring, _backend(config, "pywb-noframe"))
    assert resolved_other.penalties.ip[0].soft_threshold == 10
    assert resolved_other.penalties.ip[0].hard_threshold == 30


def test_base_scores_override_merges_without_dropping_other_classes(base_config_dict):
    base_config_dict["backends"][4]["scoring"] = {"overrides": {"base_scores": {"anonymous": 70}}}
    config = AACConfig.model_validate(base_config_dict)
    resolved = resolve_scoring_config(config.scoring, _backend(config, "pywb-patching"))

    assert resolved.base_scores["anonymous"] == 70
    assert resolved.base_scores["researcher"] == 80
    assert resolved.base_scores["internal"] == 100
