"""装配期交叉校验：配置与点表的双向核对、处置策略与结论一致性。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from heatnet.app import Platform, build_platform
from heatnet.assembly import (
    KIND_MISMATCH,
    MISSING_IN_POINTS,
    PHASES,
    POLICY_BLOCK,
    POLICY_RESTRICT,
    SIDE_CONFIG,
    SIDE_POINTS,
    UNMANAGED_IN_CONFIG,
)
from heatnet.cli import main
from heatnet.config import DeviceDecl, LayoutConfig, config_to_mapping, default_config
from heatnet.console.api import Api
from heatnet.errors import InterlockError


def _layout(zones=None, devices=None) -> LayoutConfig:
    base = default_config().layout
    return LayoutConfig(
        zones=base.zones if zones is None else zones,
        devices=base.devices if devices is None else devices,
    )


def _platform(tmp_path, clock, layout: LayoutConfig) -> Platform:
    config = dataclasses.replace(default_config(), layout=layout)
    return build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))


def _layout_without_device(*device_names: str) -> LayoutConfig:
    devices = tuple(
        decl for decl in default_config().layout.devices if decl.device not in device_names
    )
    return _layout(devices=devices)


def test_default_assembly_is_clear(platform):
    report = platform.assembly
    assert report.verdict == "ok"
    assert report.findings == ()
    assert report.blocked is False
    assert PHASES == ("config", "points", "cross")
    assert platform.status()["assembly"]["verdict"] == "ok"
    started = platform.start()
    assert started["assembly_verdict"] == "ok"


def test_declared_station_missing_from_points_blocks_startup(tmp_path, clock):
    layout = _layout(zones=default_config().layout.zones + (("west", ("st07",)),))
    platform = _platform(tmp_path, clock, layout)
    report = platform.assembly
    assert report.verdict == "blocked"
    assert report.findings[0].direction == MISSING_IN_POINTS
    assert report.findings[0].side == SIDE_POINTS
    assert report.findings[0].kind == "heat-exchange-station"
    assert report.findings[0].address == "station.st07"
    assert report.findings[0].policy == POLICY_BLOCK
    assert "点表" in report.findings[0].message
    with pytest.raises(InterlockError):
        platform.start()
    with pytest.raises(InterlockError):
        platform.startup(-8.0, 60.0)
    alarms = [entry for entry in platform.audit.alarms() if entry["action"] == "assembly_checked"]
    assert alarms and alarms[0]["severity"] == "alarm"


def test_unmanaged_station_blocks_startup(tmp_path, clock):
    zones = tuple(
        (name, stations) for name, stations in default_config().layout.zones if name != "east"
    )
    platform = _platform(tmp_path, clock, _layout(zones=zones))
    assert platform.assembly.verdict == "blocked"
    finding = platform.assembly.findings[0]
    assert finding.direction == UNMANAGED_IN_CONFIG
    assert finding.side == SIDE_CONFIG
    assert finding.address == "station.st06"
    assert finding.policy == POLICY_BLOCK
    assert "配置" in finding.message
    with pytest.raises(InterlockError):
        platform.start()


def test_unmanaged_sensor_allows_restricted_mode(tmp_path, clock):
    platform = _platform(tmp_path, clock, _layout_without_device("pressure_sensor"))
    report = platform.assembly
    assert report.verdict == "restricted"
    assert report.blocked is False
    finding = report.findings[0]
    assert finding.direction == UNMANAGED_IN_CONFIG
    assert finding.side == SIDE_CONFIG
    assert finding.kind == "pressure-sensor"
    assert finding.policy == POLICY_RESTRICT
    started = platform.start()
    assert started["assembly_verdict"] == "restricted"
    assert platform.status()["assembly"]["verdict"] == "restricted"


def test_unmanaged_weather_gateway_fences_weather_push(tmp_path, clock):
    platform = _platform(tmp_path, clock, _layout_without_device("weather"))
    assert platform.assembly.verdict == "restricted"
    with pytest.raises(InterlockError):
        platform.push_weather(-8.0)


def test_declared_meter_missing_from_points_is_restricted_and_fenced(tmp_path, clock):
    devices = default_config().layout.devices + (
        DeviceDecl("quota", "st09.heat-meter", "heat-meter"),
    )
    platform = _platform(tmp_path, clock, _layout(devices=devices))
    report = platform.assembly
    assert report.verdict == "restricted"
    finding = report.findings[0]
    assert finding.direction == MISSING_IN_POINTS
    assert finding.side == SIDE_POINTS
    assert finding.kind == "heat-meter"
    assert finding.policy == POLICY_RESTRICT
    assert report.restricted("quota.st09.heat-meter") is True
    with pytest.raises(InterlockError):
        platform.collect_heat("st09.heat-meter", 1.0)
    # 未被隔离的热量表仍可正常采集。
    collected = platform.collect_heat("st01.heat-meter", 1.0)
    assert collected["meter"] == "st01.heat-meter"


def test_kind_mismatch_blocks_startup(tmp_path, clock):
    devices = tuple(
        DeviceDecl(decl.area, decl.device, "furnace") if decl.device == "heat_source" else decl
        for decl in default_config().layout.devices
    )
    platform = _platform(tmp_path, clock, _layout(devices=devices))
    finding = platform.assembly.findings[0]
    assert finding.direction == KIND_MISMATCH
    assert finding.policy == POLICY_BLOCK
    assert platform.assembly.blocked is True
    with pytest.raises(InterlockError):
        platform.startup(-8.0, 60.0)


def test_report_is_consistent_across_console_status_and_audit(tmp_path, clock):
    platform = _platform(tmp_path, clock, _layout_without_device("pressure_sensor"))
    api = Api(platform)
    status_code, payload = api.handle("GET", "/api/assembly", {}, {})
    assert status_code == 200
    assert payload == platform.status()["assembly"]
    records = [entry for entry in platform.audit.entries() if entry["action"] == "assembly_checked"]
    assert records
    assert records[0]["payload"]["findings"] == payload["findings"]
    assert records[0]["payload"]["verdict"] == payload["verdict"]
    assert payload["registered"]["stations"] == 6
    assert payload["declared"]["stations"] == 6
    assert payload["phases"] == ["config", "points", "cross"]


def test_blocked_platform_still_serves_console(tmp_path, clock):
    layout = _layout(zones=default_config().layout.zones + (("west", ("st07",)),))
    platform = _platform(tmp_path, clock, layout)
    api = Api(platform)
    status_code, payload = api.handle("GET", "/api/assembly", {}, {})
    assert status_code == 200
    assert payload["verdict"] == "blocked"
    status_code, _status = api.handle("GET", "/api/status", {}, {})
    assert status_code == 200
    status_code, error = api.handle("POST", "/api/startup", {}, {"outdoor_c": -8.0})
    assert status_code == 409
    assert error["component"] == "assembly"


def test_config_read_failure_is_partial_and_refuses_startup(tmp_path, capsys):
    broken = config_to_mapping(default_config())
    del broken["layout"]
    path = tmp_path / "heatnet.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    exit_code = main(["status", "--config", str(path), "--storage-root", str(tmp_path / "state")])
    assert exit_code == 2
    assert "layout" in capsys.readouterr().err
