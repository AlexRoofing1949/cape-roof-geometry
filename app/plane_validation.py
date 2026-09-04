"""Independent Open3D validation of Roofer roof planes."""

from __future__ import annotations

import importlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from shapely import contains_xy
from shapely.geometry import Polygon

from .errors import TransientProviderError, UnreliableGeometryError


def _open3d() -> Any:
    try:
        return importlib.import_module("open3d")
    except (ImportError, OSError) as error:
        raise UnreliableGeometryError(
            "OPEN3D_RUNTIME_MISSING", "The independent roof-plane validator is unavailable."
        ) from error


def _unit(vector: np.ndarray) -> np.ndarray:
    length = float(np.linalg.norm(vector))
    if length <= 1e-12:
        raise UnreliableGeometryError(
            "OPEN3D_PLANE_INVALID", "Open3D returned a degenerate roof-plane normal."
        )
    result = vector / length
    return -result if result[2] < 0 else result


def validate_facet_points(
    points: np.ndarray, facet_models: list[dict[str, Any]], settings: Any
) -> dict[str, Any]:
    """Fit each Roofer facet again and require agreement before pricing."""

    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise UnreliableGeometryError(
            "OPEN3D_POINT_CLOUD_INVALID", "The independent plane validator received invalid points."
        )
    results: list[dict[str, Any]] = []
    o3d = _open3d()
    o3d.utility.random.seed(1949)
    prepared_facets: list[dict[str, Any]] = []
    for facet in facet_models:
        facet_id = str(facet.get("facetId") or "")
        vertices = facet.get("verticesMeters") or []
        polygon = Polygon([(float(vertex[0]), float(vertex[1])) for vertex in vertices])
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0.05:
            raise UnreliableGeometryError(
                "OPEN3D_FACET_FOOTPRINT_INVALID",
                "A Roofer facet cannot be independently validated in plan view.",
                details={"facetId": facet_id},
            )
        roofer_normal = _unit(np.asarray(facet.get("normal") or [], dtype=float))
        prepared_facets.append(
            {
                "facet": facet,
                "facetId": facet_id,
                "polygon": polygon.buffer(0.02),
                "normal": roofer_normal,
                "origin": np.asarray(vertices[0], dtype=float),
            }
        )

    # LoD2.2 building parts can contain roof faces that overlap in plan view.
    # Assign each return to the eligible 3D facet plane it is nearest to before
    # RANSAC; otherwise an upper face can make a lower face appear to slope in
    # the opposite direction even though both planes have valid support.
    candidate_distances = np.full((len(prepared_facets), len(points)), np.inf, dtype=float)
    plan_view_candidate_counts: list[int] = []
    for index, prepared in enumerate(prepared_facets):
        mask = contains_xy(prepared["polygon"], points[:, 0], points[:, 1])
        plan_view_candidate_counts.append(int(np.count_nonzero(mask)))
        candidate_distances[index, mask] = np.abs(
            (points[mask] - prepared["origin"]) @ prepared["normal"]
        )
    closest_facets = np.argmin(candidate_distances, axis=0)
    closest_distances = np.min(candidate_distances, axis=0)
    has_candidate = np.isfinite(closest_distances) & (
        closest_distances <= settings.open3d_maximum_assignment_distance_meters
    )

    for facet_index, prepared in enumerate(prepared_facets):
        facet = prepared["facet"]
        facet_id = prepared["facetId"]
        # Include points on reconstructed boundaries without allowing material
        # spillover from an adjacent or vertically overlapping roof plane.
        selected = points[has_candidate & (closest_facets == facet_index)]
        assigned_before_distance_gate = int(
            np.count_nonzero(np.isfinite(closest_distances) & (closest_facets == facet_index))
        )
        if len(selected) < settings.open3d_minimum_facet_points:
            raise UnreliableGeometryError(
                "OPEN3D_FACET_SUPPORT_INSUFFICIENT",
                "Too few roof points support a reconstructed facet.",
                details={
                    "facetId": facet_id,
                    "facetAreaSqFt": round(float(facet.get("areaSqFt") or 0), 2),
                    "reconstructedPitchDegrees": round(
                        float(facet.get("pitchDegrees") or 0), 3
                    ),
                    "planViewCandidatePoints": plan_view_candidate_counts[facet_index],
                    "supportPoints": int(len(selected)),
                    "discardedBeyondPlaneDistance": assigned_before_distance_gate - int(len(selected)),
                    "maximumAssignmentDistanceMeters": (
                        settings.open3d_maximum_assignment_distance_meters
                    ),
                    "minimumSupportPoints": settings.open3d_minimum_facet_points,
                },
            )

        # Southwest Florida projected coordinates are hundreds of thousands of
        # metres east and millions of metres north.  Feeding those absolute
        # values directly to RANSAC can make an otherwise ordinary residential
        # facet numerically ill-conditioned and produce a spurious vertical
        # plane with every point reported as a zero-error inlier.  Translation
        # does not change a plane normal, so fit in a local centroid frame.
        local_origin = np.mean(selected, axis=0)
        centered = selected - local_origin
        axis_spans = np.ptp(centered, axis=0)
        singular_values = np.linalg.svd(centered, compute_uv=False)
        cloud = o3d.geometry.PointCloud()
        cloud.points = o3d.utility.Vector3dVector(centered)
        plane_model, inlier_indexes = cloud.segment_plane(
            distance_threshold=settings.open3d_distance_threshold_meters,
            ransac_n=3,
            num_iterations=settings.open3d_ransac_iterations,
            probability=0.999,
        )
        coefficients = np.asarray(plane_model, dtype=float)
        fitted_normal = _unit(coefficients[:3])
        roofer_normal = prepared["normal"]
        normal_angle = math.degrees(
            math.acos(float(np.clip(np.dot(fitted_normal, roofer_normal), -1.0, 1.0)))
        )
        fitted_pitch = math.degrees(
            math.acos(float(np.clip(fitted_normal[2], -1.0, 1.0)))
        )
        inliers = centered[np.asarray(inlier_indexes, dtype=int)]
        inlier_ratio = len(inliers) / len(selected)
        denominator = float(np.linalg.norm(coefficients[:3]))
        distances = np.abs(inliers @ coefficients[:3] + coefficients[3]) / denominator
        rmse = float(np.sqrt(np.mean(np.square(distances)))) if len(distances) else math.inf

        failures: list[str] = []
        if inlier_ratio < settings.open3d_minimum_inlier_ratio:
            failures.append("PLANE_INLIER_RATIO_TOO_LOW")
        if normal_angle > settings.open3d_maximum_normal_variance_degrees:
            failures.append("PLANE_NORMAL_DISAGREEMENT")
        if rmse > settings.open3d_maximum_plane_rmse_meters:
            failures.append("PLANE_RMSE_TOO_HIGH")
        result = {
            "facetId": facet_id,
            "facetAreaSqFt": round(float(facet.get("areaSqFt") or 0), 2),
            "reconstructedPitchDegrees": round(
                float(facet.get("pitchDegrees") or 0), 3
            ),
            "fittedPitchDegrees": round(fitted_pitch, 3),
            "fittedNormal": [round(float(value), 6) for value in fitted_normal],
            "pointAxisSpanMeters": {
                axis: round(float(axis_spans[index]), 4)
                for index, axis in enumerate(("x", "y", "z"))
            },
            "pointCloudSingularValues": [
                round(float(value), 4) for value in singular_values
            ],
            "planViewCandidatePoints": plan_view_candidate_counts[facet_index],
            "supportPoints": int(len(selected)),
            "discardedBeyondPlaneDistance": assigned_before_distance_gate - int(len(selected)),
            "maximumAssignmentDistanceMeters": settings.open3d_maximum_assignment_distance_meters,
            "inlierPoints": int(len(inliers)),
            "inlierRatio": round(inlier_ratio, 4),
            "normalVarianceDegrees": round(normal_angle, 3),
            "rmseMeters": round(rmse, 4),
            "validation": "FAILED" if failures else "PASSED",
            "failures": failures,
        }
        results.append(result)
        if failures:
            raise UnreliableGeometryError(
                "OPEN3D_PLANE_VALIDATION_FAILED",
                "Independent point-cloud fitting disagrees with a reconstructed roof facet.",
                details=result,
            )

    return {
        "provider": "Open3D",
        "version": o3d.__version__,
        "validation": "PASSED",
        "facetCount": len(results),
        "facets": results,
    }


