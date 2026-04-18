from __future__ import annotations

from .base import OfficeFormatStrategy
from .default import DefaultOfficeStrategy
from .docx import DocxStrategy
from .ppt import PptStrategy
from .xlsx import XlsxStrategy

_PPT_STRATEGY = PptStrategy()
_DOCX_STRATEGY = DocxStrategy()
_XLSX_STRATEGY = XlsxStrategy()
_DEFAULT_STRATEGY = DefaultOfficeStrategy()


def get_strategy_for_format(format_name: str, *, operation: str = "") -> OfficeFormatStrategy:
    normalized = str(format_name or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    if normalized == "pptx" and normalized_operation in {"", "create", "transform", "edit", "inspect"}:
        return _PPT_STRATEGY
    if normalized == "docx":
        return _DOCX_STRATEGY
    if normalized == "xlsx":
        return _XLSX_STRATEGY
    return _DEFAULT_STRATEGY

__all__ = [
    "get_strategy_for_format",
    "OfficeFormatStrategy",
    "PptStrategy",
    "DocxStrategy",
    "XlsxStrategy",
    "DefaultOfficeStrategy",
]
