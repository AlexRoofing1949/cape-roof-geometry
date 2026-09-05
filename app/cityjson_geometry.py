"""Extract contractor-facing roof geometry from Roofer CityJSONSequence output.

Roofer labels LoD2.2 faces as RoofSurface, WallSurface, or GroundSurface. This
module uses only RoofSurface topology. It never estimates edge lengths from a
footprint, house area, roof type, or other generic assumption.
"""

from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import Point

from .errors import UnreliableGeometryError

METERS_TO_FEET = 3.280839895013123
SQUARE_METERS_TO_SQUARE_FEET = 10.763910416709722
EDGE_NODE_TOLERANCE_METERS = 0.10
EDGE_NODE_VERTICAL_TOLERANCE_METERS = 0.30


@dataclass(frozen=True)
class Facet:
    facet_id: str
    vertex_ids: tuple[int, ...]
    vertices: tuple[tuple[float, float, float], ...]
    area_square_meters: float
    horizontal_area_square_meters: float
    pitch_degrees: float
    azimuth_degrees: float
    centroid: tuple[float, float, float]
    normal: tuple[float, float, float]
    opening_count: int
    opening_perimeter_meters: float
    semantic_attributes: dict[str, Any]


@dataclass(frozen=True)
class EdgeUse:
    facet: Facet
    start_id: int
    end_id: int
    start: tuple[float, float, float]
    end: tuple[float, float, float]


