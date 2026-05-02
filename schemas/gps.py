from typing import Optional

from pydantic import BaseModel


class GpsPoint(BaseModel):
    lat: float
    lng: float
    label: Optional[str] = ""
    row_no: Optional[int] = None


class GpsParseResponse(BaseModel):
    points: list[GpsPoint]
    total: int
    skipped: int
    headers: list[str]
    label_col_used: str


class GpsVehicle(BaseModel):
    plate: str
    gps: str
    date: Optional[str] = ""
    vehicle_type: Optional[str] = ""
    notes: Optional[str] = ""


class GpsMatchResponse(BaseModel):
    vehicles: list[GpsVehicle]
    total: int
    skipped: int


class GpsRankedResult(BaseModel):
    rank: int
    plate: str
    gps: str
    vehicle_type: Optional[str] = ""
    notes: Optional[str] = ""
    distance_km: Optional[float] = None
    duration_min: Optional[float] = None
    date: Optional[str] = ""


class GpsExportRequest(BaseModel):
    results: list[GpsRankedResult]
    failed: list[dict] = []
    my_lat: Optional[str] = ""
    my_lon: Optional[str] = ""
