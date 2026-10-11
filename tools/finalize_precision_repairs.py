"""Apply exact-context fixes for zero-valued quality evidence and isolated tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'app/cityjson_geometry.py'
    data = path.read_bytes()
    marker = b'def _quality_number('
    if marker not in data:
        anchor = b'\ndef _quality_confidence('
        if data.count(anchor) != 1:
            raise RuntimeError('Unexpected quality validator layout')
        helper = b'''
def _quality_number(attributes: dict[str, Any], field: str) -> float:
    """Keep genuine zero; reject missing, non-finite, or impossible QA evidence."""
    raw = attributes.get(field)
    try:
        if raw is None or isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip()):
            raise ValueError("missing or invalid")
        value = float(raw)
        if not math.isfinite(value) or value < 0 or (field == "rf_nodata_frac" and value > 1):
            raise ValueError("out of range")
        return value
    except (TypeError, ValueError, OverflowError) as error:
        raise UnreliableGeometryError(
            "ROOFER_QUALITY_METADATA_INVALID",
            "Roof reconstruction quality evidence is missing or invalid.",
            details={"field": field},
        ) from error

'''
        data = data.replace(anchor, helper + anchor, 1)
        for before, after in [
            (b'float(attributes.get("rf_pt_density", 0) or 0)', b'_quality_number(attributes, "rf_pt_density")'),
            (b'float(attributes.get("rf_nodata_frac", 1) or 1)', b'_quality_number(attributes, "rf_nodata_frac")'),
            (b'float(attributes.get("rf_rmse_lod22", math.inf) or math.inf)', b'_quality_number(attributes, "rf_rmse_lod22")'),
        ]:
            if data.count(before) != 1:
                raise RuntimeError('Unexpected quality field layout')
            data = data.replace(before, after, 1)
        path.write_bytes(data)

    # Mock the selected-release boundary, not the old strict-pin implementation.
    path = ROOT / 'tests/test_regional_sources.py'
    data = path.read_bytes()
    before = b'@patch("app.providers._require_pinned_overture_release", side_effect=["2026-08-19.0", "2026-08-19.0"])'
    after = before.replace(b'_require_pinned_overture_release', b'_download_overture_release')
    if after not in data:
        if data.count(before) != 1:
            raise RuntimeError('Unexpected release test fixture layout')
        path.write_bytes(data.replace(before, after, 1))

    # Millimetre-scale synthetic LAS coordinates require a local integer offset.
    path = ROOT / 'tools/native_geometry_smoke.py'
    data = path.read_bytes()
    before = b"'scale_x':0.001,'scale_y':0.001,'scale_z':0.001"
    after = before + b",'offset_x':'auto','offset_y':'auto','offset_z':'auto'"
    if after not in data:
        if data.count(before) != 1:
            raise RuntimeError('Unexpected synthetic LAS writer layout')
        path.write_bytes(data.replace(before, after, 1))


if __name__ == '__main__':
    main()
