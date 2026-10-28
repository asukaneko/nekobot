"""Tests for nbot.core.failover module."""

import time
from unittest.mock import MagicMock

from nbot.core.failover import (
    FailoverState,
    _compute_cooldown,
    _extract_status_code,
    classify_http_error,
    get_failover_state,
)

# ============================================================================
# classify_http_error
# ============================================================================


def test_classify_429_is_failover():
    assert classify_http_error(429) == "failover"


def test_classify_500_is_failover():
    assert classify_http_error(500) == "failover"


def test_classify_502_is_failover():
    assert classify_http_error(502) == "failover"


def test_classify_503_is_failover():
    assert classify_http_error(503) == "failover"


def test_classify_504_is_failover():
    assert classify_http_error(504) == "failover"


def test_classify_400_is_failover():
    assert classify_http_error(400) == "failover"


def test_classify_401_is_failover():
    assert classify_http_error(401) == "failover"


def test_classify_403_is_failover():
    assert classify_http_error(403) == "failover"


def test_classify_422_is_failover():
    assert classify_http_error(422) == "failover"


def test_classify_connection_error_is_transient():
    assert classify_http_error(-1) == "transient"


def test_classify_timeout_is_transient():
    assert classify_http_error(-2) == "transient"


def test_classify_unknown_is_transient():
    assert classify_http_error(0) == "transient"


# ============================================================================
# _compute_cooldown
# ============================================================================


def test_cooldown_429_first_failure():
    cd = _compute_cooldown(1, 429)
    assert cd == 60.0


def test_cooldown_429_second_failure():
    cd = _compute_cooldown(2, 429)
    assert cd == 120.0


def test_cooldown_429_capped_at_max():
    cd = _compute_cooldown(10, 429)
    assert cd == 300.0


def test_cooldown_500_first_failure():
    cd = _compute_cooldown(1, 500)
    assert cd == 30.0


def test_cooldown_500_capped():
    cd = _compute_cooldown(10, 500)
    assert cd == 120.0


def test_cooldown_400_has_cooldown():
    cd = _compute_cooldown(1, 400)
    assert cd == 30.0


def test_cooldown_401_has_cooldown():
    cd = _compute_cooldown(1, 401)
    assert cd == 30.0


def test_cooldown_transient():
    cd = _compute_cooldown(1, 0)
    assert cd == 15.0


# ============================================================================
# _extract_status_code
# ============================================================================


def test_extract_status_code_from_http_error():
    err = MagicMock()
    err.response = MagicMock()
    err.response.status_code = 429
    assert _extract_status_code(err) == 429


def test_extract_status_code_no_response():
    err = ValueError("something")
    assert _extract_status_code(err) == 0


# ============================================================================
# FailoverState
# ============================================================================


def test_select_model_returns_first_when_all_healthy():
    state = FailoverState()
    configs = [
        {"model_id": "a", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]
    result = state.select_model(configs)
    assert result["model_id"] == "a"


def test_select_model_skips_excluded():
    state = FailoverState()
    configs = [
        {"model_id": "a", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]
    result = state.select_model(configs, exclude_ids={"a"})
    assert result["model_id"] == "b"


def test_select_model_skips_cooldown():
    state = FailoverState()
    configs = [
        {"model_id": "a", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]
    # Put model "a" in cooldown
    state.record_failure("a", 429)
    result = state.select_model(configs)
    assert result["model_id"] == "b"


def test_select_model_returns_first_as_fallback_when_all_in_cooldown():
    state = FailoverState()
    configs = [
        {"model_id": "a", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]
    state.record_failure("a", 429)
    state.record_failure("b", 500)
    result = state.select_model(configs)
    # Returns first as last resort
    assert result["model_id"] == "a"


def test_select_model_empty_list():
    state = FailoverState()
    result = state.select_model([])
    assert result is None


def test_record_success_resets_failures():
    state = FailoverState()
    state.record_failure("a", 429)
    state.record_failure("a", 429)
    health = state._health["a"]
    assert health.consecutive_failures == 2

    state.record_success("a")
    assert health.consecutive_failures == 0
    assert health.cooldown_until == 0.0


def test_record_failure_increments():
    state = FailoverState()
    state.record_failure("a", 500)
    assert state._health["a"].consecutive_failures == 1
    assert state._health["a"].last_failure_code == 500

    state.record_failure("a", 500)
    assert state._health["a"].consecutive_failures == 2


def test_record_failure_400_has_cooldown():
    state = FailoverState()
    cooldown = state.record_failure("a", 400)
    assert cooldown == 30.0
    assert state._health["a"].cooldown_until > 0.0


def test_is_available_when_no_health():
    state = FailoverState()
    assert state.is_available("unknown") is True


def test_is_available_after_failure():
    state = FailoverState()
    state.record_failure("a", 429)
    # Should be in cooldown
    assert state.is_available("a") is False


def test_is_available_after_cooldown_expires():
    state = FailoverState()
    # Manually set cooldown in the past
    state.record_failure("a", 429)
    health = state._health["a"]
    health.cooldown_until = time.monotonic() - 1
    assert state.is_available("a") is True


def test_reset_single_model():
    state = FailoverState()
    state.record_failure("a", 429)
    state.record_failure("b", 500)
    state.reset("a")
    assert "a" not in state._health
    assert "b" in state._health


def test_reset_all():
    state = FailoverState()
    state.record_failure("a", 429)
    state.record_failure("b", 500)
    state.reset()
    assert len(state._health) == 0


def test_get_all_health_summary():
    state = FailoverState()
    state.record_failure("a", 429)
    summary = state.get_all_health_summary()
    assert "a" in summary
    assert summary["a"]["consecutive_failures"] == 1
    assert summary["a"]["last_failure_code"] == 429
    assert isinstance(summary["a"]["cooldown_remaining"], float)
    assert isinstance(summary["a"]["available"], bool)


def test_singleton_returns_same_instance():
    s1 = get_failover_state()
    s2 = get_failover_state()
    assert s1 is s2


def test_select_model_with_empty_model_id():
    """Models without model_id should still be selectable."""
    state = FailoverState()
    configs = [
        {"model_id": "", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]
    result = state.select_model(configs)
    assert result["model_id"] == ""


def test_failover_full_cycle():
    """Simulate: model A fails, failover to B, B succeeds, then A recovers."""
    state = FailoverState()
    configs = [
        {"model_id": "a", "model": "gpt-4"},
        {"model_id": "b", "model": "claude-3"},
    ]

    # Initially A is selected
    assert state.select_model(configs)["model_id"] == "a"

    # A fails with 429
    state.record_failure("a", 429)
    assert state.is_available("a") is False

    # B is selected next
    assert state.select_model(configs)["model_id"] == "b"

    # B succeeds
    state.record_success("b")

    # A's cooldown expires
    state._health["a"].cooldown_until = time.monotonic() - 1

    # A is selected again
    assert state.select_model(configs)["model_id"] == "a"
