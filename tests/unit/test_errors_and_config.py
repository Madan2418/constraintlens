from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from constraintlens.config import Settings
from constraintlens.domain import errors

if TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (errors.ConstraintViolation, 422),
        (errors.InvalidEnvelope, 400),
        (errors.UnknownTrace, 404),
        (errors.UnsatisfiableConstraints, 409),
        (errors.DependencyUnavailable, 503),
    ],
)
def test_error_status_codes_match_spec(cls: type[errors.ConstraintLensError], status: int) -> None:
    # ARCHITECTURE.md §11.4
    assert cls.status_code == status
    assert issubclass(cls, errors.ConstraintLensError)


def test_dependency_unavailable_message() -> None:
    e = errors.DependencyUnavailable("judge", "timeout after 10s")
    assert e.dependency == "judge"
    assert str(e) == "judge unavailable: timeout after 10s"
    assert str(errors.DependencyUnavailable("store")) == "store unavailable"


class TestSettings:
    def test_defaults_match_spec(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.chdir(tmp_path)  # make sure no local .env leaks in
        s = Settings()
        assert (s.theta_low, s.theta_high, s.delta_min) == (0.35, 0.80, 0.15)
        assert (s.dwell_threshold_s, s.default_max_age_s, s.max_regrounds) == (300, 3600, 2)
        assert (s.fail_high_risk, s.fail_low_risk) == ("closed", "open")
        assert s.store_full_text is False
        assert s.redact_pattern_list == ["email", "card", "phone"]

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CL_THETA_LOW", "0.2")
        monkeypatch.setenv("CL_ENABLE_TIER_2", "false")
        s = Settings()
        assert s.theta_low == 0.2
        assert s.enable_tier_2 is False

    def test_inverted_band_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CL_THETA_LOW", "0.9")
        monkeypatch.setenv("CL_THETA_HIGH", "0.5")
        with pytest.raises(ValidationError, match="THETA_LOW"):
            Settings()

    def test_snapshot_redacts_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CL_JUDGE_API_KEY", "sk-secret-value")
        snap = Settings().snapshot()
        assert snap["judge_api_key"] == "***"
        assert "sk-secret-value" not in repr(snap)
