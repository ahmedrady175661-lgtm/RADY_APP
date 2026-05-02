from .audio import router as audio_router
from .check import router as check_router
from .excel import router as excel_router
from .gps import router as gps_router

__all__ = ["audio_router", "excel_router", "check_router", "gps_router"]
