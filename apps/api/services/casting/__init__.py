"""铸造域服务包。"""

from api.services.casting.yield_analysis import (
    analyze_yield,
    analyze_yield_by_inventory_guid,
    generate_yield_document,
    search_inventories,
)

__all__ = [
    "analyze_yield",
    "analyze_yield_by_inventory_guid",
    "generate_yield_document",
    "search_inventories",
]
