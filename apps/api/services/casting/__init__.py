"""铸造域服务包。"""

from api.services.casting.drawing_match import match_drawing
from api.services.casting.yield_analysis import (
    analyze_yield,
    analyze_yield_by_inventory_guid,
    generate_yield_document,
    search_inventories,
)
from api.services.casting.peel_report import query_peel_report

__all__ = [
    "analyze_yield",
    "analyze_yield_by_inventory_guid",
    "generate_yield_document",
    "search_inventories",
    "match_drawing",
    "query_peel_report",
]
