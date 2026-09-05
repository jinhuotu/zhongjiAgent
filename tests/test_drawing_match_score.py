"""图纸匹配打分单测（无 DB）。"""

from __future__ import annotations

from api.services.casting.drawing_match import normalize_extracted, score_inventory_row


def test_score_prefers_matching_sizes() -> None:
    extracted = normalize_extracted(
        {
            "dims": [
                {"label": "H", "value": 1400},
                {"label": "A", "value": 420},
                {"label": "B", "value": 220},
            ],
            "keywords": ["brick"],
            "schemeText": "scheme-2",
        }
    )
    hit = score_inventory_row(
        {
            "InventoryGUID": "g1",
            "InventoryCode": "000-00224",
            "InventoryName": "brick",
            "InventorySpecification": "1400x420x220",
            "SizeA": 420,
            "SizeB": 220,
            "SizeH": 1400,
        },
        extracted=extracted,
    )
    miss = score_inventory_row(
        {
            "InventoryGUID": "g2",
            "InventoryCode": "000-00001",
            "InventoryName": "brick",
            "InventorySpecification": "115x418x600",
            "SizeA": 418,
            "SizeB": 600,
            "SizeH": 115,
        },
        extracted=extracted,
    )
    assert hit["similarity"] > miss["similarity"]
    assert hit["similarity"] >= 70


def test_scheme_drawing_does_not_penalize_extra_800_and_phi50() -> None:
    """方案图含 800/Φ50 等工艺尺寸时，仍应对 1400×420×220 成品给高分。"""
    extracted = normalize_extracted(
        {
            "dims": [
                {"label": "W", "value": 800},
                {"label": "H1", "value": 800},
                {"label": "H", "value": 1400},
                {"label": "B", "value": 220},
            ],
            "diameters": [50],
            "keywords": ["方案一", "砂型"],
            "schemeText": "方案一：两块独立砂型底部开两个直径50孔连体浇铸，1400高",
        }
    )
    hit = score_inventory_row(
        {
            "InventoryGUID": "g1",
            "InventoryCode": "000-00224",
            "InventoryName": "直型砖",
            "InventorySpecification": "1400×420×220",
            "SizeA": 420,
            "SizeB": 220,
            "SizeH": 1400,
        },
        extracted=extracted,
    )
    # 命中成品 H=1400、B=220（图上无 420），旧算法约 27.5%
    assert hit["similarity"] >= 60
    assert any("1400" in r for r in (hit.get("matchReasons") or []))
