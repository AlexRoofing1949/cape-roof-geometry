"""Extract contractor-facing roof geometry from Roofer CityJSONSequence output.

Roofer labels LoD2.2 faces as RoofSurface, WallSurface, or GroundSurface. This
module uses only RoofSurface topology. It never estimates edge lengths from a
footprint, house area, roof type, or other generic assumption.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import UnreliableGeometryError

METERS_TO_FEET = 3.280839895013123
SQUARE_METERS_TO_SQUARE_FEET = 10.763910416709722


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


def _horizontal_ring_area(points: Iterable[tuple[float, float, float]]) -> float:
    values = list(points)
    return abs(
        sum(
            current[0] * following[1] - following[0] * current[1]
            for current, following in zip(values, values[1:] + values[:1])
        )
    ) / 2


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
    normal_x, normal_y, normal_z = use.facet.normal
    plane_height = use.start[2] - (
        normal_x * (probe_x - use.start[0])
        + normal_y * (probe_y - use.start[1])
    ) / normal_z
    edge_height = _edge_height_at_xy(
        use.start,
        use.end,
        (probe_x, probe_y, plane_height),
    )
    return plane_height - edge_height


def _normal_angle_degrees(first: Facet, second: Facet) -> float:
    cosine = max(-1.0, min(1.0, _dot(first.normal, second.normal)))
    return math.degrees(math.acos(cosine))


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


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
            decoded_rings.append((ring_ids, tuple(vertices[value] for value in ring_ids)))
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
    coplanar_tolerance_degrees: float = 2.0,
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

    edge_uses: dict[tuple[int, int], list[EdgeUse]] = defaultdict(list)
    for facet in facets:
        count = len(facet.vertex_ids)
        for index, start_id in enumerate(facet.vertex_ids):
            end_id = facet.vertex_ids[(index + 1) % count]
            key = tuple(sorted((start_id, end_id)))
            edge_uses[key].append(
                EdgeUse(facet, start_id, end_id, facet.vertices[index], facet.vertices[(index + 1) % count])
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
    counters: dict[str, int] = defaultdict(int)
    edge_prefixes = {
        "rakes": "RA",
        "eaves": "EA",
        "valleys": "VA",
        "ridges": "RI",
        "hips": "HI",
        "highPerimeters": "HP",
    }

    def add_edge(kind: str, uses: list[EdgeUse]) -> None:
        counters[kind] += 1
        first = uses[0]
        entry = {
            "edgeId": f"{edge_prefixes[kind]}{counters[kind]}",
            "lengthFeet": _round(_edge_length(first.start, first.end) * METERS_TO_FEET),
            "facetIds": [use.facet.facet_id for use in uses],
        }
        classified[kind].append(entry)

    for uses in edge_uses.values():
        if len(uses) > 2:
            raise UnreliableGeometryError("NON_MANIFOLD_ROOF_EDGE", "More than two roof facets share a reconstructed edge.")
        first = uses[0]
        if _edge_length(first.start, first.end) <= 0.10:
            continue
        if len(uses) == 1:
            elevation_change = abs(first.start[2] - first.end[2])
            if elevation_change > horizontal_edge_tolerance_meters:
                add_edge("rakes", uses)
                continue
            edge_height = (first.start[2] + first.end[2]) / 2
            if edge_height <= first.facet.centroid[2] + plane_side_tolerance_meters:
                add_edge("eaves", uses)
            else:
                # A horizontal high-side boundary with only one adjacent roof
                # plane is a measured perimeter/flashing edge, not an eave,
                # rake, ridge, hip, or valley. Keep the category explicit for
                # pricing at every pitch instead of guessing a standard type.
                add_edge("highPerimeters", uses)
            continue

        second = uses[1]
        if _normal_angle_degrees(first.facet, second.facet) <= coplanar_tolerance_degrees:
            continue
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
            add_edge("valleys", uses)
        elif decisive_deltas and all(delta < 0 for delta in decisive_deltas):
            if abs(first.start[2] - first.end[2]) <= horizontal_edge_tolerance_meters:
                add_edge("ridges", uses)
            else:
                add_edge("hips", uses)
        else:
            ambiguous.append(
                {
                    "reason": "UNCLASSIFIED_SHARED_EDGE",
                    "facetIds": [first.facet.facet_id, second.facet.facet_id],
                    "sideHeightsMeters": [_round(first_delta, 3), _round(second_delta, 3)],
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
        "internalRoofEdgeFeet": _round(
            totals["ridges"] + totals["hips"] + totals["valleys"]
        ),
        "highPerimeterFeet": totals["highPerimeters"],
        "flatRoofAreaSqFt": _round(flat_area_square_meters * SQUARE_METERS_TO_SQUARE_FEET),
        "roofOpeningCount": sum(facet.opening_count for facet in facets),
        "roofOpeningPerimeterFeet": _round(
            sum(facet.opening_perimeter_meters for facet in facets) * METERS_TO_FEET
        ),
        "confidence": _round(quality_confidence, 3),
        "facets": result_facets,
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
            }
            for facet in facets
        ]
    return result
