# Cape Coral Roofing open geometry endpoint

This directory supplies the missing `POST /v1/roof-geometry` service already expected by `automation/google-apps-script/GeometryService.gs`. It does not replace or duplicate the existing form, Sheets, Drive, pricing, PDF, email, trigger, retry, or audit code.

## Production stack

1. The footprint resolver first uses the official Overture Maps client against a pinned open Buildings release. If that source has no usable coverage, it falls back in order to the pinned Microsoft GlobalML Building Footprints release, an explicitly authorized Lee County building layer, and a cached OpenStreetMap Overpass response. Overture, Microsoft and OSM are one correlated open-map lineage rather than three votes. County/open-map consensus requires IoU at least 0.80, centroid separation no more than 2 m and area difference no more than 10%; material or correlated-source disagreement fails closed.
2. The service resolves the building against the official Census county layer and the eight-county Southwest Florida registry. It ranks registered Manatee 2025, NOAA pre-/post-Ian 2022, and Florida Peninsular 2018–2020 candidates by exact registered acquisition date and complete buffered-footprint coverage. Lee 2026 remains disabled until its files, footprint and reuse terms are verified.
3. PDAL streams only the selected buffered property crop. Allowed surface, ground and building classes come from the selected source record, so NOAA class-1 surface returns are not discarded and bathymetric-only/noise classes are not accepted as roof evidence. The enumerated class histogram and exact pipeline are retained in the audit response.
4. 3DBAG Roofer v1.0.0 reconstructs LoD2.2 roof planes from the classified point cloud and the selected footprint.
5. Open3D independently fits every reconstructed facet to the normalized roof returns and rejects weak support, excessive residual error, or normal disagreement.
6. `cityjson_geometry.py` reads Roofer's semantic `RoofSurface` faces and derives 3D facet area, pitch, azimuth, eaves, rakes, valleys, ridges, hips, and flat-roof area from their topology. Every facet's mesh area must reconcile with `horizontal_area / cos(pitch_angle)`.
7. The service reconciles area and pitch against the existing Google Solar measurement. Low density, stale data, high RMSE, ambiguous footprints, unclassified edges, material/geometry conflicts, or provider disagreement return `404`/`422` with `available:false`; no dimensions are guessed.

The current-imagery validator is implemented and intentionally fail-closed. It accepts only an enabled registry entry whose machine-readable source, exact capture date, resolution, commercial reuse terms, coverage, quality and change metrics have been recorded. Lee County's authorized 2026 building evidence is enabled; counties without authorized current evidence use the Google Solar building-model reconciliation when eligible or return `INSPECTION_REQUIRED`. Geometry remains available for audit while pricing, PDF generation and a price email stay blocked.

Roofer documents about 10 points/m² as a good input density. This service defaults to a minimum of 8 points/m², no more than 10% missing roof coverage, no more than 0.35 m LoD2.2 RMSE, a maximum 10-year LiDAR age, and an overall confidence of at least 0.80. All thresholds are environment-controlled and should be calibrated against onsite measurements before automatic estimates are enabled.

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

This service intentionally does not use RoofMapNet's non-commercial assets, the unlicensed `citygml-roof-segment-labels` repository, or the unlicensed `vinycqueiroz/roof-calculator` code.

SamGeo, MobileSAM, TorchGeo and Open-CD are not production pricing authorities in this revision. They require expressly reusable source pixels plus a versioned Southwest Florida calibration set; running an uncalibrated checkpoint is not accepted as current-roof validation. County building evidence and Google Solar model reconciliation remain the deployed fail-closed paths until those inputs exist.

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

The Apps Script provenance gate requires a public GitHub source URL and immutable service commit. Before deployment:

1. Push this `geometry-service` directory, including `config/`, to a public GitHub repository under GPL-3.0.
2. Set `SERVICE_SOURCE_URL` to that repository and `SERVICE_COMMIT` to the deployed 40-character Git commit.
3. Set `AUTH_BEARER_TOKEN` to at least 32 random bytes.
4. Create DNS for `geometry.caperoof.com` pointing to a Linux host with at least 4 GB RAM and 2 GB temporary disk.
5. From this directory, run `docker compose -f deploy/docker-compose.yml up -d --build`. Caddy obtains and renews HTTPS automatically.
6. Confirm `https://geometry.caperoof.com/healthz` returns `ready`.
7. In Apps Script Properties set:
   - `OPEN_SOURCE_GEOMETRY_ENDPOINT=https://geometry.caperoof.com/v1/roof-geometry`
   - `OPEN_SOURCE_GEOMETRY_TOKEN=<the same AUTH_BEARER_TOKEN>`
8. Run `runConfigurationDiagnostics()` and `runAllTests()` in Apps Script, then process controlled onsite-verified test properties before customer automation is enabled.

During calibration, the corresponding Apps Script Config values `AUTOMATIC_PRICING_ENABLED` and `ALLOW_HISTORICAL_VERIFIED_PRICING` must remain `false`.

When no packaged county orthophoto evidence is enabled, the service can use the
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
