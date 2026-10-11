"""Resolve the point represented by a pose before applying workspace bounds.

All poses use robot-base XYZ in metres and extrinsic xyz Euler angles in
radians. The rigid tool offset is expressed in the flange's local axes:
``p_tcp = p_flange + R_base_from_flange @ tcp_offset_tool_m``. The TCP has
the flange's orientation; this module does not model a rotated tool frame.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Any


EULER_CONVENTION = "extrinsic xyz; R = Rz(yaw) @ Ry(pitch) @ Rx(roll)"
_FRAMES = {
    "tcp": "tcp", "tool": "tcp", "flange": "flange",
    "T_robot_base_from_tcp": "tcp", "T_robot_base_from_tool": "tcp",
    "T_robot_base_from_flange": "flange",
}


class WorkspaceError(ValueError):
    """A workspace rejection with the exact coordinates and limiting axes."""

    def __init__(self, details: dict):
        self.details = details
        failures = "; ".join(
            f"{entry['role']}.{entry['axis']}={entry['value']:.6f} m "
            f"violates {entry['bound']}={entry['limit']:.6f} m "
            f"by {entry['excess_m'] * 1000:.3f} mm"
            for entry in details["violations"]
        )
        super().__init__(
            f"outside workspace (pose_reference={details['pose_reference']}, "
            f"workspace_reference={details['workspace_reference']}): {failures}"
        )


def _vector(value: Any, count: int, name: str) -> list[float]:
    if isinstance(value, (str, bytes, dict)):
        raise ValueError(f"{name} must contain {count} finite numbers")
    try:
        entries = list(value)
    except TypeError as exc:
        raise ValueError(f"{name} must contain {count} finite numbers") from exc
    if len(entries) != count or any(
        isinstance(item, bool) or not isinstance(item, Real) or not math.isfinite(item)
        for item in entries
    ):
        raise ValueError(f"{name} must contain {count} finite numbers")
    return [float(item) for item in entries]


def _validate_orientation(hardware_config: dict, state: dict | None = None) -> None:
    if hardware_config.get("orientation_unit", "radians") != "radians":
        raise ValueError("coordinate conversion requires extrinsic xyz orientation in radians")
    for source in (hardware_config, state or {}):
        for key in ("tcp_euler_convention", "euler_convention"):
            if key in source and source[key] != EULER_CONVENTION:
                raise ValueError(f"unsupported {key}: expected {EULER_CONVENTION}")


def resolve_pose_reference(hardware_config: dict, state: dict) -> str:
    """Resolve TCP/flange explicitly, never infer it from a ``tcp_pose`` key.

    An explicit local override is authoritative over legacy receiver labels.
    In receiver_reported mode all supplied frame declarations must agree.
    """
    _validate_orientation(hardware_config, state)
    configured = hardware_config.get("pose_reference", "receiver_reported")
    if configured in ("tcp", "flange"):
        return configured
    if configured != "receiver_reported":
        raise ValueError("hardware.pose_reference must be tcp, flange or receiver_reported")
    declarations = {key: state[key] for key in ("tcp_pose_frame", "pose_frame") if key in state}
    references = []
    for key, frame in declarations.items():
        if not isinstance(frame, str) or frame not in _FRAMES:
            raise ValueError(f"ambiguous receiver pose reference: unsupported {key}={frame!r}")
        references.append(_FRAMES[frame])
    if not references:
        raise ValueError("ambiguous receiver pose reference; require tcp/flange or explicit state frame")
    if len(set(references)) != 1:
        raise ValueError(f"conflicting receiver pose reference declarations: {declarations}")
    return references[0]


def convert_pose(pose: list[float], source_reference: str, target_reference: str,
                 hardware_config: dict) -> list[float]:
    """Convert the pose's reference point once, preserving orientation."""
    value = _vector(pose, 6, "pose")
    _validate_orientation(hardware_config)
    if source_reference not in ("tcp", "flange") or target_reference not in ("tcp", "flange"):
        raise ValueError("coordinate conversion requires resolved tcp/flange references")
    offset = None
    if "tcp_offset_tool_m" in hardware_config:
        offset = _vector(hardware_config["tcp_offset_tool_m"], 3, "tcp_offset_tool_m")
    if source_reference == target_reference:
        return value
    if offset is None:
        raise ValueError("tcp_offset_tool_m is required for TCP/flange conversion")
    roll, pitch, yaw = value[3:]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    # Apply Rx, then Ry, then Rz: extrinsic xyz with column vectors.
    x, y, z = offset
    y, z = cr * y - sr * z, sr * y + cr * z
    x, z = cp * x + sp * z, -sp * x + cp * z
    x, y = cy * x - sy * y, sy * x + cy * y
    sign = 1 if source_reference == "flange" else -1
    return [value[0] + sign * x, value[1] + sign * y, value[2] + sign * z, *value[3:]]


