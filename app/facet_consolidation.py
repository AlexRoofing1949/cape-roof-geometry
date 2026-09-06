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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from shapely import concave_hull
from shapely import contains_xy
from shapely.geometry import LineString, MultiPoint, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import split, unary_union

from .errors import UnreliableGeometryError


@dataclass(frozen=True)
class ConsolidatedPlane:
    point_indexes: tuple[int, ...]
    normal: tuple[float, float, float]
    centroid: tuple[float, float, float]
    rmse_meters: float
    support_hull: BaseGeometry

    @property
    def pitch_degrees(self) -> float:
        return math.degrees(math.acos(max(-1.0, min(1.0, self.normal[2]))))

    @property
    def azimuth_degrees(self) -> float:
        return math.degrees(
            math.atan2(self.normal[0] / self.normal[2], self.normal[1] / self.normal[2])
        ) % 360


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

    active_indexes = sorted(
        {
            int(relation[index_name])
            for relation in relations
            for index_name in ("left", "right")
        }
    )
    halfspace_regions: list[tuple[BaseGeometry, ConsolidatedPlane]] = []
    for index in active_indexes:
        region: BaseGeometry = polygon
        for relation in relations:
            if index == relation["left"]:
                other_index = int(relation["right"])
            elif index == relation["right"]:
                other_index = int(relation["left"])
            else:
                continue
            region = _clip_to_plane_side(
                region, candidates[index], candidates[other_index]
            )
            if region.is_empty:
                break
        if not region.is_empty and region.area > 0.25:
            halfspace_regions.append((region, candidates[index]))
    halfspace_union = (
        unary_union([region for region, _ in halfspace_regions])
        if halfspace_regions
        else Polygon()
    )
    halfspace_coverage = float(halfspace_union.intersection(polygon).area) / max(
        float(polygon.area), 0.01
    )
    halfspace_summed = sum(float(region.area) for region, _ in halfspace_regions)
    halfspace_overlap = max(
        0.0, halfspace_summed - float(halfspace_union.area)
    ) / max(float(polygon.area), 0.01)
    if halfspace_coverage >= 0.995 and halfspace_overlap <= 0.005:
        return halfspace_regions

    cells: list[BaseGeometry] = [polygon]
    for line in lines:
        next_cells: list[BaseGeometry] = []
        for cell in cells:
            try:
                pieces = split(cell, line)
            except (ValueError, TypeError) as error:
                raise UnreliableGeometryError(
                    "FACET_CONSOLIDATION_SPLIT_INVALID",
                    "A supported plane intersection could not split the roof coverage.",
                ) from error
            next_cells.extend(
                piece
                for piece in pieces.geoms
                if not piece.is_empty and piece.area > 1e-8
            )
        cells = next_cells

    cell_assignments: list[tuple[BaseGeometry, int]] = []
    for cell in cells:
        probe = cell.representative_point()
        rankings = []
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
            rankings.append(
                (
                    mismatches,
                    round(float(candidate.support_hull.distance(probe)), 9),
                    -round(float(candidate.support_hull.intersection(cell).area), 9),
                    -len(candidate.point_indexes),
                    index,
                )
            )
        winner = min(rankings)[-1]
        cell_assignments.append((cell, winner))

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


