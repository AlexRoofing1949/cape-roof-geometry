"""Versioned request schema shared with the Apps Script orchestrator."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class SolarFacet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    planeId: str | None = None
    areaSqFt: float = Field(ge=0)
    groundAreaSqFt: float | None = Field(default=None, ge=0)
    pitchDegrees: float = Field(ge=0, le=90)
    azimuthDegrees: float | None = Field(default=None, ge=0, le=360)


class SolarReference(BaseModel):
    roofAreaSqFt: float = Field(gt=0)
    averagePitchDegrees: float = Field(ge=0, le=90)
    maximumPitchDegrees: float = Field(ge=0, le=90)
    imageryDate: str | None = Field(default=None, max_length=10)
    imageryQuality: str | None = Field(default=None, max_length=20)
    facets: list[SolarFacet] = Field(min_length=1, max_length=200)

    @field_validator("imageryDate", mode="before")
    @classmethod
    def valid_imagery_date(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value)
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
            raise ValueError("imageryDate must use YYYY-MM-DD.")
        return text


class SelectedRoofType(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=80)
    family: str = Field(min_length=1, max_length=40)
    material: str = Field(min_length=1, max_length=80)
    flat: bool


class GeometryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: str
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{6,100}$")
    location: Location
    selectedRoofType: SelectedRoofType
    solarReference: SolarReference
    requiredOutputs: list[str] = Field(min_length=1, max_length=30)

    @field_validator("schemaVersion")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Only schemaVersion 1.0 is supported.")
        return value

    @field_validator("requiredOutputs")
    @classmethod
    def required_output_contract(cls, values: list[str]) -> list[str]:
        required = {
            "roofAreaSqFt",
            "averagePitchDegrees",
            "maximumPitchDegrees",
            "facets",
            "rakesFeet",
            "eavesFeet",
            "valleysFeet",
            "ridgesFeet",
            "hipsFeet",
            "flatRoofAreaSqFt",
        }
        if not required.issubset(set(values)):
            missing = ", ".join(sorted(required.difference(values)))
            raise ValueError(f"requiredOutputs is missing: {missing}")
        return values

    def audit_safe_summary(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "requestId": self.requestId,
            "location": self.location.model_dump(),
            "selectedRoofType": self.selectedRoofType.model_dump(),
            "requiredOutputs": self.requiredOutputs,
        }
