from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas import WrfTaskCreate


def valid_request() -> dict:
    return {
        "start_time": "2026-07-16T00:00:00Z",
        "end_time": "2026-07-16T06:00:00Z",
        "center": {"lat": 32.048, "lon": 118.825},
        "forecast_interval_hours": 1,
        "domains": [
            {"id": "d01", "dx": 27000, "dy": 27000, "e_we": 100, "e_sn": 79, "parent_id": 0},
            {"id": "d02", "dx": 9000, "dy": 9000, "e_we": 79, "e_sn": 61, "parent_id": 1, "i_parent_start": 11, "j_parent_start": 11},
        ],
    }


def test_valid_nested_domains_are_normalized() -> None:
    request = WrfTaskCreate.model_validate(valid_request())
    assert request.domains[1].parent_grid_ratio == 3
    assert request.start_time.isoformat().endswith("+00:00")
    assert request.spinup.mode == "off"
    assert request.spinup.hours == 0


def test_spinup_and_per_domain_physics_are_validated() -> None:
    value = valid_request()
    value["spinup"] = {"mode": "auto"}
    value["forecast_focus"] = "convection"
    value["physics"] = {"cu_physics_by_domain": [1, 3], "sf_urban_physics_by_domain": [0, 0]}
    request = WrfTaskCreate.model_validate(value)
    assert request.spinup.mode == "auto"
    assert request.physics.cu_physics_by_domain == [1, 3]


def test_per_domain_physics_count_must_match_domains() -> None:
    value = valid_request()
    value["physics"] = {"cu_physics_by_domain": [1]}
    with pytest.raises(ValidationError, match="逐域积云方案数量"):
        WrfTaskCreate.model_validate(value)


def test_child_domain_cannot_exceed_parent() -> None:
    value = valid_request()
    value["domains"][1]["i_parent_start"] = 90
    with pytest.raises(ValidationError, match="不能超出父域边界"):
        WrfTaskCreate.model_validate(value)


def test_incompatible_boundary_layer_pair_is_rejected() -> None:
    value = valid_request()
    value["physics"] = {"bl_pbl_physics": 2, "sf_sfclay_physics": 1}
    with pytest.raises(ValidationError, match="边界层方案与近地层方案不兼容"):
        WrfTaskCreate.model_validate(value)


def test_domain_grid_size_is_limited() -> None:
    value = valid_request()
    value["domains"][0]["e_we"] = 501
    with pytest.raises(ValidationError):
        WrfTaskCreate.model_validate(value)