def _solar_plane_audit(
    planes: list[ConsolidatedPlane], solar_reference: Any, settings: Any
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
    if rejected_support_fraction > maximum_rejected_support_fraction:
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
    return accepted, {
        "solarFacetCount": len(solar_facets),
        "reconstructedPlaneCount": len(planes),
        "acceptedPlaneCount": len(accepted),
        "rejectedNoisePlaneCount": len(rejected),
        "rejectedNoiseSupportFraction": round(rejected_support_fraction, 4),
        "matches": matches,
        "rejectedNoisePlanes": rejected,
    }


def validate_solar_dsm_support(
    planes: list[ConsolidatedPlane],
    dsm_path: Path,
    input_crs: str,
    settings: Any,
) -> dict[str, Any]:
    """Require the dated Google Solar DSM to support the consolidated surface shape."""

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
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise UnreliableGeometryError(
            "SOLAR_DSM_GEOREFERENCE_INVALID",
            "The Google Solar DSM georeference could not be reconciled with LiDAR.",
        ) from error

    proposed: list[tuple[int, float, float, float]] = []
    for plane_index, plane in enumerate(planes, start=1):
        coordinates = list(getattr(plane.support_hull.exterior, "coords", []))[:-1]
        if coordinates:
            stride = max(1, math.ceil(len(coordinates) / 12))
            coordinates = coordinates[::stride][:12]
        center = plane.support_hull.representative_point()
        coordinates.append((center.x, center.y))
        for x, y in coordinates:
            proposed.append(
                (plane_index, float(x), float(y), _plane_height(plane, float(x), float(y)))
            )

    samples: list[tuple[int, float]] = []
    per_plane_counts: dict[int, int] = {}
    for plane_index, x, y, lidar_height in proposed:
        try:
            target_x, target_y, _ = transformer.TransformPoint(x, y)
            pixel_x, pixel_y = gdal.ApplyGeoTransform(inverse, target_x, target_y)
            column, row = int(math.floor(pixel_x)), int(math.floor(pixel_y))
            if not (0 <= column < dataset.RasterXSize and 0 <= row < dataset.RasterYSize):
                continue
            value = band.ReadAsArray(column, row, 1, 1)
            if value is None or value.size != 1:
                continue
            dsm_height = float(value[0, 0])
            if not math.isfinite(dsm_height):
                continue
            if nodata is not None and math.isclose(dsm_height, float(nodata), abs_tol=1e-6):
                continue
        except (RuntimeError, TypeError, ValueError, OverflowError):
            continue
        samples.append((plane_index, dsm_height - lidar_height))
        per_plane_counts[plane_index] = per_plane_counts.get(plane_index, 0) + 1

    coverage = len(samples) / max(len(proposed), 1)
    minimum_coverage = float(
        getattr(settings, "solar_dsm_minimum_sample_coverage", 0.80)
    )
    supported_planes = len(per_plane_counts)
    if coverage < minimum_coverage or supported_planes != len(planes):
        raise UnreliableGeometryError(
            "SOLAR_DSM_COVERAGE_INSUFFICIENT",
            "The Google Solar DSM does not cover every consolidated facet.",
            details={
                "sampleCoverage": round(coverage, 4),
                "minimumSampleCoverage": minimum_coverage,
                "supportedFacetCount": supported_planes,
                "facetCount": len(planes),
            },
        )
    offsets = np.asarray([offset for _, offset in samples], dtype=float)
    vertical_offset = float(np.median(offsets))
    centered = offsets - vertical_offset
    rmse = float(np.sqrt(np.mean(np.square(centered))))
    maximum_rmse = float(
        getattr(settings, "solar_dsm_maximum_centered_rmse_meters", 0.75)
    )
    if rmse > maximum_rmse:
        raise UnreliableGeometryError(
            "SOLAR_DSM_SHAPE_CONFLICT",
            "Google Solar DSM elevations disagree with the consolidated LiDAR roof shape.",
            details={
                "centeredRmseMeters": round(rmse, 4),
                "maximumCenteredRmseMeters": maximum_rmse,
                "sampleCount": len(samples),
            },
        )
    return {
        "validation": "PASSED",
        "sampleCount": len(samples),
        "sampleCoverage": round(coverage, 4),
        "supportedFacetCount": supported_planes,
        "centeredRmseMeters": round(rmse, 4),
        "verticalDatumOffsetMeters": round(vertical_offset, 4),
    }


def consolidate_roofer_feature(
    feature: dict[str, Any],
    transform: dict[str, Any] | None,
    points: np.ndarray,
    solar_reference: Any,
    settings: Any,
    *,
    solar_dsm_path: Path | None = None,
    projected_crs: str | None = None,
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
    planes, audit = discover_consolidated_planes(points, roofprint, settings)
    planes, solar_audit = _solar_plane_audit(planes, solar_reference, settings)
    dsm_audit: dict[str, Any] | None = None
    if getattr(settings, "solar_dsm_enabled", False):
        if solar_dsm_path is None or projected_crs is None:
            raise UnreliableGeometryError(
                "SOLAR_DSM_EVIDENCE_MISSING",
                "Facet consolidation requires the dated Google Solar DSM evidence.",
            )
        dsm_audit = validate_solar_dsm_support(
            planes, solar_dsm_path, projected_crs, settings
        )
    plane_indexes = {id(plane): index for index, plane in enumerate(planes)}

    regions_by_plane: dict[int, list[BaseGeometry]] = {}
    supported_roofer_facets = 0
    nearest_plane_fallback_count = 0
    candidate_counts: list[int] = []
    partition_counts: list[int] = []
    assignment_gap = float(
        getattr(settings, "facet_consolidation_assignment_gap_meters", 1.0)
    )
    for polygon, roofer_facet in zip(roofer_polygons, roofer_facets):
        ranked: list[tuple[Any, ...]] = []
        for plane_index, plane in enumerate(planes):
            overlap_area = float(plane.support_hull.intersection(polygon.buffer(0.05)).area)
            hull_distance = float(plane.support_hull.distance(polygon))
            if overlap_area <= 0.05 and hull_distance > assignment_gap:
                continue
            ranked.append(
                (
                    0 if overlap_area > 0.05 else 1,
                    round(hull_distance, 9),
                    -round(overlap_area, 9),
                    -len(plane.point_indexes),
                    plane_index,
                    plane,
                )
            )
        ranked.sort(key=lambda item: item[:-1])
        candidates = [item[-1] for item in ranked]
        if not candidates:
            nearby = []
            original_normal = np.asarray(roofer_facet.normal)
            centroid = roofer_facet.centroid
            for plane_index, plane in enumerate(planes):
                angle = _normal_angle_degrees(original_normal, np.asarray(plane.normal))
                elevation_distance = abs(
                    _plane_height(plane, centroid[0], centroid[1]) - centroid[2]
                )
                hull_distance = float(plane.support_hull.distance(polygon))
                if angle <= 5.0 and elevation_distance <= 0.60 and hull_distance <= 2.0:
                    nearby.append(
                        (
                            round(elevation_distance, 9),
                            round(angle, 9),
                            round(hull_distance, 9),
                            -len(plane.point_indexes),
                            plane_index,
                            plane,
                        )
                    )
            if not nearby:
                raise UnreliableGeometryError(
                    "FACET_CONSOLIDATION_FACET_UNSUPPORTED",
                    "A Roofer facet has no independent plane support.",
                )
            candidates = [min(nearby)[-1]]
            nearest_plane_fallback_count += 1
        # Sparse normalized returns often stop just short of a real ridge,
        # hip, or valley.  Retain both material overlaps and spatially
        # adjacent support so the fitted 3-D planes can split a coarse Roofer
        # face at their actual intersection.  The partitioner still rejects a
        # pair whose equations do not meet inside this face; proximity never
        # invents an edge by itself.
        material = [
            plane
            for plane in candidates
            if float(plane.support_hull.intersection(polygon).area)
            / max(float(polygon.area), 0.01)
            >= 0.02
            or float(plane.support_hull.distance(polygon)) <= assignment_gap
        ]
        candidates = material or candidates[:1]
        candidates = candidates[:12]
        candidate_counts.append(len(candidates))
        partitions = _partition_roofer_facet(
            polygon, candidates, adjacency_gap_meters=assignment_gap
        )
        partition_counts.append(len(partitions))
        for region, plane in partitions:
            plane_index = plane_indexes[id(plane)]
            regions_by_plane.setdefault(plane_index, []).append(region)
        supported_roofer_facets += 1

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

    # A sliver can survive at a Roofer-face boundary even after the local
    # arrangement cleanup.  Resolve it against the complete roof partition so
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
                corrected.append((piece, planes[plane_index]))
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

    vertices: list[list[float]] = []
    vertex_ids: dict[tuple[float, float, float], int] = {}
    boundaries: list[list[list[int]]] = []
    surfaces: list[dict[str, Any]] = []
    for polygon, plane in corrected:
        coordinates = list(polygon.exterior.coords)[:-1]
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
        boundaries.append([ring])
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
            "supportedRooferFacetCount": supported_roofer_facets,
            "nearestSupportedPlaneFallbackCount": nearest_plane_fallback_count,
            "correctedFacetCount": len(boundaries),
            "usedConsolidatedPlaneCount": len(regions_by_plane),
            "rooferFacetCandidateCounts": candidate_counts,
            "rooferFacetPartitionCounts": partition_counts,
            "assignmentGapMeters": assignment_gap,
            "solarReconciliation": solar_audit,
            "solarDsmReconciliation": dsm_audit,
            "fedToCanonicalTopology": True,
        }
    )
    return corrected_feature, None, audit
