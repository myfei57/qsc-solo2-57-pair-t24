"""装配阶段配置/点表交叉核对。

定死的顺序：先配置校验（没过不许登记点表），再登记点表并双向交叉核对。
配置读取失败是部分失败，不许带半份配置启动；点表差异按设备类型决定
直接拦下（关键设备）还是放行成受限模式（混水阀、热量表）。
"""

from __future__ import annotations

import json

import pytest

from heatnet.app import assemble_platform, build_platform
from heatnet.assembly import (
    STAGE_ORDER,
    AssemblyReport,
    PointEntry,
    assemble,
    cross_check,
    default_point_table,
    expected_devices,
    load_point_table,
)
from heatnet.config import ConfigError, config_to_mapping, default_config
from heatnet.errors import AssemblyError, PointTableError

STATIONS = ("st01", "st02", "st03", "st04", "st05", "st06")


def _write_points(tmp_path, entries, name="points.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"points": entries}), encoding="utf-8")
    return str(path)


def _base_entries():
    return [entry.as_dict() for entry in default_point_table(STATIONS)]


# ---- 顺序：先配置、后点表 -------------------------------------------------


def test_stage_order_is_fixed_config_before_points():
    assert STAGE_ORDER == ("config", "points")


def test_config_failure_never_registers_point_table(tmp_path):
    sentinel = tmp_path / "must_not_be_read.json"
    sentinel.write_text(json.dumps({"points": []}), encoding="utf-8")

    def broken_loader():
        raise ConfigError("半份配置", component="config")

    config, points, report = assemble(
        broken_loader, STATIONS, point_table_path=str(sentinel)
    )
    assert config is None
    assert points == []
    assert report.stage == "config"
    assert report.blocked
    assert report.verdict == "blocked"


def test_assemble_platform_rejects_partial_config(tmp_path):
    with pytest.raises(AssemblyError):
        assemble_platform(
            config_path=str(tmp_path / "nope.json"),
            point_table_path=str(tmp_path / "p.json"),
            storage_root=str(tmp_path / "state"),
        )


def test_corrupt_config_json_is_partial_failure(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        from heatnet.config import load_config

        load_config(str(cfg))
    with pytest.raises(AssemblyError):
        assemble_platform(config_path=str(cfg), storage_root=str(tmp_path / "state"))


def test_invalid_config_field_blocks_before_points(tmp_path):
    mapping = config_to_mapping(default_config())
    mapping["limits"]["source"]["flow_max_t_h"] = 1.0
    cfg = tmp_path / "bad_field.json"
    cfg.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(AssemblyError):
        assemble_platform(config_path=str(cfg), storage_root=str(tmp_path / "state"))


# ---- 点表读取失败同样不许带半份点表启动 -----------------------------------


def test_missing_point_table_raises(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(config_to_mapping(default_config())), encoding="utf-8")
    with pytest.raises(PointTableError):
        assemble_platform(
            config_path=str(cfg),
            point_table_path=str(tmp_path / "missing_points.json"),
            storage_root=str(tmp_path / "state"),
        )


def test_duplicate_and_malformed_point_entries_rejected(tmp_path):
    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps(
            {"points": [{"address": "a.b", "kind": "pump"}, {"address": "a.b", "kind": "pump"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(PointTableError):
        load_point_table(str(dup))

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"points": [{"kind": "pump"}]}), encoding="utf-8")
    with pytest.raises(PointTableError):
        load_point_table(str(bad))


# ---- 交叉核对：缺项、多项、类型冲突 ----------------------------------------


def test_missing_station_is_point_side_and_blocks(tmp_path):
    entries = [d for d in _base_entries() if d["address"] != "station.st03"]
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    report = platform.assembly_report
    assert report.verdict == "blocked"
    assert not report.allowed_to_start()
    blocking = report.as_dict()["blocking"]
    assert blocking[0]["address"] == "station.st03"
    assert blocking[0]["code"] == "missing_in_points"
    assert blocking[0]["responsibility"] == "points"
    with pytest.raises(AssemblyError):
        platform.start()
    with pytest.raises(AssemblyError):
        platform.startup(-5.0)


def test_extra_pump_is_config_side_and_blocks(tmp_path):
    entries = _base_entries() + [{"address": "secondary.spare_pump", "kind": "pump"}]
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    report = platform.assembly_report
    assert report.blocked
    item = report.as_dict()["blocking"][0]
    assert item["address"] == "secondary.spare_pump"
    assert item["code"] == "extra_in_points"
    assert item["responsibility"] == "config"


def test_kind_mismatch_is_both_sides_and_blocks(tmp_path):
    entries = [dict(d) for d in _base_entries()]
    for entry in entries:
        if entry["address"] == "plant.heat_source":
            entry["kind"] = "pump"
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    report = platform.assembly_report
    assert report.blocked
    item = report.as_dict()["blocking"][0]
    assert item["code"] == "kind_mismatch"
    assert item["responsibility"] == "both"


def test_missing_valve_and_extra_meter_allow_restricted(tmp_path):
    entries = [d for d in _base_entries() if d["address"] != "valve.st04.mix"]
    entries.append({"address": "quota.st09.heat-meter", "kind": "heat-meter"})
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    report = platform.assembly_report
    assert report.verdict == "restricted"
    assert report.allowed_to_start()
    assert report.restricted
    started = platform.start()
    assert started["restricted"] is True
    data = report.as_dict()
    assert data["responsibility"] == {"config": 1, "points": 1, "both": 0}
    addresses = {item["address"] for item in data["restricted_devices"]}
    assert addresses == {"valve.st04.mix", "quota.st09.heat-meter"}


def test_default_point_table_matches_config_exactly():
    entries = default_point_table(STATIONS)
    discrepancies = cross_check(expected_devices(STATIONS), entries)
    assert discrepancies == ()
    report = AssemblyReport(stage="points", verdict="ok")
    assert report.ok and report.allowed_to_start() and not report.restricted


# ---- 结论一致：控制台与审计读同一份结论 ------------------------------------


def test_assembly_verdict_is_audited_and_matches_status(tmp_path):
    entries = [d for d in _base_entries() if d["address"] != "valve.st01.mix"]
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    records = [
        record
        for record in platform.audit.records()
        if record.action == "assembly_check"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.severity == "warning"
    assert record.payload["verdict"] == platform.assembly_report.verdict == "restricted"
    assert platform.status()["assembly"]["verdict"] == "restricted"
    # 结论同样落进了 journal，跨重启口径一致。
    journaled = platform.audit.replay_from_journal()
    assert any(item["action"] == "assembly_check" for item in journaled)


def test_blocked_assembly_audited_as_alarm(tmp_path):
    entries = [d for d in _base_entries() if d["address"] != "station.st01"]
    path = _write_points(tmp_path, entries)
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    record = [r for r in platform.audit.records() if r.action == "assembly_check"][0]
    assert record.severity == "alarm"
    assert record.payload["verdict"] == "blocked"


# ---- 控制台展示 ------------------------------------------------------------


def test_console_assembly_and_readiness_endpoints(tmp_path):
    from heatnet.console.api import Api

    path = _write_points(
        tmp_path, [d for d in _base_entries() if d["address"] != "station.st02"]
    )
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    api = Api(platform)

    code, body = api.handle("GET", "/api/assembly", {}, {})
    assert code == 200
    assert body["verdict"] == "blocked"
    assert body["stage_order"] == ["config", "points"]
    assert body["point_side"][0]["address"] == "station.st02"

    code, readiness = api.handle("GET", "/api/readiness", {}, {})
    assert code == 200
    assert readiness["ready"] is False
    assert readiness["checks"]["assembly_passed"] is False
    assert readiness["assembly"]["verdict"] == "blocked"

    code, startup = api.handle("POST", "/api/startup", {}, {"outdoor_c": -5.0})
    assert code == 409
    assert startup["error"] == "AssemblyError"


def test_restricted_platform_readiness_still_reports(tmp_path):
    from heatnet.console.api import Api

    path = _write_points(
        tmp_path, [d for d in _base_entries() if d["address"] != "valve.st05.mix"]
    )
    platform = assemble_platform(
        config=default_config(), point_table_path=path, storage_root=str(tmp_path / "state")
    )
    api = Api(platform)
    code, readiness = api.handle("GET", "/api/readiness", {}, {})
    assert code == 200
    assert readiness["info"]["restricted"] is True
    assert readiness["assembly"]["verdict"] == "restricted"


# ---- 默认装配（未提供点表）保持向后兼容 ------------------------------------


def test_build_platform_default_assembly_is_ok(platform):
    assert platform.assembly_report.ok
    started = platform.start()
    assert started["assembly"]["verdict"] == "ok"


def test_point_entry_requires_address_and_kind():
    with pytest.raises(PointTableError):
        PointEntry.from_mapping({"kind": "pump"})
    with pytest.raises(PointTableError):
        PointEntry.from_mapping({"address": "a.b"})
