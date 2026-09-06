"""Deterministic Open3D roof-facet consolidation before edge classification.

Roofer remains the source of the initial LoD2.2 roof coverage.  This module
uses the normalized LiDAR returns to split a Roofer face when it contains
multiple independently supported planes and to merge adjacent Roofer
fragments that represent one physical plane.  It returns a CityJSONFeature so
the existing canonical topology and plane-intersection implementation remains
the only edge-classification path.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from shapely import concave_hull
from shapely import contains_xy
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, split, unary_union

from .errors import UnreliableGeometryError


@dataclass(frozen=True)
class ConsolidatedPlane:
    point_indexes: tuple[int, ...]
    normal: tuple[float, float, float]
    centroid: tuple[float, float, float]
    rmse_meters: float
    support_hull: BaseGeometry
    # A bounded, deterministic sample of the actual LiDAR returns supporting
    # this plane. DSM reconciliation must use interior measured returns, not
    # hull vertices that commonly land on mixed roof/wall/vegetation pixels.
    support_coordinates: tuple[tuple[float, float, float], ...] = ()
    dsm_residual_rmse_meters: float | None = None

    @property
    def pitch_degrees(self) -> float:
        return math.degrees(math.acos(max(-1.0, min(1.0, self.normal[2]))))

    @property
    def azimuth_degrees(self) -> float:
        return math.degrees(
            math.atan2(self.normal[0] / self.normal[2], self.normal[1] / self.normal[2])
        ) % 360


@dataclass(frozen=True)
class DsmResidualSample:
    plane_index: int
    point_index: int
    x: float
    y: float
    lidar_height: float
    dsm_minus_lidar: float


def _spatial_dsm_residual_components(
    samples: list[DsmResidualSample],
    centered_residuals: np.ndarray,
    *,
    residual_threshold_meters: float,
    maximum_gap_meters: float,
) -> list[list[int]]:
    """Return deterministic connected components of material DSM residuals.

    Residual sign is part of connectivity so a rooftop obstruction above a
    plane is never joined to a low/no-data edge artefact.  This helper uses
    measured sample coordinates and has no knowledge of private calibration
    targets.
    """

    eligible = [
        index
        for index, residual in enumerate(centered_residuals)
        if abs(float(residual)) > residual_threshold_meters
    ]
    if not eligible:
        return []
    union = _UnionFind(len(eligible))
    for left_offset, left_index in enumerate(eligible):
        left = samples[left_index]
        left_residual = float(centered_residuals[left_index])
        for right_offset in range(left_offset + 1, len(eligible)):
            right_index = eligible[right_offset]
            right = samples[right_index]
            right_residual = float(centered_residuals[right_index])
            if left_residual * right_residual <= 0:
                continue
            if math.hypot(left.x - right.x, left.y - right.y) > maximum_gap_meters:
                continue
            # Do not bridge two vertically distinct residual populations just
            # because their pixels touch in plan.
            if abs(left_residual - right_residual) > max(
                residual_threshold_meters, 0.50
            ):
                continue
            union.union(left_offset, right_offset)
    members: dict[int, list[int]] = {}
    for offset, sample_index in enumerate(eligible):
        members.setdefault(union.find(offset), []).append(sample_index)
    return sorted(
        (sorted(component) for component in members.values()),
        key=lambda component: (
            -len(component),
            round(samples[component[0]].x, 6),
            round(samples[component[0]].y, 6),
        ),
    )


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = np.arange(size, dtype=np.int32)
        self.rank = np.zeros(size, dtype=np.uint8)

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = int(self.parent[value])
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _open3d() -> Any:
    try:
        return importlib.import_module("open3d")
    except (ImportError, OSError) as error:
        raise UnreliableGeometryError(
            "OPEN3D_RUNTIME_MISSING",
            "The deterministic facet consolidator is unavailable.",
        ) from error


def _unit_normal(values: np.ndarray) -> np.ndarray:
    length = float(np.linalg.norm(values))
    if length <= 1e-12:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_PLANE_INVALID",
            "A roof-plane candidate has a degenerate normal.",
        )
    result = values / length
    return -result if result[2] < 0 else result


def _fit_plane(points: np.ndarray, indexes: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    selected = points[indexes]
    centroid = np.mean(selected, axis=0)
    _, _, vectors = np.linalg.svd(selected - centroid, full_matrices=False)
    normal = _unit_normal(vectors[-1])
    residuals = np.abs((selected - centroid) @ normal)
    rmse = float(np.sqrt(np.mean(np.square(residuals))))
    return normal, centroid, rmse


def _support_hull(points: np.ndarray, indexes: np.ndarray) -> BaseGeometry:
    cloud = MultiPoint(points[indexes, :2].tolist())
    hull = concave_hull(cloud, ratio=0.25, allow_holes=False)
    if hull.geom_type != "Polygon" or not hull.is_valid or hull.area <= 0.05:
        hull = cloud.convex_hull
    return hull.simplify(0.02, preserve_topology=True)


def _support_coordinate_sample(
    points: np.ndarray, indexes: np.ndarray, *, maximum: int = 96
) -> tuple[tuple[float, float, float], ...]:
    """Return an order-independent, spatially distributed support sample."""

    selected = np.asarray(points[indexes], dtype=float)
    if not len(selected):
        return ()
    order = np.lexsort((selected[:, 2], selected[:, 1], selected[:, 0]))
    selected = selected[order]
    if len(selected) > maximum:
        offsets = np.linspace(0, len(selected) - 1, maximum, dtype=np.int32)
        selected = selected[offsets]
    return tuple(tuple(float(value) for value in row) for row in selected)


def _normal_angle_degrees(left: np.ndarray, right: np.ndarray) -> float:
    return math.degrees(math.acos(float(np.clip(np.dot(left, right), -1.0, 1.0))))


def _plane_height(plane: ConsolidatedPlane, x: float, y: float) -> float:
    nx, ny, nz = plane.normal
    cx, cy, cz = plane.centroid
    if nz <= 1e-8:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_PLANE_VERTICAL",
            "A consolidated roof facet is vertical.",
        )
    return cz - (nx * (x - cx) + ny * (y - cy)) / nz


def discover_consolidated_planes(
    points: np.ndarray,
    roofprint: BaseGeometry,
    settings: Any,
) -> tuple[list[ConsolidatedPlane], dict[str, Any]]:
    """Discover connected planes and agglomerate only globally coplanar fragments."""

    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_POINTS_INVALID",
            "Facet consolidation received invalid roof-return coordinates.",
        )
    crop_buffer = float(getattr(settings, "facet_consolidation_crop_buffer_meters", 0.20))
    cropped_mask = contains_xy(roofprint.buffer(crop_buffer), points[:, 0], points[:, 1])
    cropped = points[cropped_mask]
    minimum_points = int(getattr(settings, "facet_consolidation_minimum_points", 20))
    if len(cropped) < minimum_points:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SUPPORT_INSUFFICIENT",
            "Too few normalized roof returns remain inside the reconstructed roofprint.",
            details={"supportPoints": int(len(cropped)), "minimumSupportPoints": minimum_points},
        )

    # Projected coordinates in Southwest Florida are large.  Open3D receives a
    # translated cloud to avoid numeric conditioning differences; fitted plane
    # centroids below remain in the original CRS.
    local_origin = np.mean(cropped, axis=0)
    local = cropped - local_origin
    o3d = _open3d()
    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector(local)
    normal_radius = float(getattr(settings, "facet_consolidation_normal_radius_meters", 0.45))
    cloud.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=normal_radius, max_nn=48)
    )
    normals = np.asarray(cloud.normals, dtype=float)
    normals[normals[:, 2] < 0] *= -1
    lengths = np.linalg.norm(normals, axis=1)
    valid_normals = lengths > 1e-9
    normals[valid_normals] /= lengths[valid_normals, None]

    tree = o3d.geometry.KDTreeFlann(cloud)
    union = _UnionFind(len(cropped))
    neighbor_radius = float(
        getattr(settings, "facet_consolidation_neighbor_radius_meters", 0.40)
    )
    maximum_normal_angle = float(
        getattr(settings, "facet_consolidation_maximum_normal_angle_degrees", 4.0)
    )
    maximum_residual = float(
        getattr(settings, "facet_consolidation_maximum_local_residual_meters", 0.06)
    )
    cosine = math.cos(math.radians(maximum_normal_angle))
    accepted_neighbor_pairs = 0
    # A union is permitted only for spatial neighbours whose independently
    # estimated normals and reciprocal point-to-plane residuals agree.
    for index, point in enumerate(local):
        if not valid_normals[index]:
            continue
        _, neighbors, _ = tree.search_radius_vector_3d(point, neighbor_radius)
        for other in sorted(int(value) for value in neighbors if int(value) > index):
            if not valid_normals[other]:
                continue
            if float(np.dot(normals[index], normals[other])) < cosine:
                continue
            delta = local[other] - point
            if abs(float(np.dot(normals[index], delta))) > maximum_residual:
                continue
            if abs(float(np.dot(normals[other], delta))) > maximum_residual:
                continue
            union.union(index, other)
            accepted_neighbor_pairs += 1

    members: dict[int, list[int]] = {}
    for index in range(len(cropped)):
        members.setdefault(union.find(index), []).append(index)

    maximum_fit_rmse = float(
        getattr(settings, "facet_consolidation_maximum_plane_rmse_meters", 0.15)
    )
    groups: list[ConsolidatedPlane] = []
    rejected_nonplanar = 0
    split_nonplanar = 0

    def split_component(indexes: np.ndarray, depth: int) -> list[np.ndarray]:
        """Break a locally connected but globally non-planar chain deterministically."""

        if depth >= 2:
            return []
        allowed = {int(value): offset for offset, value in enumerate(indexes)}
        sub_union = _UnionFind(len(indexes))
        angular_limit = maximum_normal_angle * (0.5 ** (depth + 1))
        residual_limit = maximum_residual * (0.5 ** (depth + 1))
        angular_cosine = math.cos(math.radians(angular_limit))
        for offset, index_value in enumerate(indexes):
            index = int(index_value)
            if not valid_normals[index]:
                continue
            _, neighbors, _ = tree.search_radius_vector_3d(
                local[index], neighbor_radius
            )
            for other in sorted(int(value) for value in neighbors):
                other_offset = allowed.get(other)
                if other_offset is None or other_offset <= offset:
                    continue
                if not valid_normals[other]:
                    continue
                if float(np.dot(normals[index], normals[other])) < angular_cosine:
                    continue
                delta = local[other] - local[index]
                if abs(float(np.dot(normals[index], delta))) > residual_limit:
                    continue
                if abs(float(np.dot(normals[other], delta))) > residual_limit:
                    continue
                sub_union.union(offset, other_offset)
        split_members: dict[int, list[int]] = {}
        for offset, value in enumerate(indexes):
            split_members.setdefault(sub_union.find(offset), []).append(int(value))
        return [
            np.asarray(sorted(values), dtype=np.int32)
            for values in split_members.values()
            if len(values) >= minimum_points
        ]

    for indexes_list in members.values():
        if len(indexes_list) < minimum_points:
            continue
        pending = [(np.asarray(sorted(indexes_list), dtype=np.int32), 0)]
        while pending:
            indexes, depth = pending.pop(0)
            normal, centroid, rmse = _fit_plane(cropped, indexes)
            if rmse > maximum_fit_rmse:
                children = split_component(indexes, depth)
                if len(children) > 1:
                    pending.extend((child, depth + 1) for child in children)
                    split_nonplanar += 1
                else:
                    rejected_nonplanar += 1
                continue
            hull = _support_hull(cropped, indexes)
            if hull.geom_type != "Polygon" or hull.area <= 0.05:
                continue
            groups.append(
                ConsolidatedPlane(
                    point_indexes=tuple(int(value) for value in indexes),
                    normal=tuple(float(value) for value in normal),
                    centroid=tuple(float(value) for value in centroid),
                    rmse_meters=rmse,
                    support_hull=hull,
                    support_coordinates=_support_coordinate_sample(cropped, indexes),
                )
            )

    merge_angle = float(
        getattr(settings, "facet_consolidation_merge_angle_degrees", 4.0)
    )
    merge_distance = float(
        getattr(settings, "facet_consolidation_merge_plane_distance_meters", 0.18)
    )
    merge_gap = float(getattr(settings, "facet_consolidation_merge_gap_meters", 2.0))
    merge_count = 0
    # Refit the complete proposed group on every merge.  This prevents a
    # transitive A~B~C chain from merging A with C when the final plane is not
    # globally supported.
    while True:
        candidates: list[tuple[tuple[float, ...], int, int, ConsolidatedPlane]] = []
        for left in range(len(groups)):
            for right in range(left + 1, len(groups)):
                first, second = groups[left], groups[right]
                first_normal = np.asarray(first.normal)
                second_normal = np.asarray(second.normal)
                angle = _normal_angle_degrees(first_normal, second_normal)
                if angle > merge_angle:
                    continue
                gap = float(first.support_hull.distance(second.support_hull))
                if gap > merge_gap:
                    continue
                delta = np.asarray(second.centroid) - np.asarray(first.centroid)
                separation = max(
                    abs(float(np.dot(first_normal, delta))),
                    abs(float(np.dot(second_normal, delta))),
                )
                if separation > merge_distance:
                    continue
                combined_indexes = np.asarray(
                    sorted(set(first.point_indexes) | set(second.point_indexes)),
                    dtype=np.int32,
                )
                normal, centroid, rmse = _fit_plane(cropped, combined_indexes)
                if rmse > min(maximum_fit_rmse, merge_distance):
                    continue
                hull = _support_hull(cropped, combined_indexes)
                combined = ConsolidatedPlane(
                    point_indexes=tuple(int(value) for value in combined_indexes),
                    normal=tuple(float(value) for value in normal),
                    centroid=tuple(float(value) for value in centroid),
                    rmse_meters=rmse,
                    support_hull=hull,
                    support_coordinates=_support_coordinate_sample(
                        cropped, combined_indexes
                    ),
                )
                key = (
                    round(angle, 9),
                    round(separation, 9),
                    round(gap, 9),
                    *tuple(round(value, 6) for value in combined.centroid),
                )
                candidates.append((key, left, right, combined))
        if not candidates:
            break
        _, left, right, combined = min(candidates, key=lambda item: item[0])
        groups = [
            group for index, group in enumerate(groups) if index not in {left, right}
        ] + [combined]
        merge_count += 1

    groups.sort(
        key=lambda plane: (
            tuple(round(value, 6) for value in plane.centroid),
            tuple(round(value, 6) for value in plane.normal),
            -len(plane.point_indexes),
        )
    )
    # Absorb points left in small connected fragments only when an existing
    # consolidated plane independently explains their normal, elevation, and
    # spatial position.  These returns strengthen support coverage without
    # creating extra facets from roof-edge noise or lowering the coverage gate.
    assigned_indexes = {
        point_index for group in groups for point_index in group.point_indexes
    }
    absorb_by_group: dict[int, list[int]] = {}
    absorb_residual = max(maximum_residual * 2.0, 0.10)
    assignment_gap = float(
        getattr(settings, "facet_consolidation_assignment_gap_meters", 1.0)
    )
    absorb_without_normal_by_group: dict[int, set[int]] = {}
    absorbed_without_normal_support = 0
    for point_index in range(len(cropped)):
        if point_index in assigned_indexes:
            continue
        point = cropped[point_index]
        point_xy = Point(float(point[0]), float(point[1]))
        rankings = []
        for group_index, group in enumerate(groups):
            normal = np.asarray(group.normal)
            angle = (
                _normal_angle_degrees(normals[point_index], normal)
                if valid_normals[point_index]
                else None
            )
            residual = abs(float((point - np.asarray(group.centroid)) @ normal))
            if residual > absorb_residual:
                continue
            hull_distance = float(group.support_hull.distance(point_xy))
            if hull_distance > merge_gap:
                continue
            normal_supported = (
                angle is not None and angle <= maximum_normal_angle * 2.0
            )
            # At roof edges and in sparse areas an Open3D local normal often
            # blends two planes. A return may still strengthen a plane when
            # its elevation residual is tight and it is adjacent to existing
            # support. This preserves the coverage gate while recovering
            # independently corroborated measured returns.
            if not normal_supported and (
                residual > max(maximum_residual * 1.5, 0.08)
                or hull_distance > assignment_gap
            ):
                continue
            rankings.append(
                (
                    round(residual, 9),
                    0 if normal_supported else 1,
                    round(angle if angle is not None else 180.0, 9),
                    round(hull_distance, 9),
                    group_index,
                )
            )
        if rankings:
            rankings.sort()
            winner = rankings[0]
            # Without a trustworthy local normal, reject an assignment that
            # is geometrically ambiguous between two nearly equal planes.
            if winner[1] == 1 and len(rankings) > 1:
                runner_up = rankings[1]
                if (
                    runner_up[0] - winner[0] < 0.02
                    and runner_up[3] - winner[3] < 0.10
                ):
                    continue
            absorb_by_group.setdefault(winner[-1], []).append(point_index)
            if winner[1] == 1:
                absorb_without_normal_by_group.setdefault(winner[-1], set()).add(
                    point_index
                )
    absorbed_point_count = 0
    strengthened_groups: list[ConsolidatedPlane] = []
    for group_index, group in enumerate(groups):
        additions = absorb_by_group.get(group_index, [])
        combined_indexes = np.asarray(
            sorted(set(group.point_indexes) | set(additions)), dtype=np.int32
        )
        normal, centroid, rmse = _fit_plane(cropped, combined_indexes)
        if additions and rmse <= maximum_fit_rmse:
            absorbed_point_count += len(additions)
            absorbed_without_normal_support += len(
                absorb_without_normal_by_group.get(group_index, set())
            )
            strengthened_groups.append(
                ConsolidatedPlane(
                    point_indexes=tuple(int(value) for value in combined_indexes),
                    normal=tuple(float(value) for value in normal),
                    centroid=tuple(float(value) for value in centroid),
                    rmse_meters=rmse,
                    support_hull=_support_hull(cropped, combined_indexes),
                    support_coordinates=_support_coordinate_sample(
                        cropped, combined_indexes
                    ),
                )
            )
        else:
            strengthened_groups.append(group)
    groups = strengthened_groups

    groups.sort(
        key=lambda plane: (
            tuple(round(value, 6) for value in plane.centroid),
            tuple(round(value, 6) for value in plane.normal),
            -len(plane.point_indexes),
        )
    )
    supported_points = sum(len(plane.point_indexes) for plane in groups)
    minimum_support_fraction = float(
        getattr(settings, "facet_consolidation_minimum_support_fraction", 0.75)
    )
    support_fraction = supported_points / len(cropped)
    if support_fraction < minimum_support_fraction:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_COVERAGE_INSUFFICIENT",
            "Independent plane clusters do not cover enough of the normalized roof returns.",
            details={
                "supportFraction": round(support_fraction, 4),
                "minimumSupportFraction": minimum_support_fraction,
                "candidateFacetCount": len(groups),
            },
        )
    if not groups:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_EMPTY",
            "No independently supported roof facets were recovered.",
        )
    return groups, {
        "algorithm": "OPEN3D_CONNECTED_PLANE_AGGLOMERATION",
        "inputPointCount": int(len(points)),
        "croppedPointCount": int(len(cropped)),
        "initialConnectedComponentCount": sum(
            len(indexes) >= minimum_points for indexes in members.values()
        ),
        "consolidatedPlaneCount": len(groups),
        "mergeCount": merge_count,
        "absorbedSmallFragmentPointCount": absorbed_point_count,
        "absorbedPlaneSupportedPointCount": absorbed_without_normal_support,
        "splitNonplanarComponentCount": split_nonplanar,
        "rejectedNonplanarComponentCount": rejected_nonplanar,
        "supportFraction": round(support_fraction, 4),
        "acceptedNeighborPairCount": accepted_neighbor_pairs,
        "splitDisconnectedSections": True,
        "parameters": {
            "cropBufferMeters": crop_buffer,
            "normalRadiusMeters": normal_radius,
            "neighborRadiusMeters": neighbor_radius,
            "maximumNormalAngleDegrees": maximum_normal_angle,
            "maximumLocalResidualMeters": maximum_residual,
            "minimumPoints": minimum_points,
            "maximumPlaneRmseMeters": maximum_fit_rmse,
            "mergeAngleDegrees": merge_angle,
            "mergePlaneDistanceMeters": merge_distance,
            "mergeGapMeters": merge_gap,
        },
    }


def _clip_to_plane_side(
    region: BaseGeometry,
    first: ConsolidatedPlane,
    second: ConsolidatedPlane,
) -> BaseGeometry:
    """Clip a region to the plane-intersection side supported by ``first``."""

    n1 = np.asarray(first.normal)
    n2 = np.asarray(second.normal)
    c1 = np.asarray(first.centroid)
    c2 = np.asarray(second.centroid)
    # z = ax + by + c for each fitted plane.
    a1, b1 = -n1[0] / n1[2], -n1[1] / n1[2]
    a2, b2 = -n2[0] / n2[2], -n2[1] / n2[2]
    z1 = c1[2] - a1 * c1[0] - b1 * c1[1]
    z2 = c2[2] - a2 * c2[0] - b2 * c2[1]
    a, b, c = a1 - a2, b1 - b2, z1 - z2
    denominator = a * a + b * b
    support_centroid = first.support_hull.centroid
    first_side = a * support_centroid.x + b * support_centroid.y + c
    if denominator <= 1e-12:
        # Parallel planes cannot define a new shared roof edge.  They should
        # already have merged if coplanar; otherwise their level separation is
        # retained by Roofer's existing subdivision rather than guessed here.
        return region if abs(first_side) <= 0.02 else Polygon()
    if abs(first_side) <= 0.02:
        support_values = [
            a * float(x) + b * float(y) + c
            for x, y in getattr(first.support_hull.exterior, "coords", [])
        ]
        if support_values:
            first_side = max(support_values, key=abs)
    if abs(first_side) <= 0.02:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SIDE_AMBIGUOUS",
            "A plane candidate's own support lies on a proposed split boundary.",
        )
    # Anchor the infinite intersection line at the point nearest this small
    # roof region, not at the point nearest global coordinate (0, 0).  In UTM
    # that global anchor can be hundreds of kilometres away along the same
    # line, so a finite segment centred there never reaches the property.
    region_center = region.representative_point()
    signed_offset = (
        a * region_center.x + b * region_center.y + c
    ) / denominator
    origin = np.asarray(
        (
            region_center.x - a * signed_offset,
            region_center.y - b * signed_offset,
        )
    )
    direction = np.asarray((-b, a))
    direction /= max(float(np.linalg.norm(direction)), 1e-12)
    bounds = region.bounds
    extent = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1.0) * 8 + 100
    line = LineString([origin - direction * extent, origin + direction * extent])
    try:
        pieces = split(region, line)
    except (ValueError, TypeError) as error:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SPLIT_INVALID",
            "A supported plane intersection could not split the roof coverage.",
        ) from error
    selected = []
    for piece in pieces.geoms:
        probe = piece.representative_point()
        side = a * probe.x + b * probe.y + c
        if side * first_side >= -1e-8:
            selected.append(piece)
    return unary_union(selected) if selected else Polygon()


def _partition_roofer_facet(
    polygon: Polygon,
    candidates: list[ConsolidatedPlane],
    adjacency_gap_meters: float = 1.25,
    minimum_interior_clearance_meters: float = 0.20,
    plane_evidence: dict[int, dict[str, float | None]] | None = None,
    assignment_audit: list[dict[str, Any]] | None = None,
    lidar_points: np.ndarray | None = None,
) -> list[tuple[BaseGeometry, ConsolidatedPlane]]:
    if len(candidates) == 1:
        return [(polygon, candidates[0])]
    # Build an arrangement only from spatially adjacent fitted planes whose
    # true 3-D intersection crosses this Roofer face.  Classifying the
    # resulting cells is more robust than repeatedly clipping a face against
    # every candidate: a non-adjacent third plane can no longer erase two
    # otherwise valid neighbouring facets.
    relations: list[dict[str, Any]] = []
    lines: list[LineString] = []
    bounds = polygon.bounds
    extent = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1.0) * 8 + 100
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            first, second = candidates[left], candidates[right]
            n1, n2 = np.asarray(first.normal), np.asarray(second.normal)
            c1, c2 = np.asarray(first.centroid), np.asarray(second.centroid)
            a1, b1 = -n1[0] / n1[2], -n1[1] / n1[2]
            a2, b2 = -n2[0] / n2[2], -n2[1] / n2[2]
            z1 = c1[2] - a1 * c1[0] - b1 * c1[1]
            z2 = c2[2] - a2 * c2[0] - b2 * c2[1]
            a, b, c = a1 - a2, b1 - b2, z1 - z2
            denominator = a * a + b * b
            if denominator <= 1e-12:
                continue
            polygon_center = polygon.representative_point()
            signed_offset = (
                a * polygon_center.x + b * polygon_center.y + c
            ) / denominator
            origin = np.asarray(
                (
                    polygon_center.x - a * signed_offset,
                    polygon_center.y - b * signed_offset,
                )
            )
            direction = np.asarray((-b, a))
            direction /= max(float(np.linalg.norm(direction)), 1e-12)
            line = LineString([origin - direction * extent, origin + direction * extent])
            clipped_line = line.intersection(polygon)
            if clipped_line.is_empty:
                continue
            if (
                clipped_line.centroid.distance(polygon.boundary)
                <= minimum_interior_clearance_meters
            ):
                continue
            if (
                line.distance(first.support_hull) > adjacency_gap_meters
                or line.distance(second.support_hull) > adjacency_gap_meters
                or first.support_hull.distance(second.support_hull)
                > adjacency_gap_meters
            ):
                continue
            first_probe = first.support_hull.representative_point()
            second_probe = second.support_hull.representative_point()
            first_side = a * first_probe.x + b * first_probe.y + c
            second_side = a * second_probe.x + b * second_probe.y + c
            if abs(first_side) <= 0.02 or abs(second_side) <= 0.02:
                continue
            if first_side * second_side >= 0:
                continue
            unit_a, unit_b = a / math.sqrt(denominator), b / math.sqrt(denominator)
            first_distance = first_side / math.sqrt(denominator)
            second_distance = second_side / math.sqrt(denominator)
            first_on_line = (
                first_probe.x - unit_a * first_distance,
                first_probe.y - unit_b * first_distance,
            )
            second_on_line = (
                second_probe.x - unit_a * second_distance,
                second_probe.y - unit_b * second_distance,
            )
            first_height_delta = _plane_height(
                first, first_probe.x, first_probe.y
            ) - _plane_height(first, *first_on_line)
            second_height_delta = _plane_height(
                second, second_probe.x, second_probe.y
            ) - _plane_height(second, *second_on_line)
            if first_height_delta * second_height_delta <= 0:
                continue
            relations.append(
                {
                    "left": left,
                    "right": right,
                    "a": a,
                    "b": b,
                    "c": c,
                    "leftSide": first_side,
                    "rightSide": second_side,
                    "junctionShape": (
                        "CONCAVE" if first_height_delta > 0 else "CONVEX"
                    ),
                }
            )
            normalized_length = math.sqrt(denominator)
            normalized = np.asarray((a, b, c), dtype=float) / normalized_length
            if normalized[0] < 0 or (
                abs(normalized[0]) <= 1e-12 and normalized[1] < 0
            ):
                normalized = -normalized
            duplicate = False
            for existing in relations[:-1]:
                existing_line = existing["normalizedLine"]
                direction_agreement = abs(
                    float(np.dot(normalized[:2], existing_line[:2]))
                )
                line_separation = abs(
                    float(
                        (normalized[0] - existing_line[0]) * polygon_center.x
                        + (normalized[1] - existing_line[1]) * polygon_center.y
                        + normalized[2]
                        - existing_line[2]
                    )
                )
                if direction_agreement >= math.cos(math.radians(2.0)) and line_separation <= 0.10:
                    duplicate = True
                    break
            relations[-1]["normalizedLine"] = normalized
            if not duplicate:
                lines.append(line)

    if not relations:
        strongest = max(
            enumerate(candidates),
            key=lambda item: (
                float(item[1].support_hull.intersection(polygon).area),
                len(item[1].point_indexes),
                -item[1].rmse_meters,
                -item[0],
            ),
        )[1]
        return [(polygon, strongest)]

    # Node every supported plane intersection and the roofprint boundary in a
    # single GEOS operation, then polygonize once.  Sequentially splitting a
    # cell makes the answer depend on line order and can leave incompatible
    # T-junctions between neighbouring faces.
    try:
        arrangement_edges = unary_union(
            [polygon.boundary]
            + [line.intersection(polygon) for line in lines]
        )
        cells = [
            cell
            for cell in polygonize(arrangement_edges)
            if not cell.is_empty
            and cell.area > 1e-8
            and polygon.covers(cell.representative_point())
        ]
    except (ValueError, TypeError) as error:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SPLIT_INVALID",
            "Supported plane intersections could not form a global arrangement.",
        ) from error

    cell_assignments: list[tuple[BaseGeometry, int]] = []
    cell_label_costs: list[dict[int, float]] = []
    for cell in cells:
        probe = cell.representative_point()
        cell_points = np.empty((0, 3), dtype=float)
        if lidar_points is not None and len(lidar_points):
            local_mask = contains_xy(
                cell.buffer(0.02), lidar_points[:, 0], lidar_points[:, 1]
            )
            cell_points = lidar_points[local_mask]
        rankings = []
        ranking_evidence: dict[int, dict[str, float | int | bool]] = {}
        for index, candidate in enumerate(candidates):
            mismatches = 0
            for relation in relations:
                if index not in {relation["left"], relation["right"]}:
                    continue
                expected_side = (
                    relation["leftSide"]
                    if index == relation["left"]
                    else relation["rightSide"]
                )
                actual_side = (
                    relation["a"] * probe.x
                    + relation["b"] * probe.y
                    + relation["c"]
                )
                if actual_side * expected_side < -1e-8:
                    mismatches += 1
            evidence = (plane_evidence or {}).get(id(candidate), {})
            spatial_distance = float(candidate.support_hull.distance(probe))
            support_overlap = float(candidate.support_hull.intersection(cell).area)
            overlap_ratio = support_overlap / max(float(cell.area), 0.01)
            local_lidar_rmse = float(candidate.rmse_meters)
            local_lidar_median = float(candidate.rmse_meters)
            local_lidar_support = 0
            local_lidar_inlier_ratio = 0.0
            if len(cell_points):
                predicted_heights = np.asarray(
                    [
                        _plane_height(candidate, float(point[0]), float(point[1]))
                        for point in cell_points
                    ],
                    dtype=float,
                )
                residuals = np.abs(cell_points[:, 2] - predicted_heights)
                # This is an assignment gate, not a plane-fit gate.  The later
                # Open3D validator still performs independent RANSAC.  Here we
                # only keep a cell from being labelled with a plane that has no
                # local LiDAR support.
                local_inliers = residuals <= 0.25
                local_lidar_support = int(np.count_nonzero(local_inliers))
                local_lidar_inlier_ratio = local_lidar_support / len(cell_points)
                if local_lidar_support:
                    supported_residuals = residuals[local_inliers]
                    local_lidar_rmse = float(
                        np.sqrt(np.mean(np.square(supported_residuals)))
                    )
                    local_lidar_median = float(np.median(supported_residuals))
                else:
                    local_lidar_rmse = math.inf
                    local_lidar_median = math.inf
            minimum_local_support = min(10, max(3, math.ceil(len(cell_points) * 0.20)))
            locally_unsupported = bool(
                len(cell_points) >= 3
                and local_lidar_support < minimum_local_support
            )
            dsm_rmse = float(evidence.get("dsmRmseMeters") or 0.0)
            solar_pitch_variance = float(
                evidence.get("solarPitchVarianceDegrees") or 0.0
            )
            solar_azimuth_variance = float(
                evidence.get("solarAzimuthVarianceDegrees") or 0.0
            )
            evidence_score = (
                spatial_distance
                + local_lidar_median
                + local_lidar_rmse
                + dsm_rmse
                + solar_pitch_variance / 10.0
                + solar_azimuth_variance / 180.0
                - min(1.0, overlap_ratio)
            )
            rankings.append(
                (
                    int(locally_unsupported),
                    mismatches,
                    round(evidence_score, 9),
                    round(spatial_distance, 9),
                    -round(support_overlap, 9),
                    -len(candidate.point_indexes),
                    index,
                )
            )
            ranking_evidence[index] = {
                "cellLidarPointCount": int(len(cell_points)),
                "cellLidarSupportPoints": local_lidar_support,
                "cellLidarInlierRatio": round(local_lidar_inlier_ratio, 6),
                "cellLidarResidualMedianMeters": (
                    None if not math.isfinite(local_lidar_median) else round(local_lidar_median, 6)
                ),
                "cellLidarResidualRmseMeters": (
                    None if not math.isfinite(local_lidar_rmse) else round(local_lidar_rmse, 6)
                ),
                "cellLidarSupportRequired": minimum_local_support,
                "cellLidarSupported": not locally_unsupported,
            }
        winner = min(rankings)[-1]
        cell_assignments.append((cell, winner))
        cell_label_costs.append(
            {
                int(ranking[-1]): (
                    float(ranking[0]) * 100000.0
                    + float(ranking[1]) * 1000.0
                    + min(100.0, float(ranking[2]))
                    + max(0.0, float(ranking[3]))
                )
                for ranking in rankings
            }
        )
        if assignment_audit is not None:
            winner_plane = candidates[winner]
            winner_evidence = (plane_evidence or {}).get(id(winner_plane), {})
            winner_lidar_evidence = ranking_evidence[winner]
            assignment_audit.append(
                {
                    "cellAreaSquareMeters": round(float(cell.area), 6),
                    "winnerPlaneOrdinal": winner + 1,
                    "relationMismatchCount": int(min(rankings)[1]),
                    "evidenceScore": round(float(min(rankings)[2]), 6),
                    "spatialSupportDistanceMeters": round(
                        float(winner_plane.support_hull.distance(probe)), 6
                    ),
                    "spatialSupportOverlapSquareMeters": round(
                        float(winner_plane.support_hull.intersection(cell).area), 6
                    ),
                    "lidarPlaneRmseMeters": round(
                        float(winner_plane.rmse_meters), 6
                    ),
                    "dsmResidualRmseMeters": winner_evidence.get(
                        "dsmRmseMeters"
                    ),
                    "solarPitchVarianceDegrees": winner_evidence.get(
                        "solarPitchVarianceDegrees"
                    ),
                    "solarAzimuthVarianceDegrees": winner_evidence.get(
                        "solarAzimuthVarianceDegrees"
                    ),
                    **winner_lidar_evidence,
                }
            )

    # A cell-wise minimum can put two different planes on opposite sides of a
    # line created by an unrelated plane pair.  Such a seam is not the 3-D
    # intersection of its incident planes and cannot be classified safely as
    # ridge, hip, or valley.  Optimize labels across the complete arrangement
    # with a hard geometric compatibility penalty, then reject any unresolved
    # seam before CityJSON edge classification.
    adjacency: list[tuple[int, int, BaseGeometry, float]] = []
    for left in range(len(cell_assignments)):
        left_cell = cell_assignments[left][0]
        for right in range(left + 1, len(cell_assignments)):
            right_cell = cell_assignments[right][0]
            shared = left_cell.boundary.intersection(right_cell.boundary)
            shared_length = float(shared.length)
            if shared_length > 1e-6:
                adjacency.append((left, right, shared, shared_length))

    def labels_compatible(
        left_label: int, right_label: int, shared: BaseGeometry
    ) -> bool:
        if left_label == right_label:
            return True
        left_plane = candidates[left_label]
        right_plane = candidates[right_label]
        intersection_direction = np.cross(
            np.asarray(left_plane.normal), np.asarray(right_plane.normal)
        )
        horizontal_length = math.hypot(
            float(intersection_direction[0]), float(intersection_direction[1])
        )
        if horizontal_length <= 1e-8:
            return False
        shared_lines = _line_parts(shared)
        if not shared_lines:
            return False
        longest = max(shared_lines, key=lambda line: float(line.length))
        start, end = longest.coords[0], longest.coords[-1]
        edge_x, edge_y = float(end[0] - start[0]), float(end[1] - start[1])
        edge_length = math.hypot(edge_x, edge_y)
        if edge_length <= 1e-8:
            return False
        alignment = math.degrees(
            math.acos(
                min(
                    1.0,
                    abs(
                        (
                            edge_x * float(intersection_direction[0])
                            + edge_y * float(intersection_direction[1])
                        )
                        / (edge_length * horizontal_length)
                    ),
                )
            )
        )
        midpoint = longest.interpolate(0.5, normalized=True)
        height_delta = abs(
            _plane_height(left_plane, midpoint.x, midpoint.y)
            - _plane_height(right_plane, midpoint.x, midpoint.y)
        )
        return alignment <= 5.0 and height_delta <= 0.10

    initial_labels = [label for _, label in cell_assignments]
    labels = list(initial_labels)
    incident: dict[int, list[tuple[int, BaseGeometry, float]]] = {
        index: [] for index in range(len(cell_assignments))
    }
    for left, right, shared, shared_length in adjacency:
        incident[left].append((right, shared, shared_length))
        incident[right].append((left, shared, shared_length))
    order = sorted(
        range(len(cell_assignments)),
        key=lambda index: (
            -round(float(cell_assignments[index][0].area), 9),
            round(float(cell_assignments[index][0].centroid.x), 9),
            round(float(cell_assignments[index][0].centroid.y), 9),
            index,
        ),
    )
    for _ in range(30):
        changed = False
        for cell_index in order:
            scored_labels = []
            for label, unary_cost in cell_label_costs[cell_index].items():
                conflict_cost = 0.0
                for neighbour, shared, shared_length in incident[cell_index]:
                    if not labels_compatible(label, labels[neighbour], shared):
                        conflict_cost += 1000000.0 + shared_length * 10000.0
                scored_labels.append((unary_cost + conflict_cost, label))
            winner = min(scored_labels)[1]
            if winner != labels[cell_index]:
                labels[cell_index] = winner
                changed = True
        if not changed:
            break
    unresolved = []
    for left, right, shared, shared_length in adjacency:
        if not labels_compatible(labels[left], labels[right], shared):
            unresolved.append(
                {
                    "leftCell": left,
                    "rightCell": right,
                    "lengthMeters": round(shared_length, 6),
                }
            )
    if unresolved:
        raise UnreliableGeometryError(
            "FACET_GLOBAL_LABEL_INCONSISTENT",
            "The global planar arrangement has an edge unsupported by its incident planes.",
            details={
                "unresolvedEdgeCount": len(unresolved),
                "unresolvedEdgeLengthMeters": round(
                    sum(item["lengthMeters"] for item in unresolved), 6
                ),
            },
        )
    cell_assignments = [
        (cell, labels[index]) for index, (cell, _) in enumerate(cell_assignments)
    ]
    if assignment_audit is not None:
        for index, (record, (_, final_label)) in enumerate(
            zip(assignment_audit, cell_assignments)
        ):
            record["finalWinnerPlaneOrdinal"] = final_label + 1
            record["globalLabelAdjusted"] = final_label != initial_labels[index]

    # Arrangement lines can intersect within centimetres of one another and
    # create numerical sliver cells.  Preserve complete coverage while
    # preventing those artefacts from becoming fabricated roof facets by
    # assigning each sliver to the non-sliver cell sharing its longest edge.
    minimum_partition_area = 0.25
    regular_indexes = [
        index
        for index, (cell, _) in enumerate(cell_assignments)
        if cell.area >= minimum_partition_area
    ]
    for index, (cell, winner) in enumerate(cell_assignments):
        if cell.area >= minimum_partition_area:
            continue
        neighbours = []
        for other_index in regular_indexes:
            other_cell, other_winner = cell_assignments[other_index]
            shared = float(cell.boundary.intersection(other_cell.boundary).length)
            if shared > 1e-8:
                neighbours.append(
                    (
                        -round(shared, 9),
                        round(float(cell.distance(other_cell)), 9),
                        other_index,
                        other_winner,
                    )
                )
        if neighbours:
            cell_assignments[index] = (cell, min(neighbours)[-1])

    assigned: dict[int, list[BaseGeometry]] = {}
    for cell, winner in cell_assignments:
        assigned.setdefault(winner, []).append(cell)

    regions: list[tuple[BaseGeometry, ConsolidatedPlane]] = []
    for index, assigned_cells in sorted(assigned.items()):
        combined = unary_union(assigned_cells)
        pieces = [combined] if combined.geom_type == "Polygon" else list(combined.geoms)
        regions.extend(
            (piece, candidates[index])
            for piece in pieces
            if piece.geom_type == "Polygon" and piece.area > 1e-8
        )
    union = unary_union([region for region, _ in regions]) if regions else Polygon()
    coverage = float(union.intersection(polygon).area) / max(float(polygon.area), 0.01)
    summed = sum(float(region.area) for region, _ in regions)
    overlap = max(0.0, summed - float(union.area)) / max(float(polygon.area), 0.01)
    if coverage < 0.995 or overlap > 0.005:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_PARTITION_INCOMPLETE",
            "Independent planes do not form one unambiguous partition of a Roofer facet.",
            details={
                "coverageRatio": round(coverage, 4),
                "overlapRatio": round(overlap, 4),
                "candidatePlaneCount": len(candidates),
            },
        )
    return regions


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    parts: list[Polygon] = []
    for item in getattr(geometry, "geoms", []):
        parts.extend(_polygon_parts(item))
    return parts


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [LineString(geometry.coords)]
    parts: list[LineString] = []
    for item in getattr(geometry, "geoms", []):
        parts.extend(_line_parts(item))
    return parts


def _merge_adjacent_supported_fragments(
    regions: list[tuple[BaseGeometry, ConsolidatedPlane]],
    lidar_points: np.ndarray,
    settings: Any,
) -> tuple[list[tuple[BaseGeometry, ConsolidatedPlane]], dict[str, Any]]:
    """Merge only adjacent fragments whose combined LiDAR fit stays planar."""

    merged = list(regions)
    merge_records: list[dict[str, Any]] = []
    configured_angle = float(
        getattr(settings, "facet_consolidation_merge_angle_degrees", 4.0)
    )
    maximum_fragment_angle = max(configured_angle, 6.0)
    maximum_fit_rmse = min(
        float(
            getattr(
                settings,
                "facet_consolidation_maximum_plane_rmse_meters",
                0.15,
            )
        ),
        max(
            0.075,
            float(
                getattr(
                    settings,
                    "facet_consolidation_maximum_local_residual_meters",
                    0.06,
                )
            )
            * 1.5,
        ),
    )
    while True:
        candidates: list[
            tuple[
                tuple[float, ...],
                int,
                int,
                BaseGeometry,
                ConsolidatedPlane,
                float,
                float,
                float,
                float,
                float,
            ]
        ] = []
        for left in range(len(merged)):
            left_region, left_plane = merged[left]
            for right in range(left + 1, len(merged)):
                right_region, right_plane = merged[right]
                if id(left_plane) == id(right_plane):
                    continue
                shared_length = float(
                    left_region.boundary.intersection(right_region.boundary).length
                )
                if shared_length <= 0.10:
                    continue
                angle = _normal_angle_degrees(
                    np.asarray(left_plane.normal), np.asarray(right_plane.normal)
                )
                if angle > maximum_fragment_angle:
                    continue
                shared_lines = _line_parts(
                    left_region.boundary.intersection(right_region.boundary)
                )
                if not shared_lines:
                    continue
                shared_line = max(shared_lines, key=lambda line: float(line.length))
                start = shared_line.coords[0]
                end = shared_line.coords[-1]
                edge_dx = float(end[0] - start[0])
                edge_dy = float(end[1] - start[1])
                edge_length = math.hypot(edge_dx, edge_dy)
                if edge_length <= 1e-8:
                    continue
                intersection_direction = np.cross(
                    np.asarray(left_plane.normal), np.asarray(right_plane.normal)
                )
                intersection_xy_length = math.hypot(
                    float(intersection_direction[0]),
                    float(intersection_direction[1]),
                )
                if intersection_xy_length <= 1e-8:
                    continue
                alignment = math.degrees(
                    math.acos(
                        min(
                            1.0,
                            abs(
                                (
                                    edge_dx * float(intersection_direction[0])
                                    + edge_dy * float(intersection_direction[1])
                                )
                                / (edge_length * intersection_xy_length)
                            ),
                        )
                    )
                )

                midpoint = shared_line.interpolate(0.5, normalized=True)
                normal_x, normal_y = -edge_dy / edge_length, edge_dx / edge_length

                def inward_slope(
                    region: BaseGeometry, plane: ConsolidatedPlane
                ) -> float:
                    positive = Point(
                        midpoint.x + normal_x * 0.01,
                        midpoint.y + normal_y * 0.01,
                    )
                    direction = 1.0 if region.covers(positive) else -1.0
                    gradient_x = -plane.normal[0] / plane.normal[2]
                    gradient_y = -plane.normal[1] / plane.normal[2]
                    return direction * (
                        gradient_x * normal_x + gradient_y * normal_y
                    )

                left_inward_slope = inward_slope(left_region, left_plane)
                right_inward_slope = inward_slope(right_region, right_plane)
                # This final merge is not a general angle heuristic. It is
                # permitted only for a small fragment at a boundary which is
                # demonstrably not the incident planes' intersection and for
                # which neither side has a classifiable dihedral derivative.
                if (
                    alignment <= 5.0
                    or abs(left_inward_slope) > 0.08
                    or abs(right_inward_slope) > 0.08
                ):
                    continue
                combined_indexes = np.asarray(
                    sorted(
                        set(left_plane.point_indexes)
                        | set(right_plane.point_indexes)
                    ),
                    dtype=np.int32,
                )
                if not len(combined_indexes) or int(np.max(combined_indexes)) >= len(
                    lidar_points
                ):
                    continue
                smaller_support_fraction = min(
                    len(left_plane.point_indexes), len(right_plane.point_indexes)
                ) / max(len(combined_indexes), 1)
                if smaller_support_fraction > 0.15:
                    continue
                normal, centroid, rmse = _fit_plane(lidar_points, combined_indexes)
                if rmse > maximum_fit_rmse:
                    continue
                combined_region = unary_union((left_region, right_region))
                if combined_region.geom_type != "Polygon" or not combined_region.is_valid:
                    continue
                plane = ConsolidatedPlane(
                    point_indexes=tuple(map(int, combined_indexes)),
                    normal=tuple(map(float, normal)),
                    centroid=tuple(map(float, centroid)),
                    rmse_meters=rmse,
                    support_hull=_support_hull(lidar_points, combined_indexes),
                    support_coordinates=_support_coordinate_sample(
                        lidar_points, combined_indexes
                    ),
                    dsm_residual_rmse_meters=max(
                        value
                        for value in (
                            left_plane.dsm_residual_rmse_meters,
                            right_plane.dsm_residual_rmse_meters,
                            0.0,
                        )
                        if value is not None
                    ),
                )
                key = (
                    round(rmse, 9),
                    round(angle, 9),
                    -round(shared_length, 9),
                    round(combined_region.centroid.x, 6),
                    round(combined_region.centroid.y, 6),
                )
                candidates.append(
                    (
                        key,
                        left,
                        right,
                        combined_region,
                        plane,
                        angle,
                        smaller_support_fraction,
                        alignment,
                        left_inward_slope,
                        right_inward_slope,
                    )
                )
        if not candidates:
            break
        (
            _,
            left,
            right,
            combined_region,
            combined_plane,
            angle,
            smaller_support_fraction,
            alignment,
            left_inward_slope,
            right_inward_slope,
        ) = min(candidates, key=lambda item: item[0])
        merge_records.append(
            {
                "angleDegrees": round(angle, 4),
                "combinedRmseMeters": round(combined_plane.rmse_meters, 4),
                "smallerSupportFraction": round(smaller_support_fraction, 4),
                "boundaryAlignmentDegrees": round(alignment, 4),
                "inwardSlopes": [
                    round(left_inward_slope, 4),
                    round(right_inward_slope, 4),
                ],
            }
        )
        merged = [
            value
            for index, value in enumerate(merged)
            if index not in {left, right}
        ] + [(combined_region, combined_plane)]
    return merged, {
        "validation": "COMBINED_PLANE_SUPPORT_REQUIRED",
        "mergeCount": len(merge_records),
        "maximumFragmentAngleDegrees": maximum_fragment_angle,
        "maximumCombinedRmseMeters": round(maximum_fit_rmse, 4),
        "merges": merge_records,
    }


def _validate_watertight_partition(
    regions: list[tuple[BaseGeometry, ConsolidatedPlane]],
    roofprint: BaseGeometry,
) -> dict[str, Any]:
    """Require a complete non-overlapping two-manifold planar subdivision."""

    polygons = [region for region, _ in regions]
    if not polygons or any(
        polygon.geom_type != "Polygon" or not polygon.is_valid for polygon in polygons
    ):
        raise UnreliableGeometryError(
            "FACET_GLOBAL_ARRANGEMENT_INVALID",
            "The global facet arrangement contains an invalid surface.",
        )
    union = unary_union(polygons)
    roof_area = max(float(roofprint.area), 0.01)
    gap_area = float(roofprint.difference(union).area)
    outside_area = float(union.difference(roofprint).area)
    overlap_area = max(0.0, sum(float(item.area) for item in polygons) - float(union.area))
    maximum_area_error = max(1e-6, roof_area * 1e-8)
    if max(gap_area, outside_area, overlap_area) > maximum_area_error:
        raise UnreliableGeometryError(
            "FACET_GLOBAL_ARRANGEMENT_INCOMPLETE",
            "The global facet arrangement has a material gap or overlap.",
            details={
                "gapAreaSquareMeters": round(gap_area, 6),
                "outsideAreaSquareMeters": round(outside_area, 6),
                "overlapAreaSquareMeters": round(overlap_area, 6),
                "maximumAreaErrorSquareMeters": round(maximum_area_error, 6),
            },
        )

    noded = unary_union([polygon.boundary for polygon in polygons])
    dangling = 0
    non_manifold = 0
    non_manifold_lengths: list[float] = []
    dangling_lengths: list[float] = []
    redundant_segment_count = 0
    redundant_segment_length = 0.0
    failure_patterns: dict[str, int] = {}
    exterior_segments = 0
    interior_segments = 0
    tolerance = 1e-6
    # The arrangement has already been noded by GEOS.  Validate ownership on
    # the resulting boundary segments themselves instead of probing two tiny
    # offsets from each segment.  Offset probes become numerically unstable at
    # Florida State Plane coordinate magnitudes and previously reported
    # sub-millimetre ``INTERIOR_SIDE_UNOWNED`` seams even though the same
    # segment was present on both facet boundaries.  Boundary ownership is the
    # direct two-manifold invariant: one owner on the roofprint exterior and
    # exactly two owners everywhere else.
    boundary_tolerance = 1e-7
    for line in _line_parts(noded):
        coordinates = list(line.coords)
        for start, end in zip(coordinates, coordinates[1:]):
            segment = LineString([start, end])
            if segment.length <= tolerance:
                continue
            midpoint = segment.interpolate(0.5, normalized=True)
            boundary_owners = [
                index
                for index, polygon in enumerate(polygons)
                if polygon.boundary.distance(midpoint) <= boundary_tolerance
            ]
            on_roofprint_boundary = (
                roofprint.boundary.distance(midpoint) <= boundary_tolerance
            )
            if on_roofprint_boundary:
                exterior_segments += 1
                if len(boundary_owners) != 1:
                    non_manifold += 1
                    non_manifold_lengths.append(float(segment.length))
                    failure_patterns["EXTERIOR_OWNERSHIP"] = (
                        failure_patterns.get("EXTERIOR_OWNERSHIP", 0) + 1
                    )
            elif roofprint.covers(midpoint):
                interior_segments += 1
                if len(boundary_owners) < 2:
                    dangling += 1
                    dangling_lengths.append(float(segment.length))
                    failure_patterns["INTERIOR_SIDE_UNOWNED"] = (
                        failure_patterns.get("INTERIOR_SIDE_UNOWNED", 0) + 1
                    )
                elif len(boundary_owners) > 2:
                    non_manifold += 1
                    non_manifold_lengths.append(float(segment.length))
                    failure_patterns["INTERIOR_MULTIPLE_OWNERS"] = (
                        failure_patterns.get("INTERIOR_MULTIPLE_OWNERS", 0) + 1
                    )
            else:
                non_manifold += 1
                non_manifold_lengths.append(float(segment.length))
                failure_patterns["OUTSIDE_ROOFPRINT"] = (
                    failure_patterns.get("OUTSIDE_ROOFPRINT", 0) + 1
                )
    if dangling or non_manifold:
        raise UnreliableGeometryError(
            "FACET_GLOBAL_ARRANGEMENT_NON_MANIFOLD",
            "The global facet arrangement is not a watertight two-manifold.",
            details={
                "danglingInteriorSegmentCount": dangling,
                "nonManifoldSegmentCount": non_manifold,
                "danglingInteriorFeet": round(sum(dangling_lengths) * 3.280839895, 3),
                "nonManifoldFeet": round(sum(non_manifold_lengths) * 3.280839895, 3),
                "maximumNonManifoldSegmentFeet": round(
                    max(non_manifold_lengths, default=0.0) * 3.280839895, 3
                ),
                "failurePatterns": failure_patterns,
                "exteriorSegmentCount": exterior_segments,
                "interiorSegmentCount": interior_segments,
            },
        )
    return {
        "validation": "PASSED",
        "facetCount": len(polygons),
        "interiorRingCount": sum(len(polygon.interiors) for polygon in polygons),
        "interiorRingAreasSquareMeters": sorted(
            round(float(Polygon(ring).area), 6)
            for polygon in polygons
            for ring in polygon.interiors
        ),
        "gapAreaSquareMeters": round(gap_area, 9),
        "outsideAreaSquareMeters": round(outside_area, 9),
        "overlapAreaSquareMeters": round(overlap_area, 9),
        "exteriorSegmentCount": exterior_segments,
        "interiorSegmentCount": interior_segments,
        "suppressedRedundantSegmentCount": redundant_segment_count,
        "suppressedRedundantSegmentFeet": round(
            redundant_segment_length * 3.280839895, 3
        ),
        "exteriorOwnership": 1,
        "interiorOwnership": 2,
    }


def _solar_plane_audit(
    planes: list[ConsolidatedPlane],
    solar_reference: Any,
    settings: Any,
    *,
    enforce_conflicts: bool = True,
) -> tuple[list[ConsolidatedPlane], dict[str, Any]]:
    solar_facets = list(getattr(solar_reference, "facets", []) or [])
    if not solar_facets:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SOLAR_MISSING",
            "Google Solar supplied no roof-plane evidence for consolidation.",
        )
    maximum_pitch = float(
        getattr(settings, "maximum_solar_pitch_variance_degrees", 10.0)
    )
    matches = []
    accepted: list[ConsolidatedPlane] = []
    rejected: list[dict[str, Any]] = []
    total_support = sum(len(plane.point_indexes) for plane in planes)
    for index, plane in enumerate(planes, start=1):
        ranked = []
        for solar_index, solar in enumerate(solar_facets, start=1):
            pitch_variance = abs(plane.pitch_degrees - float(solar.pitchDegrees))
            solar_azimuth = getattr(solar, "azimuthDegrees", None)
            azimuth_variance = None
            if solar_azimuth is not None and plane.pitch_degrees > 5:
                azimuth_variance = abs(
                    (plane.azimuth_degrees - float(solar_azimuth) + 180) % 360 - 180
                )
            score = pitch_variance + (azimuth_variance or 0.0) / 30.0
            ranked.append((score, pitch_variance, azimuth_variance, solar_index))
        _, pitch_variance, azimuth_variance, solar_index = min(ranked)
        match = {
                    "facetOrdinal": index,
                    "supportPoints": len(plane.point_indexes),
                    "supportFraction": round(
                        len(plane.point_indexes) / max(total_support, 1), 4
                    ),
                    "solarFacetOrdinal": solar_index,
                    "pitchVarianceDegrees": round(pitch_variance, 3),
                    "azimuthVarianceDegrees": (
                        None if azimuth_variance is None else round(azimuth_variance, 3)
                    ),
                }
        if pitch_variance > maximum_pitch:
            rejected.append(match)
        else:
            accepted.append(plane)
            matches.append(match)
    rejected_support = sum(item["supportPoints"] for item in rejected)
    rejected_support_fraction = rejected_support / max(total_support, 1)
    maximum_rejected_support_fraction = float(
        getattr(settings, "facet_consolidation_maximum_solar_rejected_support_fraction", 0.05)
    )
    if enforce_conflicts and rejected_support_fraction > maximum_rejected_support_fraction:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_SOLAR_PITCH_CONFLICT",
            "Material independent plane support disagrees with Google Solar pitch evidence.",
            details={
                "rejectedPlaneCount": len(rejected),
                "rejectedSupportFraction": round(rejected_support_fraction, 4),
                "maximumRejectedSupportFraction": maximum_rejected_support_fraction,
                "maximumPitchVarianceDegrees": maximum_pitch,
                "rejectedPlanes": rejected,
            },
        )
    return (accepted if enforce_conflicts else planes), {
        "solarFacetCount": len(solar_facets),
        "reconstructedPlaneCount": len(planes),
        "acceptedPlaneCount": len(accepted if enforce_conflicts else planes),
        "rejectedNoisePlaneCount": len(rejected),
        "rejectedNoiseSupportFraction": round(rejected_support_fraction, 4),
        "conflictsEnforced": enforce_conflicts,
        "matches": matches,
        "rejectedNoisePlanes": rejected,
    }


def _temporal_evidence_role(
    solar_imagery_date: str | None,
    lidar_reference_date: str | None,
) -> dict[str, Any]:
    """Decide whether Solar evidence may veto the selected LiDAR geometry."""

    try:
        solar_date = date.fromisoformat(str(solar_imagery_date or ""))
        lidar_date = date.fromisoformat(str(lidar_reference_date or ""))
    except ValueError as error:
        raise UnreliableGeometryError(
            "FACET_EVIDENCE_DATE_INVALID",
            "Facet reconciliation requires dated Solar and LiDAR evidence.",
        ) from error
    historical_only = solar_date < lidar_date
    return {
        "solarImageryDate": solar_date.isoformat(),
        "lidarReferenceDate": lidar_date.isoformat(),
        "role": "HISTORICAL_CORROBORATION_ONLY" if historical_only else "VALIDATION",
        "mayVetoNewerLidar": not historical_only,
    }


def _reconcile_solar_dsm_support(
    planes: list[ConsolidatedPlane],
    dsm_path: Path,
    input_crs: str,
    settings: Any,
    *,
    lidar_points: np.ndarray | None = None,
) -> tuple[list[ConsolidatedPlane], dict[str, Any]]:
    """Validate and, only with dual support, refine planes using Solar DSM."""

    try:
        gdal = importlib.import_module("osgeo.gdal")
        osr = importlib.import_module("osgeo.osr")
        dataset = gdal.Open(str(dsm_path), gdal.GA_ReadOnly)
    except (ImportError, OSError, RuntimeError) as error:
        raise UnreliableGeometryError(
            "SOLAR_DSM_RUNTIME_UNAVAILABLE",
            "The Google Solar DSM could not be opened for facet reconciliation.",
        ) from error
    if dataset is None or dataset.RasterCount != 1 or not dataset.GetProjection():
        raise UnreliableGeometryError(
            "SOLAR_DSM_INVALID",
            "The Google Solar DSM is missing its elevation band or coordinate reference system.",
        )
    try:
        inverse = gdal.InvGeoTransform(dataset.GetGeoTransform())
        source = osr.SpatialReference()
        source.SetFromUserInput(input_crs)
        target = osr.SpatialReference()
        target.ImportFromWkt(dataset.GetProjection())
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transformer = osr.CoordinateTransformation(source, target)
        reverse_transformer = osr.CoordinateTransformation(target, source)
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise UnreliableGeometryError(
            "SOLAR_DSM_GEOREFERENCE_INVALID",
            "The Google Solar DSM georeference could not be reconciled with LiDAR.",
        ) from error

    proposed: list[tuple[int, int, float, float, float]] = []
    for plane_index, plane in enumerate(planes, start=1):
        indexed_coordinates: list[tuple[int, float, float, float]] = []
        if lidar_points is not None:
            valid_indexes = [
                int(point_index)
                for point_index in plane.point_indexes
                if 0 <= int(point_index) < len(lidar_points)
            ]
            valid_indexes.sort(
                key=lambda point_index: tuple(
                    float(value) for value in lidar_points[point_index]
                )
            )
            if len(valid_indexes) > 96:
                offsets = np.linspace(
                    0, len(valid_indexes) - 1, 96, dtype=np.int32
                )
                valid_indexes = [valid_indexes[int(offset)] for offset in offsets]
            indexed_coordinates = [
                (
                    point_index,
                    float(lidar_points[point_index][0]),
                    float(lidar_points[point_index][1]),
                    float(lidar_points[point_index][2]),
                )
                for point_index in valid_indexes
            ]
        if not indexed_coordinates:
            indexed_coordinates = [
                (-1, float(row[0]), float(row[1]), float(row[2]))
                for row in plane.support_coordinates
            ]
        # Prefer actual support returns away from mixed pixels at eaves,
        # rakes, ridges, and valleys. Progressively relax the interior margin
        # only for physically small facets.
        for margin in (0.25, 0.10, 0.0):
            interior = (
                plane.support_hull.buffer(-margin)
                if margin
                else plane.support_hull
            )
            if interior.is_empty:
                continue
            filtered = [
                (point_index, x, y, z)
                for point_index, x, y, z in indexed_coordinates
                if interior.covers(Point(float(x), float(y)))
            ]
            if len(filtered) >= 5 or (margin == 0.0 and filtered):
                indexed_coordinates = filtered
                break
        if not indexed_coordinates:
            center = plane.support_hull.representative_point()
            indexed_coordinates = [
                (
                    -1,
                    float(center.x),
                    float(center.y),
                    _plane_height(plane, float(center.x), float(center.y)),
                )
            ]
        for point_index, x, y, z in indexed_coordinates:
            proposed.append((plane_index, point_index, x, y, z))

    samples: list[DsmResidualSample] = []
    per_plane_counts: dict[int, int] = {}
    proposed_pixels: set[tuple[int, int, int]] = set()
    sampled_pixels: set[tuple[int, int, int]] = set()
    geotransform = dataset.GetGeoTransform()
    for plane_index, point_index, x, y, measured_lidar_height in proposed:
        try:
            target_x, target_y, _ = transformer.TransformPoint(x, y)
            pixel_x, pixel_y = gdal.ApplyGeoTransform(inverse, target_x, target_y)
            column, row = int(math.floor(pixel_x)), int(math.floor(pixel_y))
            pixel_key = (plane_index, column, row)
            proposed_pixels.add(pixel_key)
            if not (0 <= column < dataset.RasterXSize and 0 <= row < dataset.RasterYSize):
                continue
            if pixel_key in sampled_pixels:
                continue
            value = band.ReadAsArray(column, row, 1, 1)
            if value is None or value.size != 1:
                continue
            dsm_height = float(value[0, 0])
            if not math.isfinite(dsm_height):
                continue
            if nodata is not None and math.isclose(dsm_height, float(nodata), abs_tol=1e-6):
                continue
            pixel_center_x, pixel_center_y = gdal.ApplyGeoTransform(
                geotransform, column + 0.5, row + 0.5
            )
            lidar_x, lidar_y, _ = reverse_transformer.TransformPoint(
                pixel_center_x, pixel_center_y
            )
            fitted_lidar_height = _plane_height(
                planes[plane_index - 1], float(lidar_x), float(lidar_y)
            )
        except (RuntimeError, TypeError, ValueError, OverflowError):
            continue
        sampled_pixels.add(pixel_key)
        samples.append(
            DsmResidualSample(
                plane_index=plane_index,
                point_index=point_index,
                x=float(lidar_x),
                y=float(lidar_y),
                lidar_height=float(measured_lidar_height),
                dsm_minus_lidar=dsm_height - fitted_lidar_height,
            )
        )
        per_plane_counts[plane_index] = per_plane_counts.get(plane_index, 0) + 1

    coverage = len(sampled_pixels) / max(len(proposed_pixels), 1)
    minimum_coverage = float(
        getattr(settings, "solar_dsm_minimum_sample_coverage", 0.80)
    )
    supported_planes = len(per_plane_counts)
    minimum_facet_samples = 3
    under_sampled_planes = [
        plane_index
        for plane_index in range(1, len(planes) + 1)
        if per_plane_counts.get(plane_index, 0) < minimum_facet_samples
    ]
    if (
        coverage < minimum_coverage
        or supported_planes != len(planes)
        or under_sampled_planes
    ):
        raise UnreliableGeometryError(
            "SOLAR_DSM_COVERAGE_INSUFFICIENT",
            "The Google Solar DSM does not cover every consolidated facet.",
            details={
                "sampleCoverage": round(coverage, 4),
                "minimumSampleCoverage": minimum_coverage,
                "supportedFacetCount": supported_planes,
                "facetCount": len(planes),
                "minimumSamplesPerFacet": minimum_facet_samples,
                "underSampledFacetCount": len(under_sampled_planes),
            },
        )
    offsets = np.asarray(
        [sample.dsm_minus_lidar for sample in samples], dtype=float
    )
    vertical_offset = float(np.median(offsets))
    facet_offsets: dict[int, float] = {}
    facet_rmses: dict[int, float] = {}
    centered_values = []
    rejected_outlier_count = 0
    scattered_obstruction_count = 0
    coherent_conflict_count = 0
    coherent_component_audit: list[dict[str, Any]] = []
    split_planes_by_ordinal: dict[int, list[ConsolidatedPlane]] = {}
    maximum_outlier_fraction = 0.20
    maximum_scattered_obstruction_fraction = 0.35
    for plane_index in sorted(per_plane_counts):
        plane_scattered_obstruction_count = 0
        plane_samples = [
            sample for sample in samples if sample.plane_index == plane_index
        ]
        plane_offsets = np.asarray(
            [sample.dsm_minus_lidar for sample in plane_samples],
            dtype=float,
        )
        facet_offset = float(np.median(plane_offsets))
        facet_centered = plane_offsets - facet_offset
        median_absolute_deviation = float(np.median(np.abs(facet_centered)))
        robust_limit = max(0.35, 3.5 * 1.4826 * median_absolute_deviation)
        inlier_mask = np.abs(facet_centered) <= robust_limit
        minimum_inliers = max(
            minimum_facet_samples,
            math.ceil((1.0 - maximum_outlier_fraction) * len(plane_offsets)),
        )
        spatial_components = _spatial_dsm_residual_components(
            plane_samples,
            facet_centered,
            residual_threshold_meters=robust_limit,
            maximum_gap_meters=max(
                0.50,
                float(
                    getattr(
                        settings,
                        "facet_consolidation_neighbor_radius_meters",
                        0.40,
                    )
                )
                * 2.0,
            ),
        )
        coherent_sample_indexes: set[int] = set()
        minimum_coherent_samples = max(5, math.ceil(len(plane_samples) * 0.15))
        source_plane = planes[plane_index - 1]
        for component in spatial_components:
            component_points = [
                Point(plane_samples[index].x, plane_samples[index].y)
                for index in component
            ]
            component_hull = MultiPoint(component_points).convex_hull
            component_area = float(getattr(component_hull, "area", 0.0))
            edge_samples = sum(
                source_plane.support_hull.boundary.distance(point) <= 0.25
                for point in component_points
            )
            edge_fraction = edge_samples / max(len(component), 1)
            coherent = (
                len(component) >= minimum_coherent_samples
                and component_area >= 0.25
                and edge_fraction < 0.60
            )
            component_record = {
                "facetOrdinal": plane_index,
                "sampleCount": len(component),
                "areaSquareMeters": round(component_area, 4),
                "edgeSampleFraction": round(edge_fraction, 4),
                "classification": "SCATTERED_OR_EDGE_OBSTRUCTION",
            }
            if not coherent:
                scattered_obstruction_count += len(component)
                plane_scattered_obstruction_count += len(component)
                coherent_component_audit.append(component_record)
                continue
            coherent_sample_indexes.update(component)

            # A DSM residual becomes a new roof surface only when the LiDAR
            # returns in the same coherent region independently fit a second
            # supported plane.  Otherwise it is an obstruction/change signal,
            # never a fabricated facet.
            dual_supported = False
            if lidar_points is not None:
                support_region = component_hull.buffer(0.15)
                split_indexes = np.asarray(
                    sorted(
                        point_index
                        for point_index in source_plane.point_indexes
                        if support_region.covers(
                            Point(
                                float(lidar_points[point_index][0]),
                                float(lidar_points[point_index][1]),
                            )
                        )
                    ),
                    dtype=np.int32,
                )
                remainder_indexes = np.asarray(
                    sorted(set(source_plane.point_indexes) - set(split_indexes)),
                    dtype=np.int32,
                )
                minimum_plane_points = int(
                    getattr(settings, "facet_consolidation_minimum_points", 20)
                )
                if (
                    len(split_indexes) >= minimum_plane_points
                    and len(remainder_indexes) >= minimum_plane_points
                ):
                    split_normal, split_centroid, split_rmse = _fit_plane(
                        lidar_points, split_indexes
                    )
                    base_normal, base_centroid, base_rmse = _fit_plane(
                        lidar_points, remainder_indexes
                    )
                    split_angle = _normal_angle_degrees(split_normal, base_normal)
                    split_separation = abs(
                        float(
                            np.dot(
                                split_normal,
                                split_centroid - base_centroid,
                            )
                        )
                    )
                    maximum_fit_rmse = float(
                        getattr(
                            settings,
                            "facet_consolidation_maximum_plane_rmse_meters",
                            0.15,
                        )
                    )
                    dual_supported = (
                        split_rmse <= maximum_fit_rmse
                        and base_rmse <= maximum_fit_rmse
                        and (split_angle >= 1.0 or split_separation >= 0.12)
                        and split_normal[2] >= math.cos(math.radians(60.0))
                    )
                    component_record.update(
                        {
                            "lidarSplitAngleDegrees": round(split_angle, 4),
                            "lidarSplitSeparationMeters": round(
                                split_separation, 4
                            ),
                            "lidarSplitRmseMeters": round(split_rmse, 4),
                            "lidarRemainderRmseMeters": round(base_rmse, 4),
                        }
                    )
                    if dual_supported:
                        split_planes_by_ordinal[plane_index] = [
                            ConsolidatedPlane(
                                point_indexes=tuple(map(int, remainder_indexes)),
                                normal=tuple(map(float, base_normal)),
                                centroid=tuple(map(float, base_centroid)),
                                rmse_meters=base_rmse,
                                support_hull=_support_hull(
                                    lidar_points, remainder_indexes
                                ),
                                support_coordinates=_support_coordinate_sample(
                                    lidar_points, remainder_indexes
                                ),
                            ),
                            ConsolidatedPlane(
                                point_indexes=tuple(map(int, split_indexes)),
                                normal=tuple(map(float, split_normal)),
                                centroid=tuple(map(float, split_centroid)),
                                rmse_meters=split_rmse,
                                support_hull=_support_hull(
                                    lidar_points, split_indexes
                                ),
                                support_coordinates=_support_coordinate_sample(
                                    lidar_points, split_indexes
                                ),
                            ),
                        ]
            component_record["classification"] = (
                "DUAL_SUPPORTED_SECONDARY_ROOF_SURFACE"
                if dual_supported
                else "COHERENT_DSM_WITHOUT_LIDAR_PLANE_SUPPORT"
            )
            coherent_component_audit.append(component_record)
            if not dual_supported:
                coherent_conflict_count += 1

        if coherent_conflict_count:
            raise UnreliableGeometryError(
                "SOLAR_DSM_COHERENT_CONFLICT",
                "A coherent DSM surface lacks independent LiDAR plane support.",
                details={
                    "facetOrdinal": plane_index,
                    "coherentConflictCount": coherent_conflict_count,
                    "components": coherent_component_audit,
                },
            )
        # Scattered, boundary-dominated, wall-like, and unsupported steep
        # samples are treated as obstructions only; they cannot become facets.
        if int(np.count_nonzero(inlier_mask)) < minimum_inliers:
            outlier_fraction = int(np.count_nonzero(~inlier_mask)) / max(
                len(plane_offsets), 1
            )
            scattered_only = (
                plane_scattered_obstruction_count
                == int(np.count_nonzero(~inlier_mask))
            )
            if (
                not scattered_only
                or outlier_fraction > maximum_scattered_obstruction_fraction
            ):
                raise UnreliableGeometryError(
                    "SOLAR_DSM_SHAPE_CONFLICT",
                    "Google Solar DSM elevations disagree with the consolidated LiDAR roof shape.",
                    details={
                        "facetOrdinal": plane_index,
                        "sampleCount": int(len(plane_offsets)),
                        "inlierSampleCount": int(np.count_nonzero(inlier_mask)),
                        "maximumOutlierFraction": maximum_outlier_fraction,
                        "maximumScatteredObstructionFraction": (
                            maximum_scattered_obstruction_fraction
                        ),
                        "outlierFraction": round(outlier_fraction, 4),
                    },
                )
        inlier_offsets = plane_offsets[inlier_mask]
        facet_offset = float(np.median(inlier_offsets))
        facet_centered = inlier_offsets - facet_offset
        facet_offsets[plane_index] = facet_offset
        rejected_outlier_count += int(len(plane_offsets) - len(inlier_offsets))
        facet_rmses[plane_index] = float(
            np.sqrt(np.mean(np.square(facet_centered)))
        )
        centered_values.extend(float(value) for value in facet_centered)
    centered = np.asarray(centered_values, dtype=float)
    rmse = float(np.sqrt(np.mean(np.square(centered))))
    maximum_facet_rmse = max(facet_rmses.values())
    maximum_rmse = float(
        getattr(settings, "solar_dsm_maximum_centered_rmse_meters", 0.75)
    )
    if rmse > maximum_rmse or maximum_facet_rmse > maximum_rmse:
        raise UnreliableGeometryError(
            "SOLAR_DSM_SHAPE_CONFLICT",
            "Google Solar DSM elevations disagree with the consolidated LiDAR roof shape.",
            details={
                "centeredRmseMeters": round(rmse, 4),
                "maximumFacetCenteredRmseMeters": round(maximum_facet_rmse, 4),
                "maximumCenteredRmseMeters": maximum_rmse,
                "sampleCount": len(samples),
                "rejectedOutlierCount": rejected_outlier_count,
            },
        )
    facet_offset_values = list(facet_offsets.values())
    facet_offset_range = max(facet_offset_values) - min(facet_offset_values)
    maximum_facet_offset_range = float(
        getattr(settings, "solar_dsm_maximum_facet_offset_range_meters", 1.50)
    )
    if facet_offset_range > maximum_facet_offset_range:
        raise UnreliableGeometryError(
            "SOLAR_DSM_FACET_OFFSET_CONFLICT",
            "Google Solar DSM facet elevations are inconsistent with the LiDAR roof structure.",
            details={
                "facetOffsetRangeMeters": round(facet_offset_range, 4),
                "maximumFacetOffsetRangeMeters": maximum_facet_offset_range,
                "facetCount": len(facet_offsets),
            },
        )
    refined_planes: list[ConsolidatedPlane] = []
    for plane_index, plane in enumerate(planes, start=1):
        refined_planes.extend(
            replace(
                refined,
                dsm_residual_rmse_meters=facet_rmses.get(plane_index),
            )
            for refined in split_planes_by_ordinal.get(plane_index, [plane])
        )
    return refined_planes, {
        "validation": "PASSED",
        "sampleCount": len(samples),
        "sampleCoverage": round(coverage, 4),
        "supportedFacetCount": supported_planes,
        "centeredRmseMeters": round(rmse, 4),
        "maximumFacetCenteredRmseMeters": round(maximum_facet_rmse, 4),
        "facetCenteredRmseMeters": {
            str(key): round(value, 4) for key, value in sorted(facet_rmses.items())
        },
        "facetOffsetRangeMeters": round(facet_offset_range, 4),
        "verticalDatumOffsetMeters": round(vertical_offset, 4),
        "verticalDatumNormalization": "GLOBAL_MEDIAN_DSM_MINUS_LIDAR",
        "rejectedOutlierCount": rejected_outlier_count,
        "scatteredObstructionSampleCount": scattered_obstruction_count,
        "coherentResidualComponents": coherent_component_audit,
        "dualSupportedSplitCount": len(split_planes_by_ordinal),
        "maximumOutlierFraction": maximum_outlier_fraction,
        "maximumScatteredObstructionFraction": (
            maximum_scattered_obstruction_fraction
        ),
    }


def validate_solar_dsm_support(
    planes: list[ConsolidatedPlane],
    dsm_path: Path,
    input_crs: str,
    settings: Any,
) -> dict[str, Any]:
    """Compatibility wrapper for validation-only callers and tests."""

    _, audit = _reconcile_solar_dsm_support(
        planes, dsm_path, input_crs, settings
    )
    return audit


def consolidate_roofer_feature(
    feature: dict[str, Any],
    transform: dict[str, Any] | None,
    points: np.ndarray,
    solar_reference: Any,
    settings: Any,
    *,
    solar_dsm_path: Path | None = None,
    projected_crs: str | None = None,
    solar_imagery_date: str | None = None,
    lidar_reference_date: str | None = None,
) -> tuple[dict[str, Any], None, dict[str, Any]]:
    """Return a point-supported roof-only CityJSON feature and audit record."""

    from .cityjson_geometry import _roof_facets

    roofer_facets, attributes = _roof_facets(
        feature, transform, maximum_formula_error_percent=0.5
    )
    if any(facet.opening_count for facet in roofer_facets):
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_OPENING_UNSUPPORTED",
            "Roof openings cannot yet be preserved through facet consolidation.",
        )
    roofer_polygons = [
        Polygon([(vertex[0], vertex[1]) for vertex in facet.vertices])
        for facet in roofer_facets
    ]
    roofprint = unary_union(roofer_polygons)
    if roofprint.is_empty or not roofprint.is_valid:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_ROOFPRINT_INVALID",
            "Roofer roof facets do not form valid planimetric coverage.",
        )
    temporal_audit = _temporal_evidence_role(
        solar_imagery_date or getattr(solar_reference, "imageryDate", None),
        lidar_reference_date,
    )
    enforce_solar = bool(temporal_audit["mayVetoNewerLidar"])
    planes, audit = discover_consolidated_planes(points, roofprint, settings)
    crop_buffer = float(
        getattr(settings, "facet_consolidation_crop_buffer_meters", 0.20)
    )
    consolidation_lidar_points = points[
        contains_xy(roofprint.buffer(crop_buffer), points[:, 0], points[:, 1])
    ]
    planes, solar_audit = _solar_plane_audit(
        planes,
        solar_reference,
        settings,
        enforce_conflicts=enforce_solar,
    )
    dsm_audit: dict[str, Any] | None = None
    if getattr(settings, "solar_dsm_enabled", False):
        if solar_dsm_path is None or projected_crs is None:
            raise UnreliableGeometryError(
                "SOLAR_DSM_EVIDENCE_MISSING",
                "Facet consolidation requires the dated Google Solar DSM evidence.",
            )
        if enforce_solar:
            planes, dsm_audit = _reconcile_solar_dsm_support(
                planes,
                solar_dsm_path,
                projected_crs,
                settings,
                lidar_points=consolidation_lidar_points,
            )
            planes, post_dsm_solar_audit = _solar_plane_audit(
                planes,
                solar_reference,
                settings,
                enforce_conflicts=True,
            )
            solar_audit["postDsmRefinement"] = post_dsm_solar_audit
        else:
            dsm_audit = {
                "validation": "HISTORICAL_CORROBORATION_ONLY",
                "conflictsEnforced": False,
                "solarImageryDate": temporal_audit["solarImageryDate"],
                "lidarReferenceDate": temporal_audit["lidarReferenceDate"],
                "verticalDatumNormalization": "NOT_APPLIED_TO_NEWER_LIDAR",
            }
    active_matches = (
        list(
            (solar_audit.get("postDsmRefinement") or solar_audit).get(
                "matches", []
            )
        )
        if enforce_solar
        else []
    )
    plane_evidence: dict[int, dict[str, float | None]] = {}
    if len(active_matches) == len(planes):
        for plane, match in zip(planes, active_matches):
            plane_evidence[id(plane)] = {
                "dsmRmseMeters": plane.dsm_residual_rmse_meters,
                "solarPitchVarianceDegrees": match.get("pitchVarianceDegrees"),
                "solarAzimuthVarianceDegrees": match.get(
                    "azimuthVarianceDegrees"
                ),
            }
    else:
        for plane in planes:
            plane_evidence[id(plane)] = {
                "dsmRmseMeters": plane.dsm_residual_rmse_meters,
                "solarPitchVarianceDegrees": None,
                "solarAzimuthVarianceDegrees": None,
            }
    plane_indexes = {id(plane): index for index, plane in enumerate(planes)}

    regions_by_plane: dict[int, list[BaseGeometry]] = {}
    candidate_counts: list[int] = []
    partition_counts: list[int] = []
    cell_assignment_audit: list[dict[str, Any]] = []
    assignment_gap = float(
        getattr(settings, "facet_consolidation_assignment_gap_meters", 1.0)
    )
    global_components = _polygon_parts(roofprint)
    for polygon in global_components:
        candidates = [
            plane
            for plane in planes
            if not plane.support_hull.intersection(
                polygon.buffer(assignment_gap)
            ).is_empty
        ]
        if not candidates:
            raise UnreliableGeometryError(
                "FACET_GLOBAL_ARRANGEMENT_UNSUPPORTED",
                "A connected roof component has no independent plane support.",
            )
        candidates.sort(
            key=lambda plane: (
                tuple(round(value, 6) for value in plane.centroid),
                tuple(round(value, 6) for value in plane.normal),
            )
        )
        candidate_counts.append(len(candidates))
        partitions = _partition_roofer_facet(
            polygon,
            candidates,
            adjacency_gap_meters=assignment_gap,
            plane_evidence=plane_evidence,
            assignment_audit=cell_assignment_audit,
            lidar_points=consolidation_lidar_points,
        )
        partition_counts.append(len(partitions))
        for region, plane in partitions:
            plane_index = plane_indexes[id(plane)]
            regions_by_plane.setdefault(plane_index, []).append(region)

    piece_assignments: list[tuple[BaseGeometry, int]] = []
    for plane_index, regions in regions_by_plane.items():
        combined = unary_union(regions)
        pieces = [combined] if combined.geom_type == "Polygon" else list(combined.geoms)
        for piece in pieces:
            if piece.geom_type != "Polygon" or piece.area <= 1e-8:
                continue
            piece_assignments.append((piece, plane_index))

    # A half-space can extend a plane into a disconnected Roofer section that
    # contains none of that plane's LiDAR support.  Reassign such extrapolated
    # pieces to the plane with the strongest local support evidence before
    # disconnected components are finalized as separate physical facets.
    for index, (piece, plane_index) in enumerate(piece_assignments):
        assigned_overlap = float(
            planes[plane_index].support_hull.intersection(piece).area
        )
        if assigned_overlap > 0.05:
            continue
        rankings = []
        probe = piece.representative_point()
        for candidate_index, plane in enumerate(planes):
            overlap = float(plane.support_hull.intersection(piece).area)
            rankings.append(
                (
                    -round(overlap, 9),
                    round(float(plane.support_hull.distance(probe)), 9),
                    -len(plane.point_indexes),
                    candidate_index,
                )
            )
        piece_assignments[index] = (piece, min(rankings)[-1])

    # A sliver can survive where two global intersection lines nearly coincide.
    # Resolve it against the complete roof partition so
    # no sub-threshold CityJSON facet is emitted and no roofprint area is
    # discarded.
    for index, (piece, plane_index) in enumerate(piece_assignments):
        if piece.area > 0.25:
            continue
        neighbours = []
        for other_index, (other_piece, other_plane_index) in enumerate(piece_assignments):
            if other_index == index or other_piece.area <= 0.25:
                continue
            distance = float(piece.distance(other_piece))
            if distance > 1e-6:
                continue
            shared = float(
                piece.boundary.buffer(1e-7).intersection(other_piece.boundary).length
            )
            neighbours.append(
                (
                    round(distance, 9),
                    -round(shared, 9),
                    other_index,
                    other_plane_index,
                )
            )
        if not neighbours:
            raise UnreliableGeometryError(
                "FACET_CONSOLIDATION_SLIVER_UNRESOLVED",
                "A numerical partition sliver could not be joined to a supported facet.",
            )
        piece_assignments[index] = (piece, min(neighbours)[-1])

    regrouped: dict[int, list[BaseGeometry]] = {}
    for piece, plane_index in piece_assignments:
        regrouped.setdefault(plane_index, []).append(piece)
    corrected: list[tuple[Polygon, ConsolidatedPlane]] = []
    for plane_index, pieces in regrouped.items():
        combined = unary_union(pieces)
        merged_pieces = (
            [combined] if combined.geom_type == "Polygon" else list(combined.geoms)
        )
        for piece in merged_pieces:
            if piece.geom_type == "Polygon" and piece.area > 0.25:
                material_holes = [
                    list(ring.coords)
                    for ring in piece.interiors
                    if Polygon(ring).area > 1e-8
                ]
                cleaned = Polygon(list(piece.exterior.coords), material_holes)
                corrected.append((cleaned, planes[plane_index]))
    corrected.sort(
        key=lambda item: (
            round(item[0].centroid.x, 6),
            round(item[0].centroid.y, 6),
            round(item[0].area, 6),
            tuple(round(value, 6) for value in item[1].normal),
        )
    )
    if not corrected:
        raise UnreliableGeometryError(
            "FACET_CONSOLIDATION_OUTPUT_EMPTY",
            "Facet consolidation produced no valid roof surfaces.",
        )
    corrected, final_merge_audit = _merge_adjacent_supported_fragments(
        corrected, consolidation_lidar_points, settings
    )
    corrected.sort(
        key=lambda item: (
            round(item[0].centroid.x, 6),
            round(item[0].centroid.y, 6),
            round(item[1].pitch_degrees, 6),
        )
    )
    manifold_audit = _validate_watertight_partition(corrected, roofprint)

    vertices: list[list[float]] = []
    vertex_ids: dict[tuple[float, float, float], int] = {}
    boundaries: list[list[list[int]]] = []
    surfaces: list[dict[str, Any]] = []
    for polygon, plane in corrected:
        polygon_rings = [polygon.exterior, *polygon.interiors]
        rings: list[list[int]] = []
        for polygon_ring in polygon_rings:
            coordinates = list(polygon_ring.coords)[:-1]
            if len(coordinates) < 3:
                continue
            ring = []
            for x, y in coordinates:
                z = _plane_height(plane, float(x), float(y))
                key = (round(float(x), 8), round(float(y), 8), round(float(z), 8))
                if key not in vertex_ids:
                    vertex_ids[key] = len(vertices)
                    vertices.append(list(key))
                ring.append(vertex_ids[key])
            rings.append(ring)
        if not rings:
            continue
        boundaries.append(rings)
        surfaces.append(
            {
                "type": "RoofSurface",
                "rf_slope": round(plane.pitch_degrees, 6),
                "rf_azimuth": round(plane.azimuth_degrees, 6),
                "rf_consolidated": True,
                "rf_support_points": len(plane.point_indexes),
                "rf_plane_rmse": round(plane.rmse_meters, 6),
            }
        )
    attributes = dict(attributes)
    attributes["rf_facet_consolidation"] = "OPEN3D_CONNECTED_PLANE_AGGLOMERATION"
    # Every ring above is emitted from the single, validated planar
    # arrangement.  Tell the canonical reader not to apply Roofer's broad
    # near-coordinate repair tolerance a second time: doing so can collapse a
    # legitimate short edge at a three-facet junction and fabricate a
    # non-manifold shared edge.
    attributes["rf_exact_planar_arrangement"] = True
    corrected_feature = {
        "type": "CityJSONFeature",
        "id": str(feature.get("id") or "roof"),
        "vertices": vertices,
        "CityObjects": {
            str(feature.get("id") or "roof"): {
                "type": "Building",
                "attributes": attributes,
                "geometry": [
                    {
                        "type": "MultiSurface",
                        "lod": "2.2",
                        "boundaries": boundaries,
                        "semantics": {
                            "surfaces": surfaces,
                            "values": list(range(len(surfaces))),
                        },
                    }
                ],
            }
        },
    }
    audit.update(
        {
            "inputRooferFacetCount": len(roofer_facets),
            "globalRoofComponentCount": len(global_components),
            "correctedFacetCount": len(boundaries),
            "usedConsolidatedPlaneCount": len(regions_by_plane),
            "globalComponentCandidateCounts": candidate_counts,
            "globalComponentPartitionCounts": partition_counts,
            "globalCellAssignments": cell_assignment_audit,
            "assignmentGapMeters": assignment_gap,
            "temporalEvidence": temporal_audit,
            "solarReconciliation": solar_audit,
            "solarDsmReconciliation": dsm_audit,
            "evidenceBasedFinalMerging": final_merge_audit,
            "watertightManifold": manifold_audit,
            "fedToCanonicalTopology": True,
        }
    )
    return corrected_feature, None, audit
