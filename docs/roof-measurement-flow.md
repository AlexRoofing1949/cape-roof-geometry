# Roof measurement and calculation flow

## Scope and status

This document separates the deployed calculation path from proposed reliability changes. The live `cape-roof-geometry` health endpoint reported commit `7f51be4b22f0c4783b5cfbbeb0c9a3164467c638` on 2026-10-09. The formulas below describe that production code unless a candidate difference is called out. The candidate changes described below are not deployed.

Main source references:

- [`app/pipeline.py`](https://github.com/AlexRoofing1949/cape-roof-geometry/blob/7f51be4b22f0c4783b5cfbbeb0c9a3164467c638/app/pipeline.py)
- [`app/providers.py`](https://github.com/AlexRoofing1949/cape-roof-geometry/blob/7f51be4b22f0c4783b5cfbbeb0c9a3164467c638/app/providers.py)
- [`app/cityjson_geometry.py`](https://github.com/AlexRoofing1949/cape-roof-geometry/blob/7f51be4b22f0c4783b5cfbbeb0c9a3164467c638/app/cityjson_geometry.py)

## End-to-end request path

1. **Validate the request location.** `reconstruct_roof` rejects coordinates outside the configured service area before starting reconstruction.
2. **Resolve the building and roofprint.** The provider layer selects corroborated footprint evidence. When authorized Google Solar roof-mask data is available, the service polygonizes it into the selected roofprint. After edge classification, it compares the projected total eave-plus-rake perimeter with that roofprint perimeter.
3. **Select registered LiDAR.** `select_regional_lidar` uses the county and regional source registry to identify eligible datasets. It ranks candidates using acquisition-date and buffered-footprint coverage rules. It attempts eligible sources in order so a bad crop can fall through to another source.
4. **Prepare a projected crop.** The footprint and roofprint are written in the target projected CRS. PDAL crops/reprojects the selected point cloud and applies the class allowlist recorded for that source. The pipeline records a classification audit and computes usable-point density against the projected footprint area. It rejects a source that misses the configured minimum density, including any higher source-specific minimum.
5. **Reconstruct roof planes.** The deployed `main` revision pins Roofer `v1.0.0`; the unmerged reliability candidate pins `v1.1.0-beta.1`. `_run_roofer` invokes `roofer --id-attribute request_id --split-cjseq --lod22 --plane-detect-k 15 --plane-detect-min-points 15 --plane-detect-epsilon 0.15 --complexity-factor 0.95` followed by the point-cloud, projected-footprint, and output paths. The plane-detection values are environment-overridable; the command timeout defaults to 240 s. The service accepts exactly one CityJSON feature for the property. Missing or invalid Roofer output is an error; the engine does not substitute a guessed roof.
6. **Read roof facets from CityJSON.** `_roof_facets` selects semantic `RoofSurface` faces from LoD2.2 `Solid`, `MultiSurface`, or `CompositeSurface` geometry. It decodes the CityJSON transform, canonicalizes ring orientation/start points, and subtracts any opening rings from the face measurements.
7. **Validate evidence and topology.** Facet density, no-data fraction, and reconstruction RMSE are checked. The engine nodes facet edges, classifies boundaries, reconciles roof area and pitch with the Solar reference, validates the selected roof type, and applies current-imagery/pricing gates. An ambiguous or materially inconsistent result fails closed.

## Facet measurements and formulas

For each exterior roof ring, `_newell` first translates the vertices to a local origin to reduce floating-point cancellation at large UTM coordinates. It calculates the Newell area vector `v`. The net 3D area is `|v| / 2`, less the 3D areas of any opening rings. Horizontal area is the XY shoelace area of the exterior ring, less the XY areas of openings.

The unit facet normal is `n = v / |v|`, flipped upward when needed. A vertical face (`n_z` effectively zero) cannot be priced and is rejected. Pitch and rise are:

```text
pitch_degrees = acos(n_z) × 180 / π
pitch_rise_per_12 = 12 × tan(pitch_degrees × π / 180)
```

Azimuth comes from the horizontal components of the plane normal. The engine also independently checks the projection identity:

```text
3D facet area ≈ horizontal facet area / cos(pitch)
```

Production rejects a facet when the relative error exceeds `0.000001%`. This is a numerical consistency check, not an external accuracy claim. Total roof area is the sum of net 3D facet areas, converted from square metres to square feet with `10.763910416709722`. Average pitch is weighted by each facet's 3D area; maximum pitch is the largest facet pitch. Flat-roof area sums facets at or below the configured flat-pitch threshold (default 5°).

Facet count is the number of accepted semantic roof faces after the applicable facet processing. In the broader deterministic candidate, Open3D-supported plane consolidation merges fragments belonging to one plane and splits faces when the LiDAR supports distinct planes; that processing is not present in the deployed commit.

## Roof line measurements

Facet boundaries are noded geometrically so adjacent planes can share an edge even when Roofer emits different vertex IDs or a T-junction. Deployed `EDGE_NODE_TOLERANCE_METERS` defaults to 0.10 m, and `_edge_parameter` checks full 3D point-to-segment distance. The focused candidate instead uses 0.10 m plan tolerance and 0.30 m vertical tolerance. In a direct reproduction, production rejects a node at `(5,0.05,1.24)` on the edge `(0,0,0)`–`(10,0,2)`: its plan offset is 0.05 m, but its elevation residual is 0.24 m. The candidate accepts it at edge parameter 0.5; a point with 0.80 m elevation separation remains rejected. This proves a defect class in deployed code, not the cause of a specific customer roof.

Each edge stores both 3D length and projected XY length. Deployed edge categories are derived as follows:

- **Exterior boundaries:** if the endpoint elevation change exceeds 0.15 m, the single-use boundary is a rake. Otherwise, the mean boundary elevation is compared with its facet centroid: at or below centroid elevation plus 0.08 m is an eave; above that is a separate high-perimeter/flashing edge.
- **Shared boundaries:** more than two adjacent facets fail as non-manifold; coplanar neighbors within 2° do not produce a roof line. For each remaining shared edge, the engine probes the adjacent facet planes one metre inward and treats a side-height change over 0.08 m as decisive. Consistently positive decisive changes classify a valley. Consistently negative changes classify a convex junction: an edge with no more than 0.15 m endpoint elevation change is a ridge; otherwise it is a hip. Conflicting or inconclusive changes fail as unclassified.
- **High perimeter:** it is measured separately and excluded from the eave/rake exterior-perimeter total.

After classification, `_enforce_roofprint_perimeter_consistency` compares the projected length of `eaves + rakes` with the selected roofprint perimeter. Missing lengths fail; the default maximum variance is 10%. This is an aggregate topology check. Deployed main does not individually corroborate each eave or rake against the roofprint boundary.

The output exterior perimeter is `eaves + rakes`; internal roof edge length is `ridges + hips + valleys`. Opening count and perimeter are reported separately.

## Reconciliation and error behavior

The reconstructed roof area and area-weighted pitch are compared with the request's Solar reference (production defaults: at most 15% area variance and 10° pitch variance). Other production defaults include point density of at least max(8 points/m², the source-specific minimum), LiDAR coverage of at least 0.98, no-data fraction at most 0.10, Roofer RMSE at most 0.35 m, and combined confidence of at least 0.80. These values can be overridden by service configuration; the response audit reports the active geometry quality thresholds and measured reconciliation variances. The projected roofprint perimeter must also be within 10% of the reconstructed eave-plus-rake perimeter by default. Pricing remains disabled when required current-imagery or facet-calibration gates are not approved.

`NO_RELIABLE_LIDAR_GEOMETRY` is an aggregate source-selection failure: eligible LiDAR crops were tried, but each failed for a reason other than the case where all failures are only `NO_USABLE_ROOF_RETURNS` or `LIDAR_CROP_EMPTY`. The `details.selectionAttempts` field is the evidence that distinguishes source coverage, density, point classification, Roofer, facet, and topology failures. Other geometry failures include invalid/missing CityJSON roof surfaces, area/pitch formula disagreement, incomplete edge classification, non-manifold topology, Solar area/pitch disagreement, and insufficient reconstruction quality. These codes indicate why a measurement was withheld; they should not be worked around by estimating missing dimensions.

### Reading source-attempt failures

For each rejected LiDAR source, production appends `sourceId`, `decision`, and `errorCode`; it adds `errorDetails` when the underlying geometry exception has details. If no source succeeds, the aggregate is `NO_USABLE_ROOF_RETURNS` only when every recorded failure is `NO_USABLE_ROOF_RETURNS` or `LIDAR_CROP_EMPTY`. Any other failure, or no usable candidate attempt, produces `NO_RELIABLE_LIDAR_GEOMETRY`. This makes the per-source `details.selectionAttempts` array the authoritative diagnostic for that aggregate. In the deployed API, `requestId` is present in the request payload and logs but is not echoed by the response. The current candidate adds it to failure JSON and returns it as `X-Request-ID` on every response.

Representative failures in deployed commit `7f51be4b22f0c4783b5cfbbeb0c9a3164467c638`:

| Stage | Representative code | Meaning |
| --- | --- | --- |
| Footprint selection | `BUILDING_FOOTPRINT_NOT_FOUND`, `BUILDING_FOOTPRINT_AMBIGUOUS`, `FOOTPRINT_PROVIDER_CONFLICT` | No unique, sufficiently corroborated building footprint was selected. |
| LiDAR crop and classes | `LIDAR_CROP_EMPTY`, `NO_USABLE_ROOF_RETURNS`, `LIDAR_CLASSIFICATION_AUDIT_MISSING`, `LIDAR_TILE_BEFORE_MINIMUM_ACQUISITION_DATE` | The crop is empty, lacks allowed roof-return classes, lacks a trustworthy class audit, or is too old. |
| Point-cloud quality | `LIDAR_DENSITY_TOO_LOW`, `LIDAR_COVERAGE_INCOMPLETE`, `ROOFER_RMSE_TOO_HIGH` | The cloud does not satisfy configured density, roof coverage, or reconstruction-error requirements. |
| Roofer and CityJSON | `GEOMETRY_COMMAND_FAILED`, `ROOFER_OUTPUT_COUNT_INVALID`, `CITYJSON_TRANSFORM_INVALID`, `CITYJSON_SEMANTICS_INVALID`, `ROOF_FACETS_MISSING`, `ROOFER_FALLBACK_MODEL` | Reconstruction did not produce one valid, semantic LoD2.2 roof model. |
| Facet calculations | `ROOF_FACET_INVALID`, `ROOF_FACET_VERTICAL`, `ROOF_AREA_FORMULA_MISMATCH`, `ROOF_PITCH_SEMANTICS_MISMATCH` | A facet is malformed, not safely measurable, or its independently derived area/pitch checks disagree. |
| Edges and perimeter | `NON_MANIFOLD_ROOF_EDGE`, `ROOF_EDGE_CLASSIFICATION_INCOMPLETE`, `ROOF_TOPOLOGY_PERIMETER_MISSING`, `ROOF_TOPOLOGY_PERIMETER_MISMATCH` | The facets do not form classifiable, price-safe roof-line topology or do not reconcile to the exterior boundary. |
| Cross-checks and confidence | `SOLAR_AREA_RECONCILIATION_FAILED`, `SOLAR_PITCH_RECONCILIATION_FAILED`, `SELECTED_FLAT_ROOF_MISMATCH`, `SELECTED_STEEP_ROOF_MISMATCH`, `GEOMETRY_CONFIDENCE_TOO_LOW` | The reconstruction disagrees with independent area/pitch or selected-system evidence, or falls below the confidence threshold. |
| Temporary provider/runtime issue | `GEOMETRY_COMMAND_TIMEOUT`, `OPEN_DATA_COMMAND_TIMEOUT`, `PROVIDER_NETWORK_ERROR` | A command or upstream provider timed out/failed. These are transient errors and may be returned outside `selectionAttempts`. |

The list is representative, not exhaustive. A failure code is a reason the service withheld automatic measurements; it is not evidence that the roof has no roof geometry.

## Reliability candidates not yet deployed

- The focused edge candidate uses separate XY and vertical noding tolerances and adds tests for small vertical residuals and genuinely separated roof levels.
- The broader candidate additionally canonicalizes PDAL point ordering, consolidates LiDAR-supported facets, validates Solar DSM shape, checks per-edge boundaries against the roofprint within 0.50 m, uses a local 0.75 m roofprint tangent, and derives shared-edge corrections from intersecting facet planes.
- The Roofer `1.1.0-beta.1` candidate removes time-limited reconstruction decisions and retains Roofer's default parallel reconstruction. Because it is a pre-release, it remains zero-traffic until real-roof golden comparisons and full-container replay pass.

Local verification on 2026-10-09 using Python 3.12 and the pinned FastAPI, Shapely, PyProj, and Open3D versions: deployed baseline, 61 passed; broader candidate, 111 passed and 1 skipped. The skipped test requires GDAL, which is unavailable in this Windows environment. Docker/PDAL/GDAL full-container replay and production deployment remain unverified; these unit tests do not prove that a particular customer roof is fixed.