def workspace_report(poses: dict[str, list[float]], safety: dict, *,
                     pose_reference: str | None, hardware_config: dict) -> dict:
    """Check every pose in the coordinate reference declared by the limits.

    Configurations predating workspace_reference retain direct comparisons in
    receiver coordinates. Explicit flange/TCP limits require a resolved source
    reference. No coordinate is clipped into the configured workspace.
    """
    enabled = safety.get("workspace_enabled", True)
    if type(enabled) is not bool:
        raise ValueError("safety.workspace_enabled must be a boolean")
    if not isinstance(poses, dict) or not poses:
        raise ValueError("workspace check requires named poses")
    inputs = {role: _vector(pose, 6, f"{role} pose") for role, pose in poses.items()}
    configured_reference = safety.get("workspace_reference")
    legacy = "workspace_reference" not in safety
    if legacy:
        reference = pose_reference or "receiver_reported"
        converted = {role: pose.copy() for role, pose in inputs.items()}
    else:
        if configured_reference not in ("tcp", "flange"):
            raise ValueError("safety.workspace_reference must be tcp or flange")
        if pose_reference not in ("tcp", "flange"):
            raise ValueError("explicit workspace reference requires resolved tcp/flange pose reference")
        reference = configured_reference
        converted = {role: convert_pose(pose, pose_reference, reference, hardware_config)
                     for role, pose in inputs.items()}
    if not enabled:
        return {"ok": True, "enabled": False, "status": "disabled",
                "reason": "fixed XYZ workspace bounds disabled by operator; not a reachability or collision verdict",
                "pose_reference": pose_reference or "receiver_reported",
                "workspace_reference": reference, "legacy_receiver_coordinates": legacy,
                "input_poses": inputs, "converted_poses": converted,
                "xyz_min": None, "xyz_max": None, "violations": []}
    lower = _vector(safety.get("xyz_min"), 3, "workspace xyz_min")
    upper = _vector(safety.get("xyz_max"), 3, "workspace xyz_max")
    if any(low >= high for low, high in zip(lower, upper)):
        raise ValueError("workspace xyz_min must be lower than xyz_max")
    violations = []
    for role, pose in converted.items():
        for index, axis in enumerate(("x", "y", "z")):
            value = pose[index]
            for bound, limit, excess in (("min", lower[index], lower[index] - value),
                                         ("max", upper[index], value - upper[index])):
                if excess > 0:
                    violations.append({"role": role, "axis": axis, "value": value,
                                       "bound": bound, "limit": limit, "excess_m": excess})
    return {"ok": not violations, "enabled": True, "status": "passed" if not violations else "violated",
            "pose_reference": pose_reference or "receiver_reported",
            "workspace_reference": reference, "legacy_receiver_coordinates": legacy,
            "input_poses": inputs, "converted_poses": converted,
            "xyz_min": lower, "xyz_max": upper, "violations": violations}


def require_workspace(poses: dict[str, list[float]], safety: dict, *,
                      pose_reference: str | None, hardware_config: dict) -> dict:
    result = workspace_report(poses, safety, pose_reference=pose_reference,
                              hardware_config=hardware_config)
    if not result["ok"]:
        raise WorkspaceError(result)
    return result
