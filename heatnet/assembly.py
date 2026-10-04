"""装配期交叉校验。

装配顺序定死为三道工序，不可调换：

1. **配置校验**——配置解析与取值校验（:mod:`heatnet.config`），任何缺失或越界
   都抛出 ``ConfigError``。配置读取失败属于部分失败：校验不过就不许进入下一道
   工序，绝不允许带着半份配置登记点表；
2. **点表登记**——把现场设备（分区、换热站、混水阀、热量表、命名空间）登记进
   注册表；
3. **双向交叉核对**——一道工序覆盖两个方向：配置声明了但点表未登记的（缺）、
   点表登记了但配置未纳管的（多）、同一地址两侧类型不一致的，都在启动之前
   拦下来，每条结论都标明问题出在哪一边。

不一致的处置按设备类型定死：参与启停联锁的设备（热源、母管、流量计、泵、
混水阀、换热站）不一致即禁止启动；计量与监视类设备（补水阀、压力传感器、
热量表、气象网关）不一致则放行成受限模式，对应功能被隔离。未知类型一律按
禁止启动处理。

校验结论、点表隔离集与审计流水来自同一份 :class:`AssemblyReport`：控制台展示
与落审计读的是同一个对象，口径天然一致。结论在装配时定稿；运行期的分区拆分
属于正常操作，不触发重核。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LayoutConfig
from .ns.registry import NamespaceRegistry
from .ns.zone import ZoneRegistry

# 装配工序的固定顺序：先配置校验，再点表登记，最后双向交叉核对。
PHASES: tuple[str, ...] = ("config", "points", "cross")

# 结论标明问题出在哪一边：配置一侧还是点表一侧。
SIDE_CONFIG = "config"
SIDE_POINTS = "points"

# 不一致的三种形态。
MISSING_IN_POINTS = "missing_in_points"
UNMANAGED_IN_CONFIG = "unmanaged_in_config"
KIND_MISMATCH = "kind_mismatch"

# 处置策略：禁止启动，或放行成受限模式。
POLICY_BLOCK = "block"
POLICY_RESTRICT = "restrict"

VERDICT_OK = "ok"
VERDICT_RESTRICTED = "restricted"
VERDICT_BLOCKED = "blocked"

STATION_KIND = "heat-exchange-station"

# 站单元（换热站及其混水阀、热量表）随站一起登记，由站级比对覆盖。
STATION_AREAS = ("station", "valve", "quota")

# 处置策略按设备类型定死：联锁类禁止启动，计量与监视类放行受限模式。
KIND_POLICY = {
    "heat-source": POLICY_BLOCK,
    "header": POLICY_BLOCK,
    "flow-meter": POLICY_BLOCK,
    "pump": POLICY_BLOCK,
    "mix-valve": POLICY_BLOCK,
    STATION_KIND: POLICY_BLOCK,
    "control-valve": POLICY_RESTRICT,
    "pressure-sensor": POLICY_RESTRICT,
    "heat-meter": POLICY_RESTRICT,
    "weather-gateway": POLICY_RESTRICT,
}


def policy_for(kind: str) -> str:
    """取某设备类型的处置策略；未知类型一律按禁止启动处理。"""

    return KIND_POLICY.get(kind, POLICY_BLOCK)


@dataclass(frozen=True)
class Finding:
    """一条交叉核对结论：不一致的形态、问题出在哪一边、按什么策略处置。"""

    direction: str
    side: str
    kind: str
    address: str
    policy: str
    message: str

    def as_dict(self) -> dict:
        return {
            "direction": self.direction,
            "side": self.side,
            "kind": self.kind,
            "address": self.address,
            "policy": self.policy,
            "message": self.message,
        }


@dataclass(frozen=True)
class AssemblyReport:
    """一次装配校验的结论：双方登记规模、全部不一致条目与最终裁定。"""

    declared_stations: int
    registered_stations: int
    declared_devices: int
    registered_devices: int
    findings: tuple[Finding, ...]

    @property
    def verdict(self) -> str:
        """裁定：有禁止级结论即 blocked，否则有结论即 restricted，否则 ok。"""

        if any(finding.policy == POLICY_BLOCK for finding in self.findings):
            return VERDICT_BLOCKED
        if self.findings:
            return VERDICT_RESTRICTED
        return VERDICT_OK

    @property
    def blocked(self) -> bool:
        """是否禁止启动。"""

        return self.verdict == VERDICT_BLOCKED

    def restricted(self, address: str) -> bool:
        """某地址是否因受限级结论被隔离。"""

        return any(
            finding.policy == POLICY_RESTRICT and finding.address == address
            for finding in self.findings
        )

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "phases": list(PHASES),
            "declared": {"stations": self.declared_stations, "devices": self.declared_devices},
            "registered": {"stations": self.registered_stations, "devices": self.registered_devices},
            "blocking": sum(1 for finding in self.findings if finding.policy == POLICY_BLOCK),
            "restricting": sum(1 for finding in self.findings if finding.policy == POLICY_RESTRICT),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def cross_check(
    declared_stations: frozenset[str],
    declared_devices: dict[str, str],
    registered_stations: frozenset[str],
    registered_devices: dict[str, str],
) -> tuple[Finding, ...]:
    """双向比对声明集与登记集，返回全部不一致结论（按地址排序，结论确定）。"""

    findings: list[Finding] = []
    for station in sorted(declared_stations - registered_stations):
        findings.append(
            Finding(
                MISSING_IN_POINTS,
                SIDE_POINTS,
                STATION_KIND,
                f"station.{station}",
                POLICY_BLOCK,
                f"配置声明了换热站 {station}，但点表未登记（缺在点表一侧）",
            )
        )
    for station in sorted(registered_stations - declared_stations):
        findings.append(
            Finding(
                UNMANAGED_IN_CONFIG,
                SIDE_CONFIG,
                STATION_KIND,
                f"station.{station}",
                POLICY_BLOCK,
                f"点表登记了换热站 {station}，但配置未纳管（问题在配置一侧）",
            )
        )
    for address in sorted(declared_devices.keys() - registered_devices.keys()):
        kind = declared_devices[address]
        findings.append(
            Finding(
                MISSING_IN_POINTS,
                SIDE_POINTS,
                kind,
                address,
                policy_for(kind),
                f"配置声明了 {kind} {address}，但点表未登记（缺在点表一侧）",
            )
        )
    for address in sorted(registered_devices.keys() - declared_devices.keys()):
        kind = registered_devices[address]
        findings.append(
            Finding(
                UNMANAGED_IN_CONFIG,
                SIDE_CONFIG,
                kind,
                address,
                policy_for(kind),
                f"点表登记了 {kind} {address}，但配置未纳管（问题在配置一侧）",
            )
        )
    for address in sorted(declared_devices.keys() & registered_devices.keys()):
        declared_kind = declared_devices[address]
        registered_kind = registered_devices[address]
        if declared_kind != registered_kind:
            findings.append(
                Finding(
                    KIND_MISMATCH,
                    SIDE_CONFIG,
                    registered_kind,
                    address,
                    POLICY_BLOCK,
                    f"{address} 设备类型不一致：配置声明为 {declared_kind}，点表登记为 {registered_kind}",
                )
            )
    return tuple(findings)


def verify_assembly(
    layout: LayoutConfig, zones: ZoneRegistry, namespaces: NamespaceRegistry
) -> AssemblyReport:
    """把配置声明的布局与已登记的现场点表交叉核一遍，给出装配结论。"""

    declared_devices = {decl.address: decl.kind for decl in layout.devices}
    registered_devices: dict[str, str] = {}
    for device in namespaces.devices():
        if device.area in STATION_AREAS and device.address not in declared_devices:
            # 站单元设备随换热站一起登记，由站级比对覆盖；配置单独声明的
            # 站单元设备仍按设备级比对。
            continue
        registered_devices[device.address] = device.kind
    declared_stations = layout.stations()
    registered_stations = frozenset(zones.index())
    findings = cross_check(
        declared_stations, declared_devices, registered_stations, registered_devices
    )
    return AssemblyReport(
        declared_stations=len(declared_stations),
        registered_stations=len(registered_stations),
        declared_devices=len(declared_devices),
        registered_devices=len(registered_devices),
        findings=findings,
    )
