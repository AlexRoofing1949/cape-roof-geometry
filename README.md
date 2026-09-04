# Cape Coral Roofing open geometry endpoint

This directory supplies the missing `POST /v1/roof-geometry` service already expected by `automation/google-apps-script/GeometryService.gs`. It does not replace or duplicate the existing form, Sheets, Drive, pricing, PDF, email, trigger, retry, or audit code.

## Production stack

1. The building-footprint resolver first uses the official Overture Maps client against a pinned open Buildings release. If that source has no usable coverage, it falls back in order to the pinned Microsoft GlobalML Building Footprints release, an explicitly authorized Lee County building layer, and a cached OpenStreetMap Overpass response. Overture, Microsoft and OSM are one correlated open-map lineage rather than three votes. Calibrated building consensus requires IoU at least 0.70, centroid separation no more than 4 m and area difference no more than 16%. Material or correlated-source disagreement fails closed.
2. When enabled with a Secret Manager-backed Solar credential, the service obtains the authorized high-quality Google Solar 0.1 m building-mask GeoTIFF and polygonizes the selected rooftop. It removes raster stair steps with a topology-preserving 0.25 m simplification and fails closed if that changes ground area by more than 2%. The resulting roof mask must match Solar's independent facet ground-area total within the production-calibrated 8% threshold and remain spatially consistent with the corroborated building footprint. This roof-specific outline drives the LiDAR crop and Roofer reconstruction; the broader building polygon is retained separately for current-structure/change validation. The API key and temporary signed raster URL are never returned or logged.
3. The service resolves the rooftop against the official Census county layer and the eight-county Southwest Florida registry. It ranks registered Manatee 2025, NOAA pre-/post-Ian 2022, USGS/NRCS Southwest Florida 2018–2019, and Florida Peninsular 2018–2020 candidates by the registered acquisition window and complete buffered-roofprint coverage. The Southwest registry matches all three published A, B, and B_TL EPT deliveries so both Lee and Collier are covered; its official project window is May 8, 2018 through March 1, 2019. It always tries the newest valid candidate first and rejects both registered sources and exact property-crop dates earlier than the fixed `2018-01-01` acquisition floor. Lee 2026 remains disabled until its files, footprint and reuse terms are verified.
4. PDAL streams only the selected buffered rooftop crop. Because EPT nodes are fetched concurrently, PDAL applies a canonical XY Morton order before any neighbourhood, clustering, or normal calculation so Roofer receives a canonical point sequence. Allowed surface, ground and building classes come from the selected source record, so NOAA class-1 surface returns are not discarded and bathymetric-only/noise classes are not accepted as roof evidence. The enumerated class histogram, ordering method, and exact pipeline are retained in the audit response.
5. 3DBAG Roofer v1.0.0 reconstructs LoD2.2 roof planes from the classified point cloud and the selected roofprint. Cape Roof runs it with one worker job so identical inputs are not reordered by concurrent reconstruction, sets Roofer's plane-detection inlier distance to 0.15 m instead of the less precise 0.30 m upstream default, and matches the independent Open3D RANSAC tolerance used for production evidence. Reference calibration uses complexity factor `0.95`; factor `1.0` retained sub-quarter-square-metre artifacts and therefore failed the existing minimum-facet safety gate.
6. Open3D independently refits every Roofer facet from the complete high-precision, normalized roof-return coordinates. Where LoD2.2 faces overlap in plan view, a point is assigned to the nearest 3D plane only when it is within 0.60 m of that plane; remote roof layers and vegetation cannot dilute the inlier ratio merely because they share the same map position. The validator rejects inadequate nearby support, low inlier share, normal disagreement, or excess plane RMSE before any measurement can price. The earlier PDAL HAG, cluster, surface-normal and curvature gates are not repeated during export because a second k-neighbour pass can erase valid returns beside a hip or valley; Open3D receives the accepted evidence and applies its own RANSAC gates.
7. `cityjson_geometry.py` reads Roofer's semantic `RoofSurface` faces and derives 3D facet area, horizontal area, pitch degrees, rise-per-12, azimuth, eaves, rakes, valleys, ridges, hips, external perimeter, internal roof-line length, and flat-roof area from their topology. Facet order, ring orientation, and starting vertices are coordinate-canonical so Roofer array order cannot change stable facet identities. Before classification the service clusters coordinates and nodes partial shared edges with a bounded 0.10 m tolerance, because valid Roofer facets can meet geometrically without reusing the same CityJSON vertex IDs. A proposed shared segment whose two reconstructed directions differ by more than 5 degrees is suppressed and audited as a crossing node artifact; it is never converted into a priced exterior edge. Single-use eave/rake candidates must lie within 0.50 m of the accepted Google Solar roofprint boundary; unmatched internal boundaries are retained in the audit, while high-side flashing is measured separately and excluded from exterior-perimeter reconciliation. Every accepted noncoplanar shared boundary is then independently snapped to the two incident facet planes' theoretical 3D intersection, using the Roofer boundary only for supported clipping limits and rejecting corrections over 0.35 m. The unchanged roofprint-perimeter and private reference-report gates must prove that suppressed artifacts did not remove required topology. The response records each derivation and rejection; non-manifold or ambiguous topology still fails closed. Every facet's mesh area must reconcile with `horizontal_area / cos(pitch_angle)`. County-record sketches and plans may corroborate this reconstruction but cannot supply internal edge topology or authorize pricing by themselves.
8. The service reconciles area and pitch against the existing Google Solar measurement. Low density, stale data, high RMSE, ambiguous footprints, unclassified edges, material/geometry conflicts, or provider disagreement return `404`/`422` with `available:false`; no dimensions are guessed.

