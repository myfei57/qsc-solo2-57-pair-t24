"""装配阶段交叉核对。

装配严格按两步走，顺序定死，不做双向并行：

1. **配置校验**（``stage="config"``）：先读配置并完成全部字段/取值校验。
   读取失败属于部分失败（配置文件缺失、JSON 损坏、字段缺失或越界都在这一步），
   此时手上只有半份配置，**禁止登记点表，更禁止启动**。
2. **点表登记 + 交叉核对**（``stage="points"``）：配置通过后才登记现场设备点表，
   再把「配置该有的设备」与「点表实际登记的设备」两边交叉核一遍。

核对结论分三类，每一条差异都标明责任侧（``config`` 还是 ``points``）：

- ``missing_in_points``：配置里声明了设备、点表里却没有 —— 点表侧缺项；
- ``extra_in_points``：点表里多出设备、配置没管到 —— 配置侧漏项；
- ``kind_mismatch``：同一地址两边登记的设备类型不一致 —— 双边冲突。

差异如何处置按**设备类型**定（见 :data:`DISCREPANCY_POLICY`）：关键设备
（热源、泵、换热站、补水阀、压力/流量测点）不一致直接 ``blocked`` 不许启动；
非关键设备（混水阀、热量表）不一致降级为 ``restricted`` 受限放行。
配置读取失败恒为 ``blocked``。

结论对象 :class:`AssemblyReport` 同时供控制台展示与审计落盘，两边读的是同一份
不可变数据，不会出现控制台说通过、审计却记失败的口径分歧。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .config import PlatformConfig
from .errors import ConfigError, PointTableError

# 交叉核对阶段顺序：定死为先配置、后点表，不允许双向并行。
STAGE_ORDER: tuple[str, ...] = ("config", "points")

# 受限模式：这些类型不一致时放行成受限模式（非关键设备）；
# 其余类型（关键设备）不一致一律拦下不许启动。
RESTRICTED_KINDS: frozenset[str] = frozenset(
    {"mix-valve", "heat-meter"}
)

# 按设备类型给出不一致时的处置策略，集中一处、装配期与控制台共用。
# ``block`` = 不许启动；``restrict`` = 放行成受限模式。
DISCREPANCY_POLICY: dict[str, str] = {
    "heat-source": "block",
    "weather-gateway": "block",
    "header": "block",
    "flow-meter": "block",
    "pump": "block",
    "control-valve": "block",
    "pressure-sensor": "block",
    "heat-exchange-station": "block",
    "mix-valve": "restrict",
    "heat-meter": "restrict",
}

VERDICT_OK = "ok"
VERDICT_RESTRICTED = "restricted"
VERDICT_BLOCKED = "blocked"

# 差异类型 -> 责任侧：缺项是点表没登记，多项/漏管是配置没覆盖，类型冲突两边都有问题。
_RESPONSIBILITY = {
    "missing_in_points": "points",
    "extra_in_points": "config",
    "kind_mismatch": "both",
}


@dataclass(frozen=True)
class PointEntry:
    """设备点表里的一条设备登记。"""

    address: str
    kind: str
    label: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PointEntry":
        address = raw.get("address")
        kind = raw.get("kind")
        if not isinstance(address, str) or not address:
            raise PointTableError("点表条目必须携带非空 address", component="points")
        if not isinstance(kind, str) or not kind:
            raise PointTableError(f"点表条目 {address} 必须携带非空 kind", component="points")
        label = raw.get("label", "")
        if not isinstance(label, str):
            raise PointTableError(f"点表条目 {address} 的 label 必须是字符串", component="points")
        return cls(address=address, kind=kind, label=label)

    def as_dict(self) -> dict:
        return {"address": self.address, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class Discrepancy:
    """一条配置/点表交叉差异，标明类型、责任侧与处置策略。"""

    code: str
    address: str
    kind: str
    responsibility: str
    policy: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "address": self.address,
            "kind": self.kind,
            "responsibility": self.responsibility,
            "policy": self.policy,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AssemblyReport:
    """装配交叉核对结论。

    ``stage`` 记录实际推进到的阶段：配置读取失败时停在 ``config``，点表阶段的
    差异才会推进到 ``points``。``blocked`` 恒不允许启动；``restricted`` 允许
    受限放行；``ok`` 正常启动。
    """

    stage: str
    verdict: str
    discrepancies: tuple[Discrepancy, ...] = field(default_factory=tuple)
    config_error: str = ""
    point_count: int = 0
    config_device_count: int = 0

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_BLOCKED

    @property
    def restricted(self) -> bool:
        return self.verdict == VERDICT_RESTRICTED

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_OK

    def allowed_to_start(self) -> bool:
        """是否允许进入启动流程（受限放行也算允许，但会带着限制运行）。"""

        return self.verdict in (VERDICT_OK, VERDICT_RESTRICTED)

    def by_responsibility(self, side: str) -> list[Discrepancy]:
        """按责任侧筛选差异（``config`` / ``points`` / ``both``）。"""

        return [item for item in self.discrepancies if item.responsibility == side]

    def as_dict(self) -> dict:
        """转成控制台展示与审计落盘共用的口径。"""

        blocked = [item.as_dict() for item in self.discrepancies if item.policy == "block"]
        restricted = [item.as_dict() for item in self.discrepancies if item.policy == "restrict"]
        return {
            "stage": self.stage,
            "verdict": self.verdict,
            "stage_order": list(STAGE_ORDER),
            "allowed_to_start": self.allowed_to_start(),
            "config_error": self.config_error,
            "counts": {
                "devices_config": self.config_device_count,
                "devices_points": self.point_count,
                "discrepancies": len(self.discrepancies),
                "blocking": len(blocked),
                "restricted": len(restricted),
            },
            "responsibility": {
                "config": len(self.by_responsibility("config")),
                "points": len(self.by_responsibility("points")),
                "both": len(self.by_responsibility("both")),
            },
            "blocking": blocked,
            "restricted_devices": restricted,
            "discrepancies": [item.as_dict() for item in self.discrepancies],
        }


def policy_for(kind: str) -> str:
    """按设备类型返回不一致处置策略：未知类型按最严的拦下处理。"""

    return DISCREPANCY_POLICY.get(kind, "block")


def expected_devices(station_names: tuple[str, ...]) -> dict[str, str]:
    """根据配置声明的站名单，给出平台该管理的全部设备地址 -> 类型。

    固定设备（热源、气象、一次网、泵、补水阀、压力测点）与按站登记的
    换热站、混水阀、热量表共用这一张期望表。
    """

    expected: dict[str, str] = {
        "plant.heat_source": "heat-source",
        "plant.weather": "weather-gateway",
        "primary.supply_header": "header",
        "primary.flow_meter": "flow-meter",
        "secondary.circulating_pump": "pump",
        "secondary.makeup_valve": "control-valve",
        "secondary.pressure_sensor": "pressure-sensor",
    }
    for name in station_names:
        expected[f"station.{name}"] = "heat-exchange-station"
        expected[f"valve.{name}.mix"] = "mix-valve"
        expected[f"quota.{name}.heat-meter"] = "heat-meter"
    return expected


def default_point_table(station_names: tuple[str, ...]) -> list[PointEntry]:
    """构造与配置完全一致的内置点表（未提供点表文件时使用）。"""

    return [
        PointEntry(address, kind) for address, kind in expected_devices(station_names).items()
    ]


def load_point_table(path: str) -> list[PointEntry]:
    """从 JSON 文件读取设备点表。

    点表文件缺失或结构非法属于点表读取失败，同样禁止装配继续
    （点表阶段不允许带着不完整的登记启动）。
    """

    if not os.path.isfile(path):
        raise PointTableError(f"设备点表文件不存在: {path}", component="points")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError as error:
            raise PointTableError(f"设备点表不是合法 JSON: {error}", component="points") from error
    if not isinstance(raw, Mapping):
        raise PointTableError("设备点表顶层必须是映射", component="points")
    entries_raw = raw.get("points")
    if not isinstance(entries_raw, list):
        raise PointTableError("设备点表必须包含 points 列表", component="points")
    entries = [PointEntry.from_mapping(item) for item in entries_raw]
    addresses = [entry.address for entry in entries]
    duplicates = sorted({address for address in addresses if addresses.count(address) > 1})
    if duplicates:
        raise PointTableError(
            f"设备点表存在重复登记地址: {duplicates}", component="points"
        )
    return entries


def cross_check(
    config_devices: Mapping[str, str], points: list[PointEntry]
) -> tuple[Discrepancy, ...]:
    """把配置期望设备与点表登记设备双向交叉核对，返回全部差异。"""

    point_index = {entry.address: entry for entry in points}
    discrepancies: list[Discrepancy] = []

    # 正向：配置声明了、点表没有（点表侧缺项）；同地址类型不一致（双边冲突）。
    for address, kind in config_devices.items():
        entry = point_index.get(address)
        if entry is None:
            discrepancies.append(
                Discrepancy(
                    code="missing_in_points",
                    address=address,
                    kind=kind,
                    responsibility=_RESPONSIBILITY["missing_in_points"],
                    policy=policy_for(kind),
                    detail=f"配置声明了 {kind} 设备 {address}，但设备点表未登记",
                )
            )
        elif entry.kind != kind:
            discrepancies.append(
                Discrepancy(
                    code="kind_mismatch",
                    address=address,
                    kind=kind,
                    responsibility=_RESPONSIBILITY["kind_mismatch"],
                    policy="block",
                    detail=(
                        f"设备 {address} 类型冲突：配置为 {kind}，点表登记为 {entry.kind}"
                    ),
                )
            )

    # 反向：点表多了、配置没管到（配置侧漏项）。
    for entry in points:
        if entry.address not in config_devices:
            discrepancies.append(
                Discrepancy(
                    code="extra_in_points",
                    address=entry.address,
                    kind=entry.kind,
                    responsibility=_RESPONSIBILITY["extra_in_points"],
                    policy=policy_for(entry.kind),
                    detail=f"设备点表多登记了 {entry.kind} 设备 {entry.address}，配置未覆盖",
                )
            )

    return tuple(sorted(discrepancies, key=lambda item: (item.code, item.address)))


def _verdict_for(discrepancies: tuple[Discrepancy, ...]) -> str:
    if any(item.policy == "block" for item in discrepancies):
        return VERDICT_BLOCKED
    if discrepancies:
        return VERDICT_RESTRICTED
    return VERDICT_OK


def assemble(
    config_loader: Callable[[], PlatformConfig],
    station_names: tuple[str, ...],
    *,
    point_table_path: str | None = None,
) -> tuple[PlatformConfig | None, list[PointEntry], AssemblyReport]:
    """装配入口：先配置、后点表，按定死的顺序交叉核对。

    ``config_loader`` 是无参可调用对象，返回一份完整 :class:`PlatformConfig`，
    失败时抛 :class:`ConfigError`。返回三元组：

    - 配置读取失败：``(None, [], report)``，结论恒为 ``blocked``，且**不会**
      去读点表（配置不过就不许登记点表）；
    - 点表读取失败：抛 :class:`PointTableError`（同样不许带半份点表启动）；
    - 其余：``(config, points, report)``，结论按设备类型策略为 ok/restricted/blocked。
    """

    # ---- 第一步：配置校验（没过就不许登记点表）----
    try:
        config = config_loader()
    except ConfigError as error:
        return (
            None,
            [],
            AssemblyReport(
                stage="config",
                verdict=VERDICT_BLOCKED,
                config_error=error.message,
            ),
        )

    # ---- 第二步：登记点表，再做双向交叉核对 ----
    if point_table_path is None:
        points = default_point_table(station_names)
    else:
        points = load_point_table(point_table_path)

    config_devices = expected_devices(station_names)
    discrepancies = cross_check(config_devices, points)
    report = AssemblyReport(
        stage="points",
        verdict=_verdict_for(discrepancies),
        discrepancies=discrepancies,
        point_count=len(points),
        config_device_count=len(config_devices),
    )
    return config, points, report
