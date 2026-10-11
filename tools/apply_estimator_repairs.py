"""Apply reviewed repairs to the known baseline, preserving original line endings.
Idempotent: fully repaired files are left unchanged. Refuses unknown baselines.
This tool never deploys a service, changes pricing, or reads credentials.
"""
from pathlib import Path
import difflib
import hashlib

ROOT = Path(__file__).resolve().parents[1]


def apply(path, expected_blob, marker, edits):
    target = ROOT / path
    raw = target.read_bytes()
    old = raw.decode('utf-8')
    normalized = old.replace('\r\n', '\n')
    if marker in normalized:
        return
    digest = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if digest != expected_blob:
        raise RuntimeError('Unrecognized baseline: ' + path)
    new = normalized
    for before, after in edits:
        if new.count(before) != 1:
            raise RuntimeError('Repair context is not unique in ' + path)
        new = new.replace(before, after, 1)
    before_lines, after_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=[x.rstrip('\r\n') for x in before_lines],
                                     b=[x.rstrip('\r\n') for x in after_lines], autojunk=False)
    output = []
    for tag, i, j, k, l in matcher.get_opcodes():
        output.extend(before_lines[i:j] if tag == 'equal' else after_lines[k:l])
    target.write_bytes(''.join(output).encode())


def main():
    apply('app/providers.py', 'cbbcef1376521f7942cfad8b8355f442bfc6f9af',
          'catalog_error: str | None = None', [
        ('        projected = transform_geometry(geometry, "EPSG:4326", f"EPSG:{epsg}")',
         '        # Identical duplicate records are not competing buildings.\n        if any(geometry.equals(existing[3]) for existing in candidates):\n            continue\n        projected = transform_geometry(geometry, "EPSG:4326", f"EPSG:{epsg}")'),
        ('            and abs(second[0] - first[0]) <= settings.footprint_ambiguity_meters',
         '            and abs(second[0] - first[0]) <= settings.footprint_ambiguity_meters\n            and not (first[3].contains(point) and not second[3].covers(point))'),
        ('    catalog_features: list[dict[str, Any]] | None = None',
         '    catalog_features: list[dict[str, Any]] | None = None\n    catalog_error: str | None = None\n    transient_failures = False'),
        ('            if catalog_features is None:\n                catalog_features = list(_catalog_features(settings))',
         '            if catalog_features is None and catalog_error is None:\n                try:\n                    catalog_features = list(_catalog_features(settings))\n                except (TransientProviderError, NoCoverageError, UnreliableGeometryError) as error:\n                    catalog_error = error.code\n                    transient_failures = transient_failures or error.retryable\n            if catalog_error is not None:\n                record.update(decision="SOURCE_UNAVAILABLE", errorCode=catalog_error)\n                audit.append(record)\n                continue'),
        ('            coverage, point_count = _ept_coverage(source.endpoint, settings)',
         '            try:\n                coverage, point_count = _ept_coverage(source.endpoint, settings)\n            except (TransientProviderError, NoCoverageError, UnreliableGeometryError) as error:\n                record.update(decision="SOURCE_UNAVAILABLE", errorCode=error.code)\n                audit.append(record)\n                transient_failures = transient_failures or error.retryable\n                continue'),
        ('    if not candidates:\n        raise NoCoverageError("NO_LIDAR_COVERAGE",',
         '    if not candidates:\n        if transient_failures:\n            raise TransientProviderError(\n                "LIDAR_SOURCES_TEMPORARILY_UNAVAILABLE",\n                "Eligible LiDAR sources are temporarily unavailable; retry this request.",\n            )\n        raise NoCoverageError("NO_LIDAR_COVERAGE",'),
    ])
    helper = '''
def _roofprint_point_density(
    pointcloud: Path, projected_footprint: Any, workspace: Path, settings: Settings
) -> dict[str, float | int | str]:
    """Count roof returns inside the unbuffered roofprint, excluding ground."""
    area = float(projected_footprint.area)
    if not math.isfinite(area) or area <= 0:
        raise UnreliableGeometryError("ROOFPRINT_AREA_INVALID", "The roofprint area is invalid.")
    metadata_path = workspace / "roofprint-density-metadata.json"
    pipeline_path = workspace / "roofprint-density-pipeline.json"
    pipeline_path.write_text(json.dumps({"pipeline": [
        {"type": "readers.las", "filename": str(pointcloud)},
        {"type": "filters.crop", "polygon": projected_footprint.wkt},
        {"type": "filters.expression", "expression": "Classification == 6"},
        {"type": "filters.stats", "dimensions": "Classification", "count": "Classification"},
    ]}), encoding="utf-8")
    _run(["pdal", "pipeline", "--metadata", str(metadata_path), str(pipeline_path)],
         timeout=settings.command_timeout_seconds)
    count = _classification_histogram(metadata_path).get("6", 0)
    return {"roofprintReturnCount": count, "roofprintAreaSqMeters": area,
            "pointDensityPpsm": count / area,
            "densityBasis": "class-6 returns inside unbuffered roofprint"}

'''
    apply('app/pipeline.py', '8415ea72ebdb62aa84469bb3da11e831e442f05d',
          'def _roofprint_point_density(', [
        ('import json\n', 'import json\nimport math\n'),
        ('                    "Classification == 6 || "\n                    f"(Classification == 1 && HeightAboveGround',
         '                    "Classification == 2 || Classification == 6 || "\n                    f"(Classification == 1 && HeightAboveGround'),
        ('                "is3d": True,', '                "is3d": True,\n                "where": "Classification != 2",\n                "where_merge": True,'),
        ('                "expression": "ClusterID > 0",', '                "expression": "Classification == 2 || ClusterID > 0",'),
        ('                "always_up": True,', '                "always_up": True,\n                "where": "Classification != 2",\n                "where_merge": True,'),
        ('                    "Classification == 6 || "\n                    f"(Classification == 1 && NormalZ',
         '                    "Classification == 2 || Classification == 6 || "\n                    f"(Classification == 1 && NormalZ'),
        ('\ndef _classification_histogram', helper + '\ndef _classification_histogram'),
        ('                point_density = float(point_audit["filteredPointCount"]) / max(\n                    float(projected_footprint.area), 0.01\n                )',
         '                density_audit = _roofprint_point_density(\n                    pointcloud, projected_footprint, workspace, settings\n                )\n                point_audit.update(density_audit)\n                point_density = float(density_audit["pointDensityPpsm"])'),
        ('            except UnreliableGeometryError as error:\n                attempts.append(',
         '            except (UnreliableGeometryError, TransientProviderError) as error:\n                attempts.append('),
        ('                        "errorCode": error.code,\n                        **({"errorDetails"',
         '                        "errorCode": error.code,\n                        "retryable": error.retryable,\n                        **({"errorDetails"'),
        ('        if selected is None:\n            error_codes',
         '        if selected is None:\n            if any(item.get("retryable") for item in attempts):\n                raise TransientProviderError(\n                    "LIDAR_CANDIDATES_TEMPORARILY_UNAVAILABLE",\n                    "A LiDAR candidate could not be evaluated due to a temporary failure; retry this request.",\n                )\n            error_codes'),
    ])
    apply('tests/test_regional_sources.py', '8a46e661e055b6839515c615ce0f38bf0277c12c',
          'stage.get("expression") == "Classification == 2 || ClusterID > 0"', [
        ('stage.get("expression") == "ClusterID > 0"',
         'stage.get("expression") == "Classification == 2 || ClusterID > 0"'),
        ('            self.assertLess(stage_types.index("filters.cluster"), stage_types.index("filters.assign"))',
         '            for stage_type in ("filters.cluster", "filters.normal"):\n                stage = next(item for item in stages if item["type"] == stage_type)\n                self.assertEqual(stage["where"], "Classification != 2")\n                self.assertIs(stage["where_merge"], True)\n            for dimension in ("HeightAboveGround", "NormalZ"):\n                expression = next(item["expression"] for item in stages\n                                  if dimension in item.get("expression", ""))\n                self.assertIn("Classification == 2 ||", expression)\n            self.assertLess(stage_types.index("filters.cluster"), stage_types.index("filters.assign"))'),
    ])


if __name__ == '__main__':
    main()