The current-imagery validator is implemented and intentionally fail-closed. It accepts only an enabled registry entry whose machine-readable source, exact capture date, resolution, commercial reuse terms, coverage, quality and change metrics have been recorded. Lee County's authorized 2026 building evidence is enabled; counties without authorized current evidence use the Google Solar building-model reconciliation when eligible or return `INSPECTION_REQUIRED`. Geometry remains available for audit while pricing, PDF generation and a price email stay blocked.

Roofer documents about 10 points/m² as a good input density. This service defaults to a minimum of 8 points/m², no more than 10% missing roof coverage, no more than 0.35 m LoD2.2 RMSE, a fixed minimum LiDAR acquisition date of January 1, 2018, and an overall confidence of at least 0.80. Historical LiDAR may authorize pricing only after newer authorized evidence verifies the building is unchanged. Missing, stale, low-quality, or conflicting evidence remains `INSPECTION_REQUIRED`.

## License and data terms

- Service source and Roofer: GPL-3.0.
- PDAL: BSD-3-Clause.
- Open3D: MIT.
- `overturemaps-py`: MIT.
- USGS 3DEP data: U.S. Government public domain, free of charge and without use restrictions.
- NOAA Digital Coast datasets: public access subject to the registered dataset metadata and attribution.
- Overture Buildings: ODbL 1.0; retain the response's attribution and comply with Overture's source-attribution requirements.
- Microsoft GlobalML Building Footprints: CDLA-Permissive-2.0.
- OpenStreetMap Buildings: ODbL 1.0 with OpenStreetMap contributor attribution.
- Lee County building footprints: public county GIS layer with Lee County Property Appraiser and Lee County GIS attribution.
- Google Solar building masks: Google Maps Platform terms; transient authenticated GeoTIFFs are used for the requested measurement and are not redistributed.

This service intentionally does not use RoofMapNet's non-commercial assets, the unlicensed `citygml-roof-segment-labels` repository, or the unlicensed `vinycqueiroz/roof-calculator` code.

SamGeo, MobileSAM, TorchGeo and Open-CD are not production pricing authorities in this revision. They require expressly reusable source pixels plus a versioned Southwest Florida calibration set; running an uncalibrated checkpoint is not accepted as current-roof validation. Model-produced evidence must record model/checkpoint identity, calibration-dataset version, orthorectification, co-registration, shadow/vegetation masking, polygon IoU, boundary F1, area error, addition/deletion precision and recall, false-change rate and failure rate. Missing or sub-threshold evidence is forced to `INSPECTION_REQUIRED`. County building evidence and Google Solar model reconciliation remain the deployed fail-closed paths until those inputs exist.

## Test locally

The core topology tests use a synthetic CityJSON gable and need only Python:

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

Build and run the full container:

```bash
cp .env.example .env
# Replace every placeholder in .env.
docker build -t cape-roof-geometry:1.0.0 .
docker run --rm --env-file .env -p 8080:8080 cape-roof-geometry:1.0.0
curl --fail http://127.0.0.1:8080/healthz
```

The Docker image pins the official `3dgi/roofer:v1.0.0`, Micromamba, and Caddy images by immutable registry digest and installs PDAL 2.9.2 plus `overturemaps-py` 1.0.1. Roofer's GDAL/PROJ data paths are isolated to the Roofer subprocess; PDAL and `ogr2ogr` use the pinned conda data files. `/healthz` remains `503 not_ready` if required source provenance or executables are missing.

## Publish and deploy

The Apps Script provenance gate requires a public GitHub source URL and immutable service commit. Production is routed through the `geometry.caperoof.com` external HTTPS load balancer. Its serverless NEG, `cape-roof-geometry-neg`, targets the `cape-roof-geometry` Cloud Run service in `us-central1`; deploying the same service name in another region does not update production.

Before deployment:

