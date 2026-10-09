#!/usr/bin/env python3
"""Build a private, de-identified EagleView geometry calibration manifest.

The tool reads files supplied by the operator from a directory outside the
repository.  It emits report IDs, measurements, file hashes, and comparison
errors only; customer names, addresses, claim numbers, and images are never
copied into the repository or output manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


REPORT_ID_RE = re.compile(r"^(\d{6,12})")
NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
EDGE_FIELDS = {
    "ridges": ("ridges_ft", "ridgesFeet"),
    "hips": ("hips_ft", "hipsFeet"),
    "valleys": ("valleys_ft", "valleysFeet"),
    "rakes": ("rakes_ft", "rakesFeet"),
    "eaves": ("eaves_ft", "eavesFeet"),
}


@dataclass(frozen=True)
class ReferenceMeasurements:
    report_id: str
    roof_area_sq_ft: float
    facet_count: int
    predominant_pitch_rise: float
    ridges_ft: float | None = None
    hips_ft: float | None = None
    valleys_ft: float | None = None
    rakes_ft: float | None = None
    eaves_ft: float | None = None
    flashing_ft: float | None = None
    step_flashing_ft: float | None = None
    suggested_waste_percent: float | None = None
    complexity: str | None = None

    @property
    def predominant_pitch_degrees(self) -> float:
        return math.degrees(math.atan(self.predominant_pitch_rise / 12.0))


@dataclass(frozen=True)
class ObjMeasurements:
    roof_area_sq_ft: float
    horizontal_area_sq_ft: float
    facet_count: int
    area_weighted_pitch_degrees: float
    predominant_pitch_rise: float
    predominant_pitch_degrees: float
    area_by_pitch_rise_sq_ft: dict[str, float]
    facet_pitch_rise_by_id: dict[str, float]
    maximum_formula_error_percent: float
    triangle_count: int


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _first(text: str, pattern: str, flags: int = re.IGNORECASE) -> re.Match[str] | None:
    return re.search(pattern, text, flags)


def _metric(text: str, label: str, unit: str) -> float | None:
    # Edge totals must come from their own Report Summary rows.  Without the
    # line anchor, ``Hips`` also matches the cover-page aggregate
    # ``Total Ridges/Hips`` and overstates hip length by the ridge total.
    match = _first(text, rf"^\s*{label}\s*=\s*{NUMBER}\s*{unit}", re.IGNORECASE | re.MULTILINE)
    return _number(match.group(1)) if match else None


def parse_eagleview_text(text: str) -> ReferenceMeasurements:
    """Parse the stable summary and length labels in an EagleView report."""

    report = _first(text, r"Report:\s*(\d{6,12})")
    area = _first(text, rf"Total Roof Area\s*=\s*{NUMBER}\s*sq\s*ft")
    facets = _first(text, r"Total Roof Facets\s*=\s*(\d+)")
    pitch = _first(text, rf"Predominant Pitch\s*=\s*{NUMBER}\s*/\s*12")
    if not all((report, area, facets, pitch)):
        raise ValueError("Report text is missing its ID, roof area, facet count, or predominant pitch.")

    return ReferenceMeasurements(
        report_id=report.group(1),
        roof_area_sq_ft=_number(area.group(1)),
        facet_count=int(facets.group(1)),
        predominant_pitch_rise=_number(pitch.group(1)),
        ridges_ft=_metric(text, "Ridges", "ft"),
        hips_ft=_metric(text, "Hips", "ft"),
        valleys_ft=_metric(text, "Valleys", "ft"),
        rakes_ft=_metric(text, "Rakes", "ft"),
        eaves_ft=_metric(text, r"Eaves(?:/Starter)?", "ft"),
        flashing_ft=_metric(text, "Flashing", "ft"),
        step_flashing_ft=_metric(text, r"Step\s+flashing", "ft"),
    )


def parse_eagleview_pdf(path: Path) -> ReferenceMeasurements:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised by the operator environment
        raise RuntimeError("PDF calibration requires the optional 'pypdf' package.") from exc
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return parse_eagleview_text(text)


def parse_measurement_json(path: Path) -> ReferenceMeasurements:
    payload = json.loads(path.read_text(encoding="utf-8"))
    root = payload["EAGLEVIEW_EXPORT"]
    report_id = str(root["REPORT"]["@reportId"])
    attributes = {
        str(item.get("@name")): item.get("@value")
        for item in root.get("OVERALL_SUMMARY", {}).get("ATTRIBUTE", [])
        if isinstance(item, dict)
    }

    def optional(name: str) -> float | None:
        value = attributes.get(name)
        return None if value in (None, "") else _number(str(value))

    pitch_text = str(attributes.get("PredominantPitch") or "")
    pitch_match = re.fullmatch(rf"{NUMBER}\s*/\s*12", pitch_text)
    if not pitch_match:
        raise ValueError("Measurement JSON is missing a valid predominant pitch.")
    return ReferenceMeasurements(
        report_id=report_id,
        roof_area_sq_ft=_number(str(attributes["TotalRoofArea"])),
        facet_count=int(attributes["TotalRoofFacets"]),
        predominant_pitch_rise=_number(pitch_match.group(1)),
        ridges_ft=optional("TotalRidgesLength"),
        hips_ft=optional("TotalHipsLength"),
        valleys_ft=optional("TotalValleysLength"),
        rakes_ft=optional("TotalRakesLength"),
        eaves_ft=optional("TotalEavesLength"),
        flashing_ft=optional("TotalFlashingLength"),
        step_flashing_ft=optional("TotalStepFlashingLength"),
        suggested_waste_percent=optional("SuggestedWastePercentage"),
        complexity=str(attributes.get("StructureComplexity") or "") or None,
    )


def _vector(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return b[0] - a[0], b[1] - a[1], b[2] - a[2]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def parse_eagleview_obj(path: Path) -> ObjMeasurements:
    """Measure EagleView's foot-based, triangulated roof OBJ export."""

    vertices: list[tuple[float, float, float]] = []
    current_object = ""
    roof_objects: set[str] = set()
    areas: list[float] = []
    horizontal_areas: list[float] = []
    pitches: list[float] = []
    areas_by_pitch_rise: dict[float, float] = {}
    facet_areas: dict[str, float] = {}
    facet_pitch_area: dict[str, float] = {}
    formula_errors: list[float] = []

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("v "):
            values = line.split()
            if len(values) >= 4:
                vertices.append(tuple(map(float, values[1:4])))  # type: ignore[arg-type]
        elif line.startswith("o "):
            current_object = line[2:].strip()
        elif line.startswith("f ") and current_object.startswith("Roof.") and not current_object.endswith(".Label"):
            indices = [int(part.split("/", 1)[0]) for part in line.split()[1:]]
            if len(indices) < 3:
                continue
            roof_objects.add(current_object)
            first = vertices[indices[0] - 1]
            for offset in range(1, len(indices) - 1):
                second = vertices[indices[offset] - 1]
                third = vertices[indices[offset + 1] - 1]
                cross = _cross(_vector(first, second), _vector(first, third))
                doubled_area = _norm(cross)
                if doubled_area <= 1e-9:
                    continue
                area = doubled_area / 2.0
                horizontal = abs(cross[2]) / 2.0
                pitch = math.degrees(math.atan2(math.hypot(cross[0], cross[1]), abs(cross[2])))
                cosine = math.cos(math.radians(pitch))
                formula_area = horizontal / cosine if cosine > 1e-12 else math.inf
                areas.append(area)
                horizontal_areas.append(horizontal)
                pitches.append(pitch)
                facet_areas[current_object] = facet_areas.get(current_object, 0.0) + area
                facet_pitch_area[current_object] = facet_pitch_area.get(current_object, 0.0) + area * pitch
                formula_errors.append(abs(formula_area - area) / area * 100.0)

    if not areas or not roof_objects:
        raise ValueError("OBJ contains no non-degenerate EagleView Roof.* triangles.")
    facet_pitch_rise_by_id: dict[str, float] = {}
    for facet_name, facet_area in facet_areas.items():
        facet_pitch = facet_pitch_area[facet_name] / facet_area
        # Premium report summaries publish whole rise-per-12 classes for each
        # roof facet.  EagleView OBJ facets can contain slightly non-coplanar
        # display triangles, so classify the area-weighted facet plane instead
        # of classifying each display triangle independently.
        raw_pitch_rise = math.tan(math.radians(facet_pitch)) * 12.0
        facet_pitch_rise_by_id[facet_name.removeprefix("Roof.")] = raw_pitch_rise
        # OBJ decimals can encode an exact half pitch a few ten-millionths
        # below the boundary (for example 2.49999997 for a published 3/12).
        pitch_rise = float(math.floor(raw_pitch_rise + 0.500001))
        areas_by_pitch_rise[pitch_rise] = areas_by_pitch_rise.get(pitch_rise, 0.0) + facet_area
    total_area = sum(areas)
    predominant_pitch_rise = max(areas_by_pitch_rise, key=areas_by_pitch_rise.get)  # type: ignore[arg-type]
    return ObjMeasurements(
        roof_area_sq_ft=total_area,
        horizontal_area_sq_ft=sum(horizontal_areas),
        facet_count=len(roof_objects),
        area_weighted_pitch_degrees=sum(area * pitch for area, pitch in zip(areas, pitches)) / total_area,
        predominant_pitch_rise=predominant_pitch_rise,
        predominant_pitch_degrees=math.degrees(math.atan(predominant_pitch_rise / 12.0)),
        area_by_pitch_rise_sq_ft={
            f"{rise:g}/12": area for rise, area in sorted(areas_by_pitch_rise.items())
        },
        facet_pitch_rise_by_id=dict(sorted(facet_pitch_rise_by_id.items())),
        maximum_formula_error_percent=max(formula_errors),
        triangle_count=len(areas),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_id(path: Path) -> str | None:
    match = REPORT_ID_RE.match(path.name)
    return match.group(1) if match else None


def _percent_error(actual: float, expected: float) -> float:
    return abs(actual - expected) / expected * 100.0


def _candidate_geometry(payload: dict[str, Any]) -> dict[str, Any]:
    geometry = payload.get("geometry")
    if isinstance(geometry, dict):
        return geometry
    if isinstance(payload.get("metrics"), dict):
        return payload["metrics"]
    return payload


def _candidate_topology_hash(payload: dict[str, Any], geometry: dict[str, Any]) -> str:
    for value in (
        (geometry.get("topology") or {}).get("topologyHash"),
        geometry.get("topologyHash"),
        (payload.get("topology") or {}).get("topologyHash"),
        payload.get("topologyHash"),
    ):
        text = str(value or "")
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return text
    return ""


def _candidate_formula_error_percent(geometry: dict[str, Any]) -> float | None:
    facets = geometry.get("facets")
    if not isinstance(facets, list) or not facets:
        return None
    errors = []
    for facet in facets:
        reported_error = facet.get("slopeAreaFormulaErrorPercent")
        if reported_error is not None:
            try:
                errors.append(float(reported_error))
                continue
            except (TypeError, ValueError):
                return None
        try:
            area = float(facet["areaSqFt"])
            horizontal = float(facet["horizontalAreaSqFt"])
            pitch = float(facet["pitchDegrees"])
            calculated = horizontal / math.cos(math.radians(pitch))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        if area <= 0 or not all(math.isfinite(value) for value in (area, calculated)):
            return None
        errors.append(abs(calculated - area) / area * 100.0)
    return max(errors)


def evaluate_candidate_runs(
    reference: ReferenceMeasurements,
    payloads: list[dict[str, Any]],
    *,
    maximum_area_error_percent: float = 1.0,
    maximum_pitch_error_degrees: float = 1.0,
    maximum_edge_error_feet: float = 1.0,
    minimum_repeat_runs: int = 2,
) -> dict[str, Any]:
    """Apply every production promotion gate to repeated Cape Roof results."""

    if len(payloads) < minimum_repeat_runs:
        return {
            "status": "INSUFFICIENT_REPEAT_RUNS",
            "inspectionRequired": True,
            "repeatRunCount": len(payloads),
            "minimumRepeatRuns": minimum_repeat_runs,
        }
    evaluations = []
    topology_hashes = []
    for run_index, payload in enumerate(payloads, start=1):
        geometry = _candidate_geometry(payload)
        topology_hash = _candidate_topology_hash(payload, geometry)
        topology_hashes.append(topology_hash)
        try:
            area = float(geometry["roofAreaSqFt"])
            pitch = float(geometry["averagePitchDegrees"])
            facet_count = len(geometry["facets"])
        except (KeyError, TypeError, ValueError):
            evaluations.append(
                {
                    "run": run_index,
                    "status": "FAIL",
                    "failures": ["CANDIDATE_GEOMETRY_INCOMPLETE"],
                }
            )
            continue
        area_error = _percent_error(area, reference.roof_area_sq_ft)
        pitch_error = abs(pitch - reference.predominant_pitch_degrees)
        formula_error = _candidate_formula_error_percent(geometry)
        failures = []
        if area_error > maximum_area_error_percent:
            failures.append("ROOF_AREA_ERROR_EXCEEDED")
        if pitch_error > maximum_pitch_error_degrees:
            failures.append("PITCH_ERROR_EXCEEDED")
        if facet_count != reference.facet_count:
            failures.append("FACET_COUNT_MISMATCH")
        if formula_error is None or formula_error > 1e-6:
            failures.append("COSINE_FORMULA_ERROR_EXCEEDED")
        edge_results: dict[str, Any] = {}
        for edge_name, (reference_field, candidate_field) in EDGE_FIELDS.items():
            expected = getattr(reference, reference_field)
            actual = geometry.get(candidate_field)
            if expected is None or actual is None:
                failures.append(f"{edge_name.upper()}_MEASUREMENT_MISSING")
                edge_results[edge_name] = {"status": "MISSING"}
                continue
            absolute_error = abs(float(actual) - float(expected))
            passed = absolute_error <= maximum_edge_error_feet
            if not passed:
                failures.append(f"{edge_name.upper()}_ERROR_EXCEEDED")
            edge_results[edge_name] = {
                "expectedFeet": float(expected),
                "actualFeet": float(actual),
                "absoluteErrorFeet": round(absolute_error, 4),
                "status": "PASS" if passed else "FAIL",
            }
        if not topology_hash:
            failures.append("TOPOLOGY_HASH_MISSING")
        evaluations.append(
            {
                "run": run_index,
                "status": "PASS" if not failures else "FAIL",
                "failures": failures,
                "areaErrorPercent": round(area_error, 6),
                "pitchErrorDegrees": round(pitch_error, 6),
                "facetCount": facet_count,
                "maximumFormulaErrorPercent": (
                    None if formula_error is None else round(formula_error, 9)
                ),
                "edges": edge_results,
                "topologyHash": topology_hash,
            }
        )
    stable_hash = (
        len(topology_hashes) >= minimum_repeat_runs
        and all(topology_hashes)
        and len(set(topology_hashes)) == 1
    )
    if not stable_hash:
        for evaluation in evaluations:
            evaluation.setdefault("failures", []).append("TOPOLOGY_HASH_NOT_DETERMINISTIC")
            evaluation["status"] = "FAIL"
    passed = bool(evaluations) and all(item["status"] == "PASS" for item in evaluations)
    return {
        "status": "PASS" if passed else "FAIL",
        "inspectionRequired": not passed,
        "repeatRunCount": len(payloads),
        "topologyHashDeterministic": stable_hash,
        "topologyHash": topology_hashes[0] if stable_hash else "",
        "runs": evaluations,
    }


def build_manifest(
    directory: Path,
    max_area_error_percent: float,
    max_pitch_error_degrees: float,
    max_edge_error_feet: float = 1.0,
    minimum_repeat_runs: int = 2,
) -> dict[str, Any]:
    premium_pdfs = {
        report_id: path
        for path in directory.glob("*_ECPremiumReport.PDF")
        if (report_id := _report_id(path))
    }
    json_files: dict[str, Path] = {}
    for path in directory.glob("*_EVMeasurementJSON*.json"):
        report_id = _report_id(path)
        if report_id and report_id not in json_files:
            json_files[report_id] = path
    obj_files = {report_id: path for path in directory.glob("*.obj") if (report_id := _report_id(path))}
    candidate_files: dict[str, list[Path]] = {}
    for path in sorted(directory.glob("*_CapeRoofGeometry*.json")):
        report_id = _report_id(path)
        if report_id:
            candidate_files.setdefault(report_id, []).append(path)

    report_ids = sorted(set(premium_pdfs) | set(json_files))
    records: list[dict[str, Any]] = []
    for report_id in report_ids:
        reference_path = json_files.get(report_id) or premium_pdfs.get(report_id)
        assert reference_path is not None
        reference = (
            parse_measurement_json(reference_path)
            if reference_path.suffix.lower() == ".json"
            else parse_eagleview_pdf(reference_path)
        )
        if reference.report_id != report_id:
            raise ValueError(f"Report ID mismatch for {reference_path.name}.")
        obj_path = obj_files.get(report_id)
        record: dict[str, Any] = {
            "reportId": report_id,
            "reference": asdict(reference),
            "referenceFile": reference_path.name,
            "referenceSha256": _sha256(reference_path),
            "status": "MISSING_CANDIDATE",
            "inspectionRequired": True,
        }
        if obj_path:
            measured = parse_eagleview_obj(obj_path)
            area_error = _percent_error(measured.roof_area_sq_ft, reference.roof_area_sq_ft)
            # EagleView's "predominant pitch" is the area-dominant pitch class,
            # not the area-weighted mean of every facet.  Keep the weighted mean
            # as an audit metric, but compare like-for-like here.
            pitch_error = abs(measured.predominant_pitch_degrees - reference.predominant_pitch_degrees)
            facet_match = measured.facet_count == reference.facet_count
            passed = (
                area_error <= max_area_error_percent
                and pitch_error <= max_pitch_error_degrees
                and facet_match
                and measured.maximum_formula_error_percent <= 1e-6
            )
            record.update(
                {
                    "obj": asdict(measured),
                    "objFile": obj_path.name,
                    "objSha256": _sha256(obj_path),
                    "areaErrorPercent": area_error,
                    "pitchErrorDegrees": pitch_error,
                    "facetCountMatches": facet_match,
                    "referenceObjStatus": "PASS" if passed else "FAIL",
                }
            )
        payloads: list[dict[str, Any]] = []
        for candidate_path in candidate_files.get(report_id, []):
            candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            contained_runs = candidate_payload.get("runs")
            if isinstance(contained_runs, list):
                payloads.extend(run for run in contained_runs if isinstance(run, dict))
            else:
                payloads.append(candidate_payload)
        if payloads:
            candidate_evaluation = evaluate_candidate_runs(
                reference,
                payloads,
                maximum_area_error_percent=max_area_error_percent,
                maximum_pitch_error_degrees=max_pitch_error_degrees,
                maximum_edge_error_feet=max_edge_error_feet,
                minimum_repeat_runs=minimum_repeat_runs,
            )
            record.update(
                {
                    "candidateFiles": [
                        path.name for path in candidate_files.get(report_id, [])
                    ],
                    "candidateFileSha256": [
                        _sha256(path) for path in candidate_files.get(report_id, [])
                    ],
                    "candidate": candidate_evaluation,
                    "status": candidate_evaluation["status"],
                    "inspectionRequired": candidate_evaluation["inspectionRequired"],
                }
            )
        records.append(record)

    source_fingerprint = hashlib.sha256(
        "\n".join(
            (
                f"{record['reportId']}|{record['referenceSha256']}|"
                f"{record.get('objSha256', '')}|"
                f"{','.join(record.get('candidateFileSha256', []))}"
            )
            for record in records
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": "1.0",
        "datasetVersion": f"CCR-EV-{source_fingerprint[:16]}",
        "containsCustomerIdentifiers": False,
        "thresholds": {
            "maximumAreaErrorPercent": max_area_error_percent,
            "maximumPitchErrorDegrees": max_pitch_error_degrees,
            "facetCountMustMatch": True,
            "maximumFormulaErrorPercent": 1e-6,
            "maximumEdgeErrorFeet": max_edge_error_feet,
            "minimumRepeatRuns": minimum_repeat_runs,
            "topologyHashMustBeDeterministic": True,
        },
        "summary": {
            "reports": len(records),
            "passed": sum(record["status"] == "PASS" for record in records),
            "failed": sum(record["status"] == "FAIL" for record in records),
            "missingCandidate": sum(
                record["status"] == "MISSING_CANDIDATE" for record in records
            ),
        },
        "records": records,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Private EagleView export directory.")
    parser.add_argument("--output", type=Path, help="Write de-identified JSON here; otherwise print it.")
    parser.add_argument("--max-area-error-percent", type=float, default=1.0)
    parser.add_argument("--max-pitch-error-degrees", type=float, default=1.0)
    parser.add_argument("--max-edge-error-feet", type=float, default=1.0)
    parser.add_argument("--minimum-repeat-runs", type=int, default=2)
    args = parser.parse_args(argv)
    manifest = build_manifest(
        args.directory,
        args.max_area_error_percent,
        args.max_pitch_error_degrees,
        args.max_edge_error_feet,
        args.minimum_repeat_runs,
    )
    rendered = json.dumps(manifest, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if manifest["summary"]["passed"] == manifest["summary"]["reports"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
