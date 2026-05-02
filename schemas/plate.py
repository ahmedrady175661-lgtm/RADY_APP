from typing import Optional

from pydantic import BaseModel


class PlateResult(BaseModel):
    full_plate: str
    plate_letters: Optional[str] = ""
    plate_numbers: Optional[str] = ""
    street_name: str = "غير محدد"
    vehicle_type: str = "ملاكى"
    gps: Optional[str] = ""
    recorder_name: Optional[str] = ""
    recording_date: Optional[str] = ""
    sheet_name: Optional[str] = ""
    notes: Optional[str] = ""


class ProcessResponse(BaseModel):
    plates: list[PlateResult]
    total: int


class ExcelRow(BaseModel):
    full_plate: Optional[str] = ""
    vehicle_type: Optional[str] = "ملاكى"
    street_name: Optional[str] = "غير محدد"
    notes: Optional[str] = ""
    recorder_name: Optional[str] = ""
    recording_date: Optional[str] = ""
    gps: Optional[str] = ""