1. Push this directory, including `config/`, to the public GPL-3.0 GitHub repository.
2. Build an immutable image and record both its digest and the corresponding 40-character Git commit.
3. Deploy that image to `cape-roof-geometry` in `us-central1`, with `SERVICE_SOURCE_URL` and `SERVICE_COMMIT` matching the public source.
4. Mount `AUTH_BEARER_TOKEN` from the `cape-roof-geometry-auth` Secret Manager secret. Never place its value in source, command output, or logs.
5. Keep Cloud Run invoker IAM disabled only because `/v1/roof-geometry` performs constant-time bearer authentication in the application; the endpoint must remain inaccessible without that bearer token.
6. Confirm `https://geometry.caperoof.com/healthz` returns `ready` and reports the exact deployed `serviceCommit` before changing Apps Script.
7. In Apps Script Properties set:
   - `OPEN_SOURCE_GEOMETRY_ENDPOINT=https://geometry.caperoof.com/v1/roof-geometry`
   - `OPEN_SOURCE_GEOMETRY_TOKEN=<the same AUTH_BEARER_TOKEN>`
8. Run `runConfigurationDiagnostics()` and `runAllTests()` in Apps Script, then process controlled onsite-verified test properties before customer automation is enabled.

`deploy/docker-compose.yml` remains an optional self-hosted development path; it is not the Cape Roof production route.

Cape Roof has approved historical-but-unchanged LiDAR acquired on or after January 1, 2018, so `ALLOW_HISTORICAL_VERIFIED_PRICING` is `true`. Keep `AUTOMATIC_PRICING_ENABLED=false` until deployment and live positive-path tests verify the full measurement, pricing, PDF, Drive and email transaction.

The geometry service also requires the reconstructed planimetric exterior to
match the selected roofprint perimeter.  Set
`MAXIMUM_ROOFPRINT_PERIMETER_VARIANCE_PERCENT=10`; the supported fail-closed
range is 2–15 percent.  This gate only detects broken exterior topology.  It
never derives roof lines from a parcel, appraiser sketch, or footprint and a
mismatch returns `INSPECTION_REQUIRED` through the customer-safe workflow.
Google Solar's 0.1 m roof mask is simplified at a bounded 0.15 m tolerance.
Larger tolerances are rejected because they can erase real eave transitions
while preserving nearly the same polygon area.
Facet-edge noding uses independent horizontal and vertical agreement limits;
`ROOF_EDGE_VERTICAL_NODE_TOLERANCE_METERS=0.30` permits bounded plane-fit
height residuals but does not merge vertically separated roof boundaries.

### Private EagleView calibration

Operator-authorized EagleView PDFs, measurement JSON and OBJ meshes can be checked offline without copying
customer files into this repository. Install the optional calibration dependency and point the tool at the private
export directory:

```bash
python -m pip install '.[calibration]'
python tools/eagleview_calibration.py /private/eagleview --output /private/eagleview/calibration-manifest.json
```

The manifest is deliberately de-identified: it contains report IDs, measurement totals, file hashes and comparison
errors, but no customer names, addresses, claims or imagery. OBJ facets are measured in their native foot units,
and every triangle verifies `sloped_area = horizontal_area / cos(pitch_angle)`. A report is not a calibration pass
unless area and pitch are within the configured tolerances and the roof-facet count agrees exactly. Missing or
failed geometry remains `inspectionRequired:true`; this tool never enables customer pricing.

When no packaged county orthophoto evidence is enabled, the service uses the
Google Solar Building Insights values already supplied by Apps Script as a
fail-closed current-building comparison. It requires newer, high-quality,
recent imagery metadata plus agreement between Solar ground/roof/pitch values,
the Overture footprint, and the LiDAR reconstruction. The service does not
download or redistribute Google imagery, records the provider date as
approximate, and emits the required `Includes data from Google Maps`
attribution. Any missing, stale, low-quality, or materially changed evidence
returns `INSPECTION_REQUIRED` and cannot authorize pricing.

Lee County uses the county's public 2026 aerial program (Eagle View imagery,
December 31, 2025 through March 22, 2026, 3-inch resolution) together with the official
machine-readable Building Footprints layer. The service queries only the
building polygon around the requested property, compares it with the pinned
Overture/LiDAR footprint, records the county feature ID and evidence update
date, and retains the county/Eagle View attribution. It does not store or
redistribute image pixels. Missing evidence or a material footprint change
fails closed.

The service loads `config/lidar_sources.yaml` and `config/imagery_sources.yaml` at startup and exposes their combined SHA-256 as `registryVersion`. A registry validation failure keeps `/healthz` in `not_ready`. Dataset years are never parsed from catalog filenames.

Container hosting, DNS control, and the public GitHub push are account-level operations. They cannot be represented as completed until an owner-authorized host and repository are connected.

## Endpoint behavior

The service accepts the existing schema generated by `fetchOpenSourceGeometry_()`. Successful responses preserve the exact fields already validated by Apps Script and add audit-only ridge/hip, quality, source, license, version, and reconciliation details. Names, emails, phone numbers, and street addresses are never sent to this service.