def _vector(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return b[0] - a[0], b[1] - a[1], b[2] - a[2]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(value: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(value, value))


def _newell(ring: Iterable[tuple[float, float, float]]) -> tuple[float, float, float]:
    points = list(ring)
    nx = ny = nz = 0.0
    for current, following in zip(points, points[1:] + points[:1]):
        nx += (current[1] - following[1]) * (current[2] + following[2])
        ny += (current[2] - following[2]) * (current[0] + following[0])
        nz += (current[0] - following[0]) * (current[1] + following[1])
    return nx, ny, nz


def _centroid(points: Iterable[tuple[float, float, float]]) -> tuple[float, float, float]:
    values = list(points)
    return tuple(sum(point[index] for point in values) / len(values) for index in range(3))  # type: ignore[return-value]


def _edge_length(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return _norm(_vector(a, b))


def _projected_edge_length(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    """Return the planimetric length used to reconcile roof topology."""

    return math.hypot(b[0] - a[0], b[1] - a[1])


def _projected_edge_direction_variance_degrees(
    first: EdgeUse, second: EdgeUse
) -> float:
    """Compare boundary direction in the planar topology arrangement."""

    return _vector_alignment_degrees(
        (
            first.end[0] - first.start[0],
            first.end[1] - first.start[1],
            0.0,
        ),
        (
            second.end[0] - second.start[0],
            second.end[1] - second.start[1],
            0.0,
        ),
    )


def _horizontal_ring_area(points: Iterable[tuple[float, float, float]]) -> float:
    values = list(points)
    return abs(
        sum(
            current[0] * following[1] - following[0] * current[1]
            for current, following in zip(values, values[1:] + values[:1])
        )
    ) / 2


def _canonical_ring(
    vertex_ids: tuple[int, ...],
    points: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[int, ...], tuple[tuple[float, float, float], ...]]:
    """Return a stable CCW ring whose first vertex is coordinate-canonical."""

    pairs = list(zip(vertex_ids, points))
    signed_area = sum(
        current[1][0] * following[1][1]
        - following[1][0] * current[1][1]
        for current, following in zip(pairs, pairs[1:] + pairs[:1])
    ) / 2
    if signed_area < 0:
        pairs.reverse()
    start_index = min(
        range(len(pairs)),
        key=lambda index: tuple(round(value, 8) for value in pairs[index][1]),
    )
    pairs = pairs[start_index:] + pairs[:start_index]
    return (
        tuple(pair[0] for pair in pairs),
        tuple(pair[1] for pair in pairs),
    )


def _facet_sort_key(facet: Facet) -> tuple[Any, ...]:
    """Identify a facet from geometry instead of Roofer array position."""

    return (
        tuple(round(value, 6) for value in facet.centroid),
        round(facet.horizontal_area_square_meters, 6),
        round(facet.area_square_meters, 6),
        tuple(
            round(value, 6)
            for point in facet.vertices
            for value in point
        ),
    )


def _edge_height_at_xy(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    point: tuple[float, float, float],
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return (start[2] + end[2]) / 2
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator
    return start[2] + t * (end[2] - start[2])


def _facet_plane_height_at_xy(facet: Facet, x: float, y: float) -> float:
    """Return the fitted facet-plane elevation at a projected coordinate."""

    origin_x, origin_y, origin_z = facet.vertices[0]
    normal_x, normal_y, normal_z = facet.normal
    if abs(normal_z) <= 1e-12:
        raise ValueError("Roof facet plane is vertical and has no single XY height")
    return origin_z - (
        normal_x * (x - origin_x) + normal_y * (y - origin_y)
    ) / normal_z


def _paired_edge_vertical_separations(
    first: EdgeUse, second: EdgeUse
) -> tuple[float, float, float]:
    """Measure vertical separation at plan-corresponding edge locations.

    Roofer may reconstruct two roof levels whose boundaries are coincident in
    plan but deliberately separated in elevation by a wall.  Comparing the
    original vertex order is unsafe because adjacent rings commonly run in
    opposite directions.  The lower-cost XY endpoint pairing supplies two
    plan probes; their midpoint supplies a third guard against a crossing pair.
    """

    same_order_distance = math.hypot(
        first.start[0] - second.start[0],
        first.start[1] - second.start[1],
    ) + math.hypot(
        first.end[0] - second.end[0],
        first.end[1] - second.end[1],
    )
    reverse_order_distance = math.hypot(
        first.start[0] - second.end[0],
        first.start[1] - second.end[1],
    ) + math.hypot(
        first.end[0] - second.start[0],
        first.end[1] - second.start[1],
    )
    second_start, second_end = (
        (second.end, second.start)
        if reverse_order_distance < same_order_distance
        else (second.start, second.end)
    )
    probes = [
        (
            (first.start[0] + second_start[0]) / 2,
            (first.start[1] + second_start[1]) / 2,
            0.0,
        ),
        (
            (first.end[0] + second_end[0]) / 2,
            (first.end[1] + second_end[1]) / 2,
            0.0,
        ),
    ]
    probes.append(
        (
            (probes[0][0] + probes[1][0]) / 2,
            (probes[0][1] + probes[1][1]) / 2,
            0.0,
        )
    )
    return tuple(
        abs(
            _edge_height_at_xy(first.start, first.end, probe)
            - _edge_height_at_xy(second.start, second.end, probe)
        )
        for probe in probes
    )  # type: ignore[return-value]


def _facet_side_height_delta(use: EdgeUse) -> float:
    """Return the facet's vertical change one metre inward from a shared edge.

    A vertex-average centroid can fall outside a concave roof facet. Using that
    point can invert one side of a valid ridge, hip, or valley. The projected
    ring orientation gives the local interior side of each directed edge, so a
    unit inward probe measures the plane derivative without guessing from a
    potentially exterior centroid.
    """

    dx = use.end[0] - use.start[0]
    dy = use.end[1] - use.start[1]
    horizontal_length = math.hypot(dx, dy)
    if horizontal_length <= 1e-12:
        return 0.0
    signed_area = sum(
        current[0] * following[1] - following[0] * current[1]
        for current, following in zip(
            use.facet.vertices,
            use.facet.vertices[1:] + use.facet.vertices[:1],
        )
    ) / 2
    orientation = 1.0 if signed_area >= 0 else -1.0
    inward_x = orientation * (-dy / horizontal_length)
    inward_y = orientation * (dx / horizontal_length)
    midpoint_x = (use.start[0] + use.end[0]) / 2
    midpoint_y = (use.start[1] + use.end[1]) / 2
    probe_x = midpoint_x + inward_x
    probe_y = midpoint_y + inward_y
    boundary_height = _facet_plane_height_at_xy(
        use.facet, midpoint_x, midpoint_y
    )
    plane_height = _facet_plane_height_at_xy(use.facet, probe_x, probe_y)
    return plane_height - boundary_height


def _normal_angle_degrees(first: Facet, second: Facet) -> float:
    cosine = max(-1.0, min(1.0, _dot(first.normal, second.normal)))
    return math.degrees(math.acos(cosine))


def _edge_direction_variance_degrees(first: EdgeUse, second: EdgeUse) -> float:
    return _vector_alignment_degrees(
        _vector(first.start, first.end),
        _vector(second.start, second.end),
    )


def _vector_alignment_degrees(
    first_vector: tuple[float, float, float],
    second_vector: tuple[float, float, float],
) -> float:
    """Return the unsigned angle between two undirected 3D lines."""

    cosine = abs(
        _dot(first_vector, second_vector)
        / max(_norm(first_vector) * _norm(second_vector), 1e-12)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _edge_downslope_alignment_degrees(use: EdgeUse) -> float | None:
    """Return plan-angle from an exterior edge to its facet's downslope axis."""

    edge_direction = (
        use.end[0] - use.start[0],
        use.end[1] - use.start[1],
        0.0,
    )
    downslope_direction = (
        use.facet.normal[0] / use.facet.normal[2],
        use.facet.normal[1] / use.facet.normal[2],
        0.0,
    )
    if _norm(edge_direction) <= 1e-12 or _norm(downslope_direction) <= 1e-12:
        return None
    return _vector_alignment_degrees(edge_direction, downslope_direction)


def _distance(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> float:
    return _norm(_vector(first, second))


def _project_to_line(
    point: tuple[float, float, float],
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
) -> tuple[float, float, float]:
    denominator = _dot(direction, direction)
    parameter = _dot(_vector(origin, point), direction) / denominator
    return tuple(
        origin[index] + parameter * direction[index] for index in range(3)
    )  # type: ignore[return-value]


def _plane_intersection_line(
    first: EdgeUse, second: EdgeUse
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return a stable origin and unit direction for two incident planes."""

    direction = _cross(first.facet.normal, second.facet.normal)
    direction_squared = _dot(direction, direction)
    if direction_squared <= 1e-12:
        raise UnreliableGeometryError(
            "ROOF_PLANE_INTERSECTION_UNDEFINED",
            "Adjacent noncoplanar roof facets did not produce a stable plane intersection.",
        )
    local_anchor = tuple(
        (
            first.start[index]
            + first.end[index]
            + second.start[index]
            + second.end[index]
        )
        / 4
        for index in range(3)
    )
    first_plane_point = _vector(local_anchor, first.facet.vertices[0])
    second_plane_point = _vector(local_anchor, second.facet.vertices[0])
    first_constant = _dot(first.facet.normal, first_plane_point)
    second_constant = _dot(second.facet.normal, second_plane_point)
    first_term = _cross(second.facet.normal, direction)
    second_term = _cross(direction, first.facet.normal)
    local_origin = tuple(
        (
            first_constant * first_term[index]
            + second_constant * second_term[index]
        )
        / direction_squared
        for index in range(3)
    )
    origin = tuple(
        local_anchor[index] + local_origin[index] for index in range(3)
    )
    direction_length = math.sqrt(direction_squared)
    unit_direction = tuple(
        component / direction_length for component in direction
    )
    return origin, unit_direction  # type: ignore[return-value]


def _validated_plane_intersection_edge(
    uses: list[EdgeUse], maximum_displacement_meters: float
) -> tuple[list[EdgeUse], dict[str, Any]]:
    """Snap one shared edge to the independently derived facet-plane line.

    Roofer can return each side of a shared boundary with a small, independent
    XY/Z residual.  The two fitted planes are the measurement authority; their
    3D intersection supplies a common line while the original edge endpoints
    supply only the supported clipping limits.
    """

    if len(uses) != 2:
        raise ValueError("Exactly two edge uses are required.")
    first, second = uses
    # Roofer coordinates are usually in a projected CRS with northings in the
    # millions of metres.  The helper solves in a local frame to avoid
    # cancellation when plane constants are combined.
    origin, direction = _plane_intersection_line(first, second)

    same_order_distance = _distance(first.start, second.start) + _distance(
        first.end, second.end
    )
    reverse_order_distance = _distance(first.start, second.end) + _distance(
        first.end, second.start
    )
    second_reversed = reverse_order_distance < same_order_distance
    second_start = second.end if second_reversed else second.start
    second_end = second.start if second_reversed else second.end
    supported_start = tuple(
        (first.start[index] + second_start[index]) / 2 for index in range(3)
    )
    supported_end = tuple(
        (first.end[index] + second_end[index]) / 2 for index in range(3)
    )
    corrected_start = _project_to_line(supported_start, origin, direction)
    corrected_end = _project_to_line(supported_end, origin, direction)

    def alignment_degrees(use: EdgeUse) -> float:
        edge_vector = _vector(use.start, use.end)
        alignment_cosine = abs(
            _dot(edge_vector, direction)
            / max(_norm(edge_vector) * _norm(direction), 1e-12)
        )
        return math.degrees(
            math.acos(max(-1.0, min(1.0, alignment_cosine)))
        )

    first_alignment_degrees = alignment_degrees(first)
    second_alignment_degrees = alignment_degrees(second)
    corrected_length = _distance(corrected_start, corrected_end)
    diagnostic_evidence = {
        "facetIds": [first.facet.facet_id, second.facet.facet_id],
        "correctedLengthMeters": _round(corrected_length, 3),
        "originalEdgeLengthsMeters": [
            _round(_distance(first.start, first.end), 3),
            _round(_distance(second.start, second.end), 3),
        ],
        "originalBoundaryAlignmentDegrees": [
            _round(first_alignment_degrees, 3),
            _round(second_alignment_degrees, 3),
        ],
        "originalBoundaryDirectionVarianceDegrees": _round(
            _edge_direction_variance_degrees(first, second), 3
        ),
        "incidentPlaneAngleDegrees": _round(
            _normal_angle_degrees(first.facet, second.facet), 3
        ),
    }
    if corrected_length <= 0.10:
        raise UnreliableGeometryError(
            "ROOF_PLANE_INTERSECTION_DEGENERATE",
            "The validated roof-plane intersection is too short to measure safely.",
            details=diagnostic_evidence,
        )
    displacements = (
        _distance(first.start, corrected_start),
        _distance(first.end, corrected_end),
        _distance(second_start, corrected_start),
        _distance(second_end, corrected_end),
    )
    maximum_displacement = max(displacements)
    if maximum_displacement > maximum_displacement_meters:
        raise UnreliableGeometryError(
            "ROOF_PLANE_INTERSECTION_DISPLACEMENT_EXCEEDED",
            "A reconstructed shared boundary is too far from the incident roof-plane intersection.",
            details={
                **diagnostic_evidence,
                "maximumDisplacementMeters": _round(maximum_displacement, 3),
                "allowedDisplacementMeters": _round(
                    maximum_displacement_meters, 3
                ),
            },
        )

    corrected_second_start = corrected_end if second_reversed else corrected_start
    corrected_second_end = corrected_start if second_reversed else corrected_end
    corrected = [
        EdgeUse(
            first.facet,
            first.start_id,
            first.end_id,
            corrected_start,
            corrected_end,
        ),
        EdgeUse(
            second.facet,
            second.start_id,
            second.end_id,
            corrected_second_start,
            corrected_second_end,
        ),
    ]
    return corrected, {
        "derivation": "PLANE_PLANE_BOUNDARY_INTERSECTION",
        "maximumCorrectionMeters": _round(maximum_displacement, 3),
        "allowedCorrectionMeters": _round(maximum_displacement_meters, 3),
        "originalBoundaryAlignmentDegrees": [
            _round(first_alignment_degrees, 3),
            _round(second_alignment_degrees, 3),
        ],
        "incidentPlaneAngleDegrees": diagnostic_evidence[
            "incidentPlaneAngleDegrees"
        ],
    }


def _validated_planar_consensus_edge(
    uses: list[EdgeUse],
    *,
    maximum_planar_displacement_meters: float,
    maximum_3d_correction_meters: float,
) -> tuple[list[EdgeUse], dict[str, Any]]:
    """Average a plan-coincident seam when fitted planes are nearly parallel.

    A small independent plane-fit offset can move the mathematical
    plane-plane intersection far from a boundary even though both reconstructed
    facets measure the same plan seam and their endpoint elevations agree
    within the allowed correction.  The consensus remains fail-closed: each
    original boundary must lie on its own incident facet plane, corresponding
    endpoints must be plan-coincident, and the averaged 3D endpoints must stay
    within the configured correction distance of both measured boundaries and
    both incident planes.
    """

    if len(uses) != 2:
        raise ValueError("Exactly two edge uses are required.")
    first, second = uses
    same_order_distance = math.hypot(
        first.start[0] - second.start[0],
        first.start[1] - second.start[1],
    ) + math.hypot(
        first.end[0] - second.end[0],
        first.end[1] - second.end[1],
    )
    reverse_order_distance = math.hypot(
        first.start[0] - second.end[0],
        first.start[1] - second.end[1],
    ) + math.hypot(
        first.end[0] - second.start[0],
        first.end[1] - second.start[1],
    )
    second_reversed = reverse_order_distance < same_order_distance
    second_start = second.end if second_reversed else second.start
    second_end = second.start if second_reversed else second.end
    endpoint_pairs = ((first.start, second_start), (first.end, second_end))
    planar_displacements = [
        math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in endpoint_pairs
    ]
    if max(planar_displacements) > maximum_planar_displacement_meters:
        raise UnreliableGeometryError(
            "ROOF_PLANAR_CONSENSUS_DISPLACEMENT_EXCEEDED",
            "Candidate roof boundaries are not plan-coincident enough to form a measured seam.",
        )

    def plane_distance(facet: Facet, point: tuple[float, float, float]) -> float:
        return abs(_dot(facet.normal, _vector(facet.vertices[0], point)))

    own_plane_residuals = [
        plane_distance(use.facet, point)
        for use in (first, second)
        for point in (use.start, use.end)
    ]
    if max(own_plane_residuals) > 0.02:
        raise UnreliableGeometryError(
            "ROOF_BOUNDARY_OFF_INCIDENT_PLANE",
            "A candidate boundary is inconsistent with its reconstructed incident facet plane.",
        )

    corrected_start = tuple(
        (first.start[index] + second_start[index]) / 2 for index in range(3)
    )
    corrected_end = tuple(
        (first.end[index] + second_end[index]) / 2 for index in range(3)
    )
    corrected_points = (corrected_start, corrected_end)
    correction_distances = [
        _distance(original, corrected)
        for pair, corrected in zip(endpoint_pairs, corrected_points)
        for original in pair
    ]
    incident_plane_distances = [
        plane_distance(facet, point)
        for facet in (first.facet, second.facet)
        for point in corrected_points
    ]
    maximum_correction = max(correction_distances + incident_plane_distances)
    if maximum_correction > maximum_3d_correction_meters:
        raise UnreliableGeometryError(
            "ROOF_PLANAR_CONSENSUS_CORRECTION_EXCEEDED",
            "A plan-coincident seam exceeds the allowed 3D correction distance.",
            details={
                "maximumCorrectionMeters": _round(maximum_correction, 3),
                "allowedCorrectionMeters": _round(
                    maximum_3d_correction_meters, 3
                ),
            },
        )
    if _distance(corrected_start, corrected_end) <= 0.10:
        raise UnreliableGeometryError(
            "ROOF_PLANAR_CONSENSUS_DEGENERATE",
            "A plan-coincident seam is too short to measure safely.",
        )
    corrected_second_start = corrected_end if second_reversed else corrected_start
    corrected_second_end = corrected_start if second_reversed else corrected_end
    corrected = [
        EdgeUse(
            first.facet,
            first.start_id,
            first.end_id,
            corrected_start,
            corrected_end,
        ),
        EdgeUse(
            second.facet,
            second.start_id,
            second.end_id,
            corrected_second_start,
            corrected_second_end,
        ),
    ]
    return corrected, {
        "derivation": "PLAN_COINCIDENT_ENDPOINT_CONSENSUS",
        "maximumCorrectionMeters": _round(maximum_correction, 3),
        "allowedCorrectionMeters": _round(maximum_3d_correction_meters, 3),
        "maximumPlanarDisplacementMeters": _round(
            max(planar_displacements), 3
        ),
        "allowedPlanarDisplacementMeters": _round(
            maximum_planar_displacement_meters, 3
        ),
        "incidentPlaneAngleDegrees": _round(
            _normal_angle_degrees(first.facet, second.facet), 3
        ),
    }


def _validated_plane_supported_overlap(
    first: EdgeUse,
    second: EdgeUse,
    maximum_displacement_meters: float,
    minimum_overlap_ratio: float,
    minimum_overlap_meters: float = 0.20,
) -> tuple[list[EdgeUse], dict[str, Any]]:
    """Clip fragmented facet boundaries to their common validated plane line."""

    origin, direction = _plane_intersection_line(first, second)

    def parameter(point: tuple[float, float, float]) -> float:
        return _dot(_vector(origin, point), direction)

    def point_at(value: float) -> tuple[float, float, float]:
        return tuple(
            origin[index] + value * direction[index] for index in range(3)
        )  # type: ignore[return-value]

    endpoint_distances: list[float] = []
    intervals: list[tuple[float, float]] = []
    for use in (first, second):
        values = [parameter(use.start), parameter(use.end)]
        intervals.append((min(values), max(values)))
        endpoint_distances.extend(
            _distance(point, point_at(value))
            for point, value in zip((use.start, use.end), values)
        )
    maximum_distance = max(endpoint_distances)
    if maximum_distance > maximum_displacement_meters:
        raise UnreliableGeometryError(
            "ROOF_BOUNDARY_PLANE_SUPPORT_DISPLACEMENT_EXCEEDED",
            "A fragmented facet boundary is too far from the incident plane intersection.",
            details={
                "maximumDisplacementMeters": _round(maximum_distance, 3),
                "allowedDisplacementMeters": _round(
                    maximum_displacement_meters, 3
                ),
            },
        )
    overlap_start = max(interval[0] for interval in intervals)
    overlap_end = min(interval[1] for interval in intervals)
    overlap_length = overlap_end - overlap_start
    if overlap_length < minimum_overlap_meters:
        raise UnreliableGeometryError(
            "ROOF_BOUNDARY_PLANE_SUPPORT_NO_OVERLAP",
            "Fragmented facet boundaries do not share enough support on the incident plane intersection.",
        )
    support_lengths = [end - start for start, end in intervals]
    overlap_ratio = overlap_length / max(min(support_lengths), 1e-12)
    if overlap_ratio < minimum_overlap_ratio:
        raise UnreliableGeometryError(
            "ROOF_BOUNDARY_PLANE_SUPPORT_OVERLAP_TOO_LOW",
            "Fragmented facet boundaries have insufficient mutual support on the incident plane intersection.",
            details={
                "overlapRatio": _round(overlap_ratio, 4),
                "minimumOverlapRatio": _round(minimum_overlap_ratio, 4),
            },
        )
    low = point_at(overlap_start)
    high = point_at(overlap_end)

    def corrected_use(use: EdgeUse) -> EdgeUse:
        ordered = (low, high)
        if _dot(_vector(use.start, use.end), direction) < 0:
            ordered = (high, low)
        return EdgeUse(
            use.facet,
            use.start_id,
            use.end_id,
            ordered[0],
            ordered[1],
        )

    corrected = [corrected_use(first), corrected_use(second)]
    return corrected, {
        "derivation": "PLANE_INTERSECTION_SUPPORTED_FRAGMENT_OVERLAP",
        "correctedLengthMeters": _round(overlap_length, 3),
        "maximumCorrectionMeters": _round(maximum_distance, 3),
        "allowedCorrectionMeters": _round(maximum_displacement_meters, 3),
        "overlapRatio": _round(overlap_ratio, 4),
        "minimumOverlapRatio": _round(minimum_overlap_ratio, 4),
        "incidentPlaneAngleDegrees": _round(
            _normal_angle_degrees(first.facet, second.facet), 3
        ),
    }


def _validated_coplanar_boundary_overlap(
    first: EdgeUse,
    second: EdgeUse,
    maximum_displacement_meters: float,
    minimum_overlap_ratio: float,
    minimum_overlap_meters: float = 0.20,
) -> dict[str, Any]:
    """Validate one redundant boundary between two nearly coplanar facets."""

    first_direction = _vector(first.start, first.end)
    second_direction = _vector(second.start, second.end)
    first_length = _norm(first_direction)
    second_length = _norm(second_direction)
    if min(first_length, second_length) <= 1e-12:
        raise UnreliableGeometryError(
            "ROOF_COPLANAR_BOUNDARY_DEGENERATE",
            "A coplanar boundary fragment is too short to validate.",
        )
    first_unit = tuple(value / first_length for value in first_direction)
    second_unit = tuple(value / second_length for value in second_direction)

    def line_distance(
        point: tuple[float, float, float],
        origin: tuple[float, float, float],
        direction: tuple[float, float, float],
    ) -> float:
        projected = _project_to_line(point, origin, direction)
        return _distance(point, projected)

    boundary_displacements = [
        line_distance(point, first.start, first_unit)
        for point in (second.start, second.end)
    ] + [
        line_distance(point, second.start, second_unit)
        for point in (first.start, first.end)
    ]

    def plane_distance(facet: Facet, point: tuple[float, float, float]) -> float:
        return abs(_dot(facet.normal, _vector(facet.vertices[0], point)))

    plane_displacements = [
        plane_distance(first.facet, point)
        for point in (second.start, second.end)
    ] + [
        plane_distance(second.facet, point)
        for point in (first.start, first.end)
    ]
    maximum_distance = max(boundary_displacements + plane_displacements)
    if maximum_distance > maximum_displacement_meters:
        raise UnreliableGeometryError(
            "ROOF_COPLANAR_BOUNDARY_DISPLACEMENT_EXCEEDED",
            "Coplanar facet fragments are too far apart to suppress safely.",
            details={
                "maximumDisplacementMeters": _round(maximum_distance, 3),
                "allowedDisplacementMeters": _round(
                    maximum_displacement_meters, 3
                ),
            },
        )
    second_parameters = [
        _dot(_vector(first.start, point), first_unit)
        for point in (second.start, second.end)
    ]
    overlap_start = max(0.0, min(second_parameters))
    overlap_end = min(first_length, max(second_parameters))
    overlap_length = overlap_end - overlap_start
    if overlap_length < minimum_overlap_meters:
        raise UnreliableGeometryError(
            "ROOF_COPLANAR_BOUNDARY_NO_OVERLAP",
            "Coplanar facet fragments do not share enough boundary support.",
        )
    overlap_ratio = overlap_length / max(min(first_length, second_length), 1e-12)
    if overlap_ratio < minimum_overlap_ratio:
        raise UnreliableGeometryError(
            "ROOF_COPLANAR_BOUNDARY_OVERLAP_TOO_LOW",
            "Coplanar facet fragments have insufficient mutual boundary support.",
            details={
                "overlapRatio": _round(overlap_ratio, 4),
                "minimumOverlapRatio": _round(minimum_overlap_ratio, 4),
            },
        )
    return {
        "pairingDerivation": "MUTUAL_UNIQUE_COPLANAR_BOUNDARY_PAIR",
        "suppressedLengthMeters": _round(overlap_length, 3),
        "maximumCorrectionMeters": _round(maximum_distance, 3),
        "allowedCorrectionMeters": _round(maximum_displacement_meters, 3),
        "overlapRatio": _round(overlap_ratio, 4),
        "minimumOverlapRatio": _round(minimum_overlap_ratio, 4),
        "incidentPlaneAngleDegrees": _round(
            _normal_angle_degrees(first.facet, second.facet), 3
        ),
    }


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _canonical_topology_graph(
    classified: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Expose deterministic projected endpoints without changing measurements."""

    raw_points = {
        tuple(round(float(value), 6) for value in point)
        for entries in classified.values()
        for edge in entries
        for point in edge.get("_geometryMeters", ())
    }
    ordered_points = sorted(raw_points)
    vertex_ids = {
        point: f"V-{index:06d}"
        for index, point in enumerate(ordered_points, start=1)
    }
    incident_facets: dict[tuple[float, float, float], set[str]] = {
        point: set() for point in ordered_points
    }
    type_names = {
        "rakes": "RAKE",
        "eaves": "EAVE",
        "valleys": "VALLEY",
        "ridges": "RIDGE",
        "hips": "HIP",
        "highPerimeters": "HIGH_PERIMETER",
    }
    graph_edges: list[dict[str, Any]] = []
    for kind in (
        "eaves",
        "rakes",
        "ridges",
        "hips",
        "valleys",
        "highPerimeters",
    ):
        for edge in classified[kind]:
            raw_geometry = edge.pop("_geometryMeters")
            start = tuple(round(float(value), 6) for value in raw_geometry[0])
            end = tuple(round(float(value), 6) for value in raw_geometry[-1])
            if kind in {"rakes", "hips", "valleys"}:
                if (end[2], end[0], end[1]) < (start[2], start[0], start[1]):
                    start, end = end, start
            elif end < start:
                start, end = end, start
            geometry = [list(start), list(end)]
            length_feet = _round(_edge_length(start, end) * METERS_TO_FEET)
            edge["startVertexId"] = vertex_ids[start]
            edge["endVertexId"] = vertex_ids[end]
            edge["geometryMeters"] = geometry
            edge["lengthFeet"] = length_feet
            for point in (start, end):
                incident_facets[point].update(str(value) for value in edge["facetIds"])
            graph_edges.append({"type": type_names[kind], **edge})

    graph_edges.sort(
        key=lambda edge: (
            edge["type"],
            edge["startVertexId"],
            edge["endVertexId"],
            tuple(edge["facetIds"]),
        )
    )
    vertices = [
        {
            "vertexId": vertex_ids[point],
            "x": point[0],
            "y": point[1],
            "z": point[2],
            "incidentFacetIds": sorted(incident_facets[point]),
            "source": "RECONSTRUCTED_ROOF_TOPOLOGY",
        }
        for point in ordered_points
    ]
    normalized = json.dumps(
        {"vertices": vertices, "edges": graph_edges},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return vertices, graph_edges, hashlib.sha256(normalized).hexdigest()


def _edge_parameter(
    point: tuple[float, float, float],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    tolerance_meters: float,
    vertical_tolerance_meters: float,
) -> float | None:
    direction_x = end[0] - start[0]
    direction_y = end[1] - start[1]
    denominator = direction_x * direction_x + direction_y * direction_y
    if denominator <= 1e-12:
        return None
    parameter = (
        (point[0] - start[0]) * direction_x
        + (point[1] - start[1]) * direction_y
    ) / denominator
    length = math.sqrt(denominator)
    parameter_tolerance = tolerance_meters / length
    if parameter < -parameter_tolerance or parameter > 1 + parameter_tolerance:
        return None
    bounded = max(0.0, min(1.0, parameter))
    projected_x = start[0] + bounded * direction_x
    projected_y = start[1] + bounded * direction_y
    projected_z = start[2] + bounded * (end[2] - start[2])
    if math.hypot(point[0] - projected_x, point[1] - projected_y) > tolerance_meters:
        return None
    if abs(point[2] - projected_z) > vertical_tolerance_meters:
        return None
    return bounded


def _noded_edge_uses(
    facets: list[Facet],
    tolerance_meters: float = EDGE_NODE_TOLERANCE_METERS,
    vertical_tolerance_meters: float = EDGE_NODE_VERTICAL_TOLERANCE_METERS,
) -> dict[tuple[int, int], list[EdgeUse]]:
    """Build facet adjacency from plan-coincident, elevation-compatible edges.

    Roofer can emit coincident coordinates with different CityJSON vertex IDs,
    and a long edge on one facet can meet two shorter edges at a T-junction.
    Independent plan and elevation tolerances prevent small plane-fit height
    residuals from duplicating internal lines while keeping vertically stacked
    roof boundaries separate.
    """

    representatives: list[tuple[float, float, float]] = []
    point_keys: dict[tuple[float, float, float], int] = {}
    for facet in facets:
        for point in facet.vertices:
            if point in point_keys:
                continue
            key = next(
                (
                    index
                    for index, representative in enumerate(representatives)
                    if (
                        math.hypot(
                            point[0] - representative[0],
                            point[1] - representative[1],
                        )
                        <= tolerance_meters
                        and abs(point[2] - representative[2])
                        <= vertical_tolerance_meters
                    )
                ),
                None,
            )
            if key is None:
                key = len(representatives)
                representatives.append(point)
            point_keys[point] = key

    edge_uses: dict[tuple[int, int], list[EdgeUse]] = defaultdict(list)
    all_points = list(point_keys.items())
    for facet in facets:
        count = len(facet.vertex_ids)
        for index, start_id in enumerate(facet.vertex_ids):
            end_id = facet.vertex_ids[(index + 1) % count]
            start = facet.vertices[index]
            end = facet.vertices[(index + 1) % count]
            if _edge_length(start, end) <= 0.10:
                continue
            nodes: list[tuple[float, int, tuple[float, float, float]]] = []
            for point, key in all_points:
                parameter = _edge_parameter(
                    point,
                    start,
                    end,
                    tolerance_meters,
                    vertical_tolerance_meters,
                )
                if parameter is not None:
                    nodes.append((parameter, key, point))
            nodes.sort(key=lambda node: node[0])
            distinct: list[tuple[float, int, tuple[float, float, float]]] = []
            for node in nodes:
                if distinct and node[1] == distinct[-1][1]:
                    continue
                distinct.append(node)
            for current, following in zip(distinct, distinct[1:]):
                if current[1] == following[1]:
                    continue
                segment_start = tuple(
                    start[axis] + current[0] * (end[axis] - start[axis])
                    for axis in range(3)
                )
                segment_end = tuple(
                    start[axis] + following[0] * (end[axis] - start[axis])
                    for axis in range(3)
                )
                if _edge_length(segment_start, segment_end) <= 0.10:
                    continue
                edge_key = tuple(sorted((current[1], following[1])))
                if any(use.facet.facet_id == facet.facet_id for use in edge_uses[edge_key]):
                    continue
                edge_uses[edge_key].append(
                    EdgeUse(facet, start_id, end_id, segment_start, segment_end)
                )
    return edge_uses


def _reconcile_offset_shared_boundaries(
    edge_uses: dict[tuple[int, int], list[EdgeUse]],
    *,
    roofprint_boundary: Any | None,
    exterior_boundary_maximum_distance_meters: float,
    maximum_displacement_meters: float,
    coplanar_tolerance_degrees: float,
    minimum_overlap_ratio: float = 0.80,
) -> tuple[
    dict[tuple[int, int], list[EdgeUse]],
    dict[tuple[int, int], dict[str, Any]],
    dict[str, Any],
]:
    """Pair mutually unique Roofer seams that missed strict coordinate noding.

    A Roofer boundary is eligible only when the roofprint proves that it is not
    exterior.  Candidate sides must independently agree in direction, length,
    endpoint support, incident planes, and overlapping support along the
    derived plane-plane intersection. Ambiguous candidates are left unmatched so
    the downstream safety gate requires an inspection instead of guessing.
    """

    raw_edge_count = len(edge_uses)
    if roofprint_boundary is None:
        return dict(edge_uses), {}, {
            "rawNodedEdgeCount": raw_edge_count,
            "eligibleOffsetBoundaryCount": 0,
            "offsetBoundaryPairCount": 0,
            "offsetBoundaryCandidateCount": 0,
            "repairedSharedBoundaryCount": 0,
            "repairedSharedBoundaryFeet": 0.0,
            "suppressedCoplanarBoundaryCount": 0,
            "suppressedCoplanarBoundaryFeet": 0.0,
            "ambiguousOffsetBoundaryCount": 0,
            "unpairedInteriorBoundaryCount": 0,
            "minimumBoundaryOverlapRatio": _round(
                minimum_overlap_ratio, 3
            ),
            "offsetBoundaryRejectionCounts": {},
        }

    eligible: list[tuple[tuple[int, int], EdgeUse]] = []
    for key, uses in edge_uses.items():
        if len(uses) != 1:
            continue
        use = uses[0]
        midpoint = Point(
            (use.start[0] + use.end[0]) / 2,
            (use.start[1] + use.end[1]) / 2,
        )
        if float(roofprint_boundary.distance(midpoint)) > exterior_boundary_maximum_distance_meters:
            eligible.append((key, use))
    eligible.sort(key=lambda item: item[0])

    candidates: list[
        tuple[
            tuple[int, int],
            tuple[int, int],
            list[EdgeUse] | None,
            dict[str, Any],
        ]
    ] = []
    candidates_by_key: dict[tuple[int, int], list[int]] = defaultdict(list)
    pair_count = 0
    rejection_counts: dict[str, int] = defaultdict(int)
    for first_index, (first_key, first) in enumerate(eligible):
        for second_key, second in eligible[first_index + 1 :]:
            pair_count += 1
            if first.facet.facet_id == second.facet.facet_id:
                rejection_counts["SAME_FACET"] += 1
                continue
            direction_variance = _edge_direction_variance_degrees(first, second)
            first_length = _edge_length(first.start, first.end)
            second_length = _edge_length(second.start, second.end)
            length_agreement = min(first_length, second_length) / max(
                first_length, second_length, 1e-12
            )
            plane_angle = _normal_angle_degrees(first.facet, second.facet)
            if plane_angle <= coplanar_tolerance_degrees:
                try:
                    coplanar_evidence = _validated_coplanar_boundary_overlap(
                        first,
                        second,
                        maximum_displacement_meters,
                        minimum_overlap_ratio,
                    )
                except UnreliableGeometryError as error:
                    rejection_counts[error.code] += 1
                    continue
                corrected = None
                evidence = {
                    **coplanar_evidence,
                    "facetIds": sorted(
                        [first.facet.facet_id, second.facet.facet_id]
                    ),
                    "lengthAgreementRatio": _round(length_agreement, 4),
                    "directionVarianceDegrees": _round(direction_variance, 3),
                }
            else:
                try:
                    corrected, intersection = _validated_plane_supported_overlap(
                        first,
                        second,
                        maximum_displacement_meters,
                        minimum_overlap_ratio,
                    )
                except UnreliableGeometryError as error:
                    rejection_counts[error.code] += 1
                    continue
                evidence = {
                    "pairingDerivation": "MUTUAL_UNIQUE_PLANE_SUPPORTED_BOUNDARY_PAIR",
                    "facetIds": sorted(
                        [first.facet.facet_id, second.facet.facet_id]
                    ),
                    "lengthAgreementRatio": _round(length_agreement, 4),
                    "maximumAllowedCorrectionMeters": _round(
                        maximum_displacement_meters, 3
                    ),
                    "directionVarianceDegrees": _round(direction_variance, 3),
                    "incidentPlaneAngleDegrees": _round(plane_angle, 3),
                    "planeIntersection": intersection,
                }
            candidate_index = len(candidates)
            candidates.append(
                (first_key, second_key, corrected, evidence)
            )
            candidates_by_key[first_key].append(candidate_index)
            candidates_by_key[second_key].append(candidate_index)

    reconciled = dict(edge_uses)
    repaired_evidence: dict[tuple[int, int], dict[str, Any]] = {}
    repaired_lengths: list[float] = []
    suppressed_coplanar_lengths: list[float] = []
    next_node = max(
        (node for key in edge_uses for node in key), default=-1
    ) + 1
    consumed: set[tuple[int, int]] = set()
    for candidate_index, (first_key, second_key, corrected, evidence) in enumerate(candidates):
        if first_key in consumed or second_key in consumed:
            continue
        if candidates_by_key[first_key] != [candidate_index]:
            continue
        if candidates_by_key[second_key] != [candidate_index]:
            continue
        reconciled.pop(first_key, None)
        reconciled.pop(second_key, None)
        if corrected is None:
            suppressed_coplanar_lengths.append(
                float(evidence["suppressedLengthMeters"])
            )
        else:
            repaired_key = (next_node, next_node + 1)
            next_node += 2
            reconciled[repaired_key] = corrected
            repaired_evidence[repaired_key] = evidence
            repaired_lengths.append(
                _edge_length(corrected[0].start, corrected[0].end)
            )
        consumed.update((first_key, second_key))

    ambiguous_keys = {
        key for key, matches in candidates_by_key.items() if len(matches) > 1
    }
    return reconciled, repaired_evidence, {
        "rawNodedEdgeCount": raw_edge_count,
        "eligibleOffsetBoundaryCount": len(eligible),
        "offsetBoundaryPairCount": pair_count,
        "offsetBoundaryCandidateCount": len(candidates),
        "repairedSharedBoundaryCount": len(repaired_evidence),
        "repairedSharedBoundaryFeet": _round(
            sum(repaired_lengths) * METERS_TO_FEET
        ),
        "suppressedCoplanarBoundaryCount": len(suppressed_coplanar_lengths),
        "suppressedCoplanarBoundaryFeet": _round(
            sum(suppressed_coplanar_lengths) * METERS_TO_FEET
        ),
        "ambiguousOffsetBoundaryCount": len(ambiguous_keys),
        "unpairedInteriorBoundaryCount": len(eligible) - len(consumed),
        "minimumBoundaryOverlapRatio": _round(
            minimum_overlap_ratio, 3
        ),
        "offsetBoundaryRejectionCounts": dict(sorted(rejection_counts.items())),
    }


def _decode_vertices(
    encoded: list[list[float]], transform: dict[str, list[float]] | None
) -> list[tuple[float, float, float]]:
    if not transform:
        return [tuple(map(float, point)) for point in encoded]  # type: ignore[list-item]
    scale = transform.get("scale", [1, 1, 1])
    translate = transform.get("translate", [0, 0, 0])
    if len(scale) != 3 or len(translate) != 3:
        raise UnreliableGeometryError("CITYJSON_TRANSFORM_INVALID", "Roofer returned an invalid CityJSON transform.")
    return [
        tuple(float(point[index]) * float(scale[index]) + float(translate[index]) for index in range(3))
        for point in encoded
    ]  # type: ignore[list-item]


def load_cityjson_feature(feature_path: Path, metadata_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Load one Roofer CityJSONFeature and the transform that applies to it."""

    header: dict[str, Any] | None = None
    features: list[dict[str, Any]] = []
    with feature_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("type") == "CityJSON":
                header = item
            elif item.get("type") == "CityJSONFeature":
                features.append(item)
    if len(features) != 1:
        raise UnreliableGeometryError(
            "CITYJSON_FEATURE_COUNT_INVALID",
            "Roofer did not return exactly one reconstructed building for the requested property.",
        )
    if header is None and metadata_path and metadata_path.exists():
        with metadata_path.open("r", encoding="utf-8") as handle:
            header = json.load(handle)
    transform = features[0].get("transform") or (header or {}).get("transform")
    return features[0], transform


def _roof_facets(feature: dict[str, Any], transform: dict[str, Any] | None) -> tuple[list[Facet], dict[str, Any]]:
    vertices = _decode_vertices(feature.get("vertices", []), transform)
    if not vertices:
        raise UnreliableGeometryError("CITYJSON_VERTICES_MISSING", "Roofer returned no reconstructed vertices.")

    parent_attributes: dict[str, Any] = {}
    raw_facets: list[tuple[list[list[int]], dict[str, Any]]] = []
    geometry_summaries: list[dict[str, Any]] = []

    def list_depth(value: Any) -> int:
        if not isinstance(value, list):
            return 0
        return 1 + max((list_depth(item) for item in value), default=0)

    for city_object in feature.get("CityObjects", {}).values():
        if city_object.get("type") == "Building":
            parent_attributes.update(city_object.get("attributes") or {})
        if city_object.get("type") not in {"Building", "BuildingPart"}:
            continue
        for geometry in city_object.get("geometry") or []:
            semantics = geometry.get("semantics") or {}
            geometry_summaries.append(
                {
                    "cityObjectType": str(city_object.get("type") or ""),
                    "geometryType": str(geometry.get("type") or ""),
                    "lod": str(geometry.get("lod") or ""),
                    "boundaryDepth": list_depth(geometry.get("boundaries")),
                    "semanticValueDepth": list_depth(semantics.get("values")),
                    "semanticSurfaceTypes": [
                        str(surface.get("type") or "")
                        for surface in (semantics.get("surfaces") or [])
                        if isinstance(surface, dict)
                    ],
                }
            )
            if str(geometry.get("lod")) != "2.2":
                continue
            geometry_type = str(geometry.get("type") or "")
            boundaries = geometry.get("boundaries") or []
            surfaces = semantics.get("surfaces") or []
            semantic_values = semantics.get("values") or []
            if geometry_type == "Solid":
                if len(boundaries) != len(semantic_values):
                    raise UnreliableGeometryError(
                        "CITYJSON_SEMANTICS_INVALID",
                        "Roofer surface labels do not match the reconstructed shell.",
                    )
                surface_groups = zip(boundaries, semantic_values)
            elif geometry_type in {"MultiSurface", "CompositeSurface"}:
                # Roofer's CityJSON Sequence writer emits LoD2.2 building
                # geometry as a MultiSurface.  CityJSON stores its semantic
                # indices one level shallower than a Solid.
                surface_groups = [(boundaries, semantic_values)]
            else:
                continue
            for surface_boundaries, surface_semantics in surface_groups:
                if len(surface_boundaries) != len(surface_semantics):
                    raise UnreliableGeometryError(
                        "CITYJSON_SEMANTICS_INVALID",
                        "Roofer surface labels do not match reconstructed faces.",
                    )
                for rings, semantic_index in zip(surface_boundaries, surface_semantics):
                    if semantic_index is None or semantic_index >= len(surfaces):
                        continue
                    semantic = surfaces[semantic_index]
                    if semantic.get("type") == "RoofSurface":
                        raw_facets.append((rings, semantic))

    if not raw_facets:
        raise UnreliableGeometryError(
            "ROOF_FACETS_MISSING",
            "Roofer did not reconstruct any LoD2.2 roof surfaces.",
            details={"cityJsonGeometrySummary": geometry_summaries},
        )

    facets: list[Facet] = []
    for index, (rings, semantic) in enumerate(raw_facets, start=1):
        decoded_rings: list[tuple[tuple[int, ...], tuple[tuple[float, float, float], ...]]] = []
        for ring in rings:
            ring_ids = tuple(int(value) for value in ring)
            if len(ring_ids) < 3 or any(value < 0 or value >= len(vertices) for value in ring_ids):
                raise UnreliableGeometryError(
                    "ROOF_FACET_INVALID", "Roofer returned an invalid roof facet boundary."
                )
            decoded_rings.append(
                _canonical_ring(
                    ring_ids,
                    tuple(vertices[value] for value in ring_ids),
                )
            )
        if not decoded_rings:
            raise UnreliableGeometryError(
                "ROOF_FACET_INVALID", "Roofer returned a roof facet without a boundary."
            )
        ids, points = decoded_rings[0]
        area_vector = _newell(points)
        exterior_area = _norm(area_vector) / 2
        exterior_horizontal_area = _horizontal_ring_area(points)
        opening_area = 0.0
        opening_horizontal_area = 0.0
        opening_perimeter = 0.0
        for _, opening_points in decoded_rings[1:]:
            opening_vector = _newell(opening_points)
            opening_vector_length = _norm(opening_vector)
            if opening_vector_length <= 0.02:
                raise UnreliableGeometryError(
                    "ROOF_OPENING_INVALID", "Roofer returned a degenerate roof opening."
                )
            opening_area += opening_vector_length / 2
            opening_horizontal_area += _horizontal_ring_area(opening_points)
            opening_perimeter += sum(
                _edge_length(start, end)
                for start, end in zip(opening_points, opening_points[1:] + opening_points[:1])
            )
        area = exterior_area - opening_area
        horizontal_area = exterior_horizontal_area - opening_horizontal_area
        if area <= 0.25:
            raise UnreliableGeometryError("ROOF_FACET_TOO_SMALL", "Roofer returned a degenerate roof facet.")
        normal_length = _norm(area_vector)
        normal = tuple(component / normal_length for component in area_vector)
        if normal[2] < 0:
            normal = tuple(-component for component in normal)
        if normal[2] <= 1e-5:
            raise UnreliableGeometryError("ROOF_FACET_VERTICAL", "A reconstructed roof facet is vertical and cannot be priced safely.")
        pitch = math.degrees(math.acos(max(-1.0, min(1.0, normal[2]))))
        cosine = math.cos(math.radians(pitch))
        slope_area_from_projection = horizontal_area / cosine
        area_variance = abs(slope_area_from_projection - area) / area * 100
        if area_variance > 0.5:
            raise UnreliableGeometryError(
                "ROOF_AREA_FORMULA_MISMATCH",
                "The reconstructed 3D facet area does not reconcile with horizontal area divided by cosine of pitch.",
                details={"facetId": f"F{index}", "areaVariancePercent": _round(area_variance, 3)},
            )
        downslope_x = normal[0] / normal[2]
        downslope_y = normal[1] / normal[2]
        azimuth = math.degrees(math.atan2(downslope_x, downslope_y)) % 360
        semantic_pitch = semantic.get("rf_slope")
        if semantic_pitch is not None and abs(float(semantic_pitch) - pitch) > 3:
            raise UnreliableGeometryError("ROOF_PITCH_SEMANTICS_MISMATCH", "Roofer's roof slope and reconstructed plane disagree.")
        facets.append(
            Facet(
                facet_id=f"F{index}",
                vertex_ids=ids,
                vertices=points,
                area_square_meters=area,
                horizontal_area_square_meters=horizontal_area,
                pitch_degrees=pitch,
                azimuth_degrees=azimuth,
                centroid=_centroid(points),
                normal=normal,  # type: ignore[arg-type]
                opening_count=max(0, len(decoded_rings) - 1),
                opening_perimeter_meters=opening_perimeter,
                semantic_attributes=dict(semantic),
            )
        )
    facets.sort(key=_facet_sort_key)
    facets = [
        replace(facet, facet_id=f"F{index}")
        for index, facet in enumerate(facets, start=1)
    ]
    return facets, parent_attributes


def _quality_confidence(attributes: dict[str, Any], minimum_density: float, maximum_nodata_fraction: float, maximum_rmse: float) -> tuple[float, dict[str, float]]:
    if attributes.get("rf_success") is False or attributes.get("rf_pointcloud_unusable") is True:
        raise UnreliableGeometryError("ROOFER_RECONSTRUCTION_UNUSABLE", "Roofer marked the source point cloud or reconstruction unusable.")
    if str(attributes.get("rf_extrusion_mode", "standard")) != "standard":
        raise UnreliableGeometryError("ROOFER_FALLBACK_MODEL", "Roofer used a fallback extrusion instead of verified LoD2.2 roof planes.")
    density = float(attributes.get("rf_pt_density", 0) or 0)
    nodata = float(attributes.get("rf_nodata_frac", 1) or 1)
    rmse = float(attributes.get("rf_rmse_lod22", math.inf) or math.inf)
    if density < minimum_density:
        raise UnreliableGeometryError(
            "LIDAR_DENSITY_TOO_LOW",
            "The available LiDAR point density is below the verified-measurement threshold.",
            details={
                "pointDensityPpsm": round(density, 4),
                "minimumPointDensityPpsm": minimum_density,
            },
        )
    if nodata > maximum_nodata_fraction:
        raise UnreliableGeometryError(
            "LIDAR_COVERAGE_INCOMPLETE",
            "The available LiDAR has excessive missing coverage over the roof.",
            details={
                "noDataFraction": round(nodata, 4),
                "maximumNoDataFraction": maximum_nodata_fraction,
            },
        )
    if rmse > maximum_rmse:
        raise UnreliableGeometryError(
            "ROOFER_RMSE_TOO_HIGH",
            "The reconstructed roof exceeds the maximum geometry error threshold.",
            details={
                "rooferRmseMeters": round(rmse, 4),
                "maximumRooferRmseMeters": maximum_rmse,
            },
        )
    components = {
        "density": min(1.0, density / max(minimum_density * 1.5, 0.01)),
        "coverage": max(0.0, min(1.0, 1 - nodata / max(maximum_nodata_fraction, 0.001))),
        "rmse": max(0.0, min(1.0, 1 - rmse / max(maximum_rmse, 0.001))),
    }
    confidence = components["density"] * 0.35 + components["coverage"] * 0.35 + components["rmse"] * 0.30
    return confidence, components


def extract_roof_geometry(
    feature: dict[str, Any],
    transform: dict[str, Any] | None,
    *,
    flat_pitch_degrees: float = 5.0,
    horizontal_edge_tolerance_meters: float = 0.15,
    plane_side_tolerance_meters: float = 0.08,
    coplanar_tolerance_degrees: float = 3.0,
    edge_node_tolerance_meters: float = EDGE_NODE_TOLERANCE_METERS,
    edge_node_vertical_tolerance_meters: float = EDGE_NODE_VERTICAL_TOLERANCE_METERS,
    plane_intersection_maximum_displacement_meters: float = 0.35,
    shared_edge_maximum_direction_variance_degrees: float = 5.0,
    roofprint_boundary: Any | None = None,
    exterior_boundary_maximum_distance_meters: float = 0.50,
    minimum_density: float = 8.0,
    maximum_nodata_fraction: float = 0.10,
    maximum_rmse_meters: float = 0.35,
    include_validation_facets: bool = False,
) -> dict[str, Any]:
    """Return measured facets and classified roof lines from one Roofer model."""

    facets, attributes = _roof_facets(feature, transform)
    quality_confidence, quality_components = _quality_confidence(
        attributes, minimum_density, maximum_nodata_fraction, maximum_rmse_meters
    )

    edge_uses = _noded_edge_uses(
        facets,
        edge_node_tolerance_meters,
        # Build one deterministic planar arrangement first.  Elevation is
        # validated below per paired segment so vertically separated roof
        # levels are recorded as measured transitions instead of leaking into
        # exterior-edge or missing-seam failures.
        math.inf,
    )
    edge_uses, repaired_edge_evidence, boundary_repair_audit = (
        _reconcile_offset_shared_boundaries(
            edge_uses,
            roofprint_boundary=roofprint_boundary,
            exterior_boundary_maximum_distance_meters=(
                exterior_boundary_maximum_distance_meters
            ),
            maximum_displacement_meters=(
                plane_intersection_maximum_displacement_meters
            ),
            coplanar_tolerance_degrees=coplanar_tolerance_degrees,
        )
    )

    classified: dict[str, list[dict[str, Any]]] = {
        "rakes": [],
        "eaves": [],
        "valleys": [],
        "ridges": [],
        "hips": [],
        "highPerimeters": [],
    }
    ambiguous: list[dict[str, Any]] = []
    rejected_noded_adjacencies: list[dict[str, Any]] = []
    unmatched_interior_boundaries: list[dict[str, Any]] = []
    vertical_level_transitions: list[dict[str, Any]] = []
    planar_consensus_lengths: list[float] = []
    counters: dict[str, int] = defaultdict(int)
    edge_prefixes = {
        "rakes": "RA",
        "eaves": "EA",
        "valleys": "VA",
        "ridges": "RI",
        "hips": "HI",
        "highPerimeters": "HP",
    }

    def add_edge(
        kind: str,
        uses: list[EdgeUse],
        classification_evidence: dict[str, Any] | None = None,
    ) -> None:
        counters[kind] += 1
        first = uses[0]
        entry = {
            "edgeId": f"{edge_prefixes[kind]}{counters[kind]}",
            "lengthFeet": _round(_edge_length(first.start, first.end) * METERS_TO_FEET),
            "projectedLengthFeet": _round(
                _projected_edge_length(first.start, first.end) * METERS_TO_FEET
            ),
            "facetIds": [use.facet.facet_id for use in uses],
            "_geometryMeters": [first.start, first.end],
            "classificationEvidence": classification_evidence
            or {
                "derivation": "RECONSTRUCTED_FACET_BOUNDARY",
                "adjacentFacetCount": len(uses),
            },
        }
        classified[kind].append(entry)

    def classify_exterior(
        use: EdgeUse, classification_evidence: dict[str, Any] | None = None
    ) -> None:
        elevation_change = abs(use.start[2] - use.end[2])
        edge_height = (use.start[2] + use.end[2]) / 2
        if (
            elevation_change <= horizontal_edge_tolerance_meters
            and edge_height > use.facet.centroid[2] + plane_side_tolerance_meters
        ):
            # A horizontal high-side boundary with only one adjacent roof
            # plane is a measured perimeter/flashing edge, not an eave,
            # rake, ridge, hip, or valley. Keep the category explicit for
            # pricing at every pitch instead of guessing a standard type.
            add_edge("highPerimeters", [use], classification_evidence)
            return

        boundary_evidence = classification_evidence
        if roofprint_boundary is not None:
            midpoint = Point(
                (use.start[0] + use.end[0]) / 2,
                (use.start[1] + use.end[1]) / 2,
            )
            distance = float(roofprint_boundary.distance(midpoint))
            if distance > exterior_boundary_maximum_distance_meters:
                unmatched_interior_boundaries.append(
                    {
                        "facetId": use.facet.facet_id,
                        "lengthFeet": _round(
                            _edge_length(use.start, use.end) * METERS_TO_FEET
                        ),
                        "projectedLengthFeet": _round(
                            _projected_edge_length(use.start, use.end)
                            * METERS_TO_FEET
                        ),
                        "roofprintDistanceMeters": _round(distance, 3),
                    }
                )
                return
            boundary_evidence = {
                **(classification_evidence or {}),
                "derivation": "ROOFPRINT_CORROBORATED_FACET_BOUNDARY",
                "adjacentFacetCount": 1,
                "roofprintDistanceMeters": _round(distance, 3),
                "maximumRoofprintDistanceMeters": _round(
                    exterior_boundary_maximum_distance_meters, 3
                ),
            }

        downslope_alignment = _edge_downslope_alignment_degrees(use)
        if downslope_alignment is not None:
            boundary_evidence = {
                **(boundary_evidence or {}),
                "edgeDirectionToDownslopeDegrees": _round(
                    downslope_alignment, 3
                ),
                "classificationRule": "FACET_SLOPE_DIRECTION",
            }
        # A true rake travels substantially across the facet contours.  Small
        # vertical changes along a raster-derived eave are expected plane-fit
        # residuals and must not turn a hip-roof perimeter into rakes.
        if downslope_alignment is not None and downslope_alignment <= 45.0:
            add_edge("rakes", [use], boundary_evidence)
        else:
            add_edge("eaves", [use], boundary_evidence)

    for edge_key, uses in edge_uses.items():
        if len(uses) > 2:
            raise UnreliableGeometryError("NON_MANIFOLD_ROOF_EDGE", "More than two roof facets share a reconstructed edge.")
        first = uses[0]
        if _edge_length(first.start, first.end) <= 0.10:
            continue
        if len(uses) == 1:
            classify_exterior(first)
            continue

        second = uses[1]
        vertical_separations = _paired_edge_vertical_separations(first, second)
        minimum_vertical_separation = min(vertical_separations)
        minimum_level_transition_separation = max(
            edge_node_vertical_tolerance_meters,
            plane_intersection_maximum_displacement_meters,
        )
        if minimum_vertical_separation > minimum_level_transition_separation:
            lower = min(
                uses,
                key=lambda use: _edge_height_at_xy(
                    use.start,
                    use.end,
                    (
                        (use.start[0] + use.end[0]) / 2,
                        (use.start[1] + use.end[1]) / 2,
                        0.0,
                    ),
                ),
            )
            evidence = {
                "derivation": "PLAN_COINCIDENT_VERTICAL_LEVEL_TRANSITION",
                "adjacentFacetCount": 2,
                "facetIds": sorted(
                    [first.facet.facet_id, second.facet.facet_id]
                ),
                "verticalSeparationMeters": [
                    _round(value, 3) for value in vertical_separations
                ],
                "minimumVerticalSeparationMeters": _round(
                    minimum_vertical_separation, 3
                ),
                "verticalNodeToleranceMeters": _round(
                    edge_node_vertical_tolerance_meters, 3
                ),
                "minimumLevelTransitionSeparationMeters": _round(
                    minimum_level_transition_separation, 3
                ),
                "classificationRule": "LOWER_ROOF_WALL_INTERFACE",
            }
            add_edge("highPerimeters", [lower], evidence)
            vertical_level_transitions.append(
                {
                    **evidence,
                    "lengthFeet": _round(
                        _edge_length(lower.start, lower.end) * METERS_TO_FEET
                    ),
                }
            )
            continue
        direction_variance = _projected_edge_direction_variance_degrees(
            first, second
        )
        if direction_variance > shared_edge_maximum_direction_variance_degrees:
            evidence = {
                "derivation": "SUPPRESSED_CROSSING_NODE_ARTIFACT",
                "facetIds": [first.facet.facet_id, second.facet.facet_id],
                "directionVarianceDegrees": _round(direction_variance, 3),
                "directionVarianceReference": "PROJECTED_BOUNDARY",
                "maximumDirectionVarianceDegrees": _round(
                    shared_edge_maximum_direction_variance_degrees, 3
                ),
                "candidateLengthFeet": _round(
                    (
                        _edge_length(first.start, first.end)
                        + _edge_length(second.start, second.end)
                    )
                    / 2
                    * METERS_TO_FEET
                ),
            }
            rejected_noded_adjacencies.append(evidence)
            continue
        if _normal_angle_degrees(first.facet, second.facet) <= coplanar_tolerance_degrees:
            continue
        plane_intersection_direction = _cross(
            first.facet.normal, second.facet.normal
        )
        boundary_alignments = [
            _vector_alignment_degrees(
                _vector(use.start, use.end), plane_intersection_direction
            )
            for use in uses
        ]
        alignment_exceeded = any(
            alignment > shared_edge_maximum_direction_variance_degrees
            for alignment in boundary_alignments
        )
        plane_intersection_error: UnreliableGeometryError | None = None
        if alignment_exceeded:
            plane_intersection_error = UnreliableGeometryError(
                "ROOF_PLANE_INTERSECTION_MISALIGNED",
                "The reconstructed boundary direction is not aligned with the incident plane intersection.",
            )
        else:
            try:
                uses, intersection_evidence = _validated_plane_intersection_edge(
                    uses, plane_intersection_maximum_displacement_meters
                )
            except UnreliableGeometryError as error:
                plane_intersection_error = error
        if plane_intersection_error is not None:
            try:
                uses, intersection_evidence = _validated_planar_consensus_edge(
                    uses,
                    maximum_planar_displacement_meters=edge_node_tolerance_meters,
                    maximum_3d_correction_meters=(
                        plane_intersection_maximum_displacement_meters
                    ),
                )
            except UnreliableGeometryError as consensus_error:
                if not alignment_exceeded:
                    raise plane_intersection_error
                evidence = {
                    "derivation": "SUPPRESSED_PLANE_INTERSECTION_MISALIGNMENT",
                    "facetIds": [first.facet.facet_id, second.facet.facet_id],
                    "directionVarianceDegrees": _round(direction_variance, 3),
                    "originalBoundaryAlignmentDegrees": [
                        _round(alignment, 3) for alignment in boundary_alignments
                    ],
                    "maximumDirectionVarianceDegrees": _round(
                        shared_edge_maximum_direction_variance_degrees, 3
                    ),
                    "incidentPlaneAngleDegrees": _round(
                        _normal_angle_degrees(first.facet, second.facet), 3
                    ),
                    "planarConsensusRejectionCode": consensus_error.code,
                    "candidateLengthFeet": _round(
                        (
                            _edge_length(first.start, first.end)
                            + _edge_length(second.start, second.end)
                        )
                        / 2
                        * METERS_TO_FEET
                    ),
                }
                rejected_noded_adjacencies.append(evidence)
                continue
            intersection_evidence["planeIntersectionFallback"] = {
                "errorCode": plane_intersection_error.code,
                "originalBoundaryAlignmentDegrees": [
                    _round(alignment, 3) for alignment in boundary_alignments
                ],
            }
            planar_consensus_lengths.append(
                _edge_length(uses[0].start, uses[0].end)
            )
        first, second = uses
        intersection_evidence["adjacentFacetCount"] = 2
        if edge_key in repaired_edge_evidence:
            intersection_evidence["boundaryPairing"] = repaired_edge_evidence[
                edge_key
            ]
        first_delta = _facet_side_height_delta(first)
        second_delta = _facet_side_height_delta(second)
        first_decisive = abs(first_delta) > plane_side_tolerance_meters
        second_decisive = abs(second_delta) > plane_side_tolerance_meters
        # A roof plane can be tangent to a shared edge while the adjacent plane
        # has a decisive cross-edge derivative.  In that case the non-zero
        # derivative still determines the local dihedral sign: positive is a
        # concave/valley edge and negative is a convex ridge/hip edge.  Requiring
        # both derivatives to clear the tolerance incorrectly rejects valid
        # flat-to-slope and rake-aligned intersections.  Opposing decisive
        # derivatives remain ambiguous and fail closed below.
        decisive_deltas = [
            delta
            for delta, decisive in (
                (first_delta, first_decisive),
                (second_delta, second_decisive),
            )
            if decisive
        ]
        if decisive_deltas and all(delta > 0 for delta in decisive_deltas):
            intersection_evidence["junctionShape"] = "CONCAVE"
            add_edge("valleys", uses, intersection_evidence)
        elif decisive_deltas and all(delta < 0 for delta in decisive_deltas):
            intersection_evidence["junctionShape"] = "CONVEX"
            if abs(first.start[2] - first.end[2]) <= horizontal_edge_tolerance_meters:
                add_edge("ridges", uses, intersection_evidence)
            else:
                add_edge("hips", uses, intersection_evidence)
        else:
            ambiguous.append(
                {
                    "reason": "UNCLASSIFIED_SHARED_EDGE",
                    "facetIds": [first.facet.facet_id, second.facet.facet_id],
                    "sideHeightsMeters": [_round(first_delta, 3), _round(second_delta, 3)],
                    "planeIntersection": intersection_evidence,
                }
            )

    if ambiguous:
        raise UnreliableGeometryError(
            "ROOF_EDGE_CLASSIFICATION_INCOMPLETE",
            "At least one reconstructed roof edge could not be classified without guessing.",
            details={"ambiguousEdges": ambiguous},
        )

    roof_area_square_meters = sum(facet.area_square_meters for facet in facets)
    weighted_pitch = sum(facet.pitch_degrees * facet.area_square_meters for facet in facets) / roof_area_square_meters
    maximum_pitch = max(facet.pitch_degrees for facet in facets)
    flat_area_square_meters = sum(
        facet.area_square_meters for facet in facets if facet.pitch_degrees <= flat_pitch_degrees
    )

    result_facets = [
        {
            "facetId": facet.facet_id,
            "areaSqFt": _round(facet.area_square_meters * SQUARE_METERS_TO_SQUARE_FEET),
            "horizontalAreaSqFt": _round(
                facet.horizontal_area_square_meters * SQUARE_METERS_TO_SQUARE_FEET
            ),
            "slopeAreaFormulaSqFt": _round(
                (
                    facet.horizontal_area_square_meters
                    / math.cos(math.radians(facet.pitch_degrees))
                )
                * SQUARE_METERS_TO_SQUARE_FEET
            ),
            "pitchDegrees": _round(facet.pitch_degrees),
            "pitchRisePer12": _round(12 * math.tan(math.radians(facet.pitch_degrees))),
            "azimuthDegrees": _round(facet.azimuth_degrees),
            "classification": "FLAT" if facet.pitch_degrees <= flat_pitch_degrees else "SLOPED",
            "openingCount": facet.opening_count,
            "openingPerimeterFeet": _round(facet.opening_perimeter_meters * METERS_TO_FEET),
        }
        for facet in facets
    ]

    totals = {
        kind: _round(sum(edge["lengthFeet"] for edge in entries))
        for kind, entries in classified.items()
    }
    projected_totals = {
        kind: _round(sum(edge["projectedLengthFeet"] for edge in entries))
        for kind, entries in classified.items()
    }
    topology_vertices, topology_edges, topology_hash = _canonical_topology_graph(
        classified
    )
    result = {
        "roofAreaSqFt": _round(roof_area_square_meters * SQUARE_METERS_TO_SQUARE_FEET),
        "averagePitchDegrees": _round(weighted_pitch),
        "maximumPitchDegrees": _round(maximum_pitch),
        "rakesFeet": totals["rakes"],
        "eavesFeet": totals["eaves"],
        "valleysFeet": totals["valleys"],
        "ridgesFeet": totals["ridges"],
        "hipsFeet": totals["hips"],
        "totalHorizontalRoofAreaSqFt": _round(
            sum(facet.horizontal_area_square_meters for facet in facets)
            * SQUARE_METERS_TO_SQUARE_FEET
        ),
        "externalPerimeterFeet": _round(totals["eaves"] + totals["rakes"]),
        "externalProjectedPerimeterFeet": _round(
            projected_totals["eaves"]
            + projected_totals["rakes"]
        ),
        "internalRoofEdgeFeet": _round(
            totals["ridges"] + totals["hips"] + totals["valleys"]
        ),
        "highPerimeterFeet": totals["highPerimeters"],
        "topology": {
            "contractVersion": "1.1",
            "topologyHash": topology_hash,
            "vertexCount": len(topology_vertices),
            "edgeCount": len(topology_edges),
            "edgeNodeToleranceMeters": _round(edge_node_tolerance_meters, 3),
            "edgeNodeVerticalToleranceMeters": _round(
                edge_node_vertical_tolerance_meters, 3
            ),
            "nodingMode": "PLANAR_WITH_VERTICAL_TRANSITION_VALIDATION",
            "nodedEdgeCount": len(edge_uses),
            **boundary_repair_audit,
            "sharedEdgeCount": sum(1 for uses in edge_uses.values() if len(uses) == 2)
            - len(rejected_noded_adjacencies)
            - len(vertical_level_transitions),
            "verticalLevelTransitionCount": len(vertical_level_transitions),
            "verticalLevelTransitionFeet": _round(
                sum(item["lengthFeet"] for item in vertical_level_transitions)
            ),
            "verticalLevelTransitions": vertical_level_transitions,
            "planarConsensusSharedBoundaryCount": len(planar_consensus_lengths),
            "planarConsensusSharedBoundaryFeet": _round(
                sum(planar_consensus_lengths) * METERS_TO_FEET
            ),
            "exteriorEdgeCount": sum(1 for uses in edge_uses.values() if len(uses) == 1),
            "sharedEdgeMaximumDirectionVarianceDegrees": _round(
                shared_edge_maximum_direction_variance_degrees, 3
            ),
            "rejectedNodedAdjacencyCount": len(rejected_noded_adjacencies),
            "rejectedNodedAdjacencies": rejected_noded_adjacencies,
            "suppressedCrossingArtifactFeet": _round(
                sum(
                    item["candidateLengthFeet"]
                    for item in rejected_noded_adjacencies
                )
            ),
            "exteriorBoundaryMaximumDistanceMeters": _round(
                exterior_boundary_maximum_distance_meters, 3
            ),
            "unmatchedInteriorBoundaryCount": len(unmatched_interior_boundaries),
            "unmatchedInteriorBoundaryFeet": _round(
                sum(item["lengthFeet"] for item in unmatched_interior_boundaries)
            ),
            "unmatchedInteriorBoundaries": unmatched_interior_boundaries,
        },
        "flatRoofAreaSqFt": _round(flat_area_square_meters * SQUARE_METERS_TO_SQUARE_FEET),
        "roofOpeningCount": sum(facet.opening_count for facet in facets),
        "roofOpeningPerimeterFeet": _round(
            sum(facet.opening_perimeter_meters for facet in facets) * METERS_TO_FEET
        ),
        "confidence": _round(quality_confidence, 3),
        "facets": result_facets,
        "vertices": topology_vertices,
        "edges": topology_edges,
        "rakes": classified["rakes"],
        "eaves": classified["eaves"],
        "valleys": classified["valleys"],
        "ridges": classified["ridges"],
        "hips": classified["hips"],
        "highPerimeters": classified["highPerimeters"],
        "quality": {
            "pointDensityPerSquareMeter": _round(float(attributes.get("rf_pt_density", 0)), 3),
            "nodataFraction": _round(float(attributes.get("rf_nodata_frac", 0)), 4),
            "rmseMeters": _round(float(attributes.get("rf_rmse_lod22", 0)), 4),
            "components": {key: _round(value, 3) for key, value in quality_components.items()},
        },
    }
    if include_validation_facets:
        result["_validationFacets"] = [
            {
                "facetId": facet.facet_id,
                "verticesMeters": [list(vertex) for vertex in facet.vertices],
                "normal": list(facet.normal),
                "areaSqFt": _round(
                    facet.area_square_meters * SQUARE_METERS_TO_SQUARE_FEET
                ),
                "pitchDegrees": _round(facet.pitch_degrees, 3),
            }
            for facet in facets
        ]
    return result