def validate_roofer_planes(
    pointcloud_path: Path,
    facet_models: list[dict[str, Any]],
    workspace: Path,
    settings: Any,
) -> dict[str, Any]:
    """Export normalized roof XYZ values and independently validate every Roofer facet."""

    o3d = _open3d()
    if o3d.__version__ != settings.open3d_version:
        raise UnreliableGeometryError(
            "OPEN3D_VERSION_MISMATCH",
            "The installed independent plane validator does not match the pinned production version.",
            details={"configuredVersion": settings.open3d_version, "installedVersion": o3d.__version__},
        )
    xyz_path = workspace / "open3d-roof-points.csv"
    pipeline_path = workspace / "open3d-points-pipeline.json"
    pipeline_path.write_text(
        json.dumps(
            {
                "pipeline": [
                    str(pointcloud_path),
                    {"type": "filters.expression", "expression": "Classification == 6"},
                    # Class-1 sources have already passed the production HAG,
                    # clustering, surface-normal and curvature pipeline before
                    # being normalized to class 6. Repeating k-neighbour
                    # filters here can erase legitimate small facets because
                    # their neighbourhood crosses a hip or valley. Export the
                    # normalized roof returns unchanged and let Open3D RANSAC,
                    # inlier-ratio, normal-agreement and RMSE gates perform the
                    # independent validation on the complete evidence set.
                    {
                        "type": "writers.text",
                        "filename": str(xyz_path),
                        "format": "csv",
                        "order": "X:8,Y:8,Z:8",
                        "keep_unspecified": False,
                        "write_header": True,
                    },
                ]
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["pdal", "pipeline", str(pipeline_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.command_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        raise TransientProviderError(
            "OPEN3D_POINT_EXPORT_FAILED",
            "The independent roof-plane validation input could not be prepared.",
        ) from error
    try:
        points = np.loadtxt(xyz_path, delimiter=",", skiprows=1, dtype=float)
    except (OSError, ValueError) as error:
        raise UnreliableGeometryError(
            "OPEN3D_POINT_CLOUD_INVALID",
            "The independent plane validator could not read exact XYZ roof returns.",
        ) from error
    if points.ndim == 1 and points.size == 3:
        points = points.reshape(1, 3)
    if len(points) == 0:
        raise UnreliableGeometryError(
            "OPEN3D_POINT_CLOUD_EMPTY", "No normalized roof returns were available for independent validation."
        )
    return validate_facet_points(points, facet_models, settings)
