"""The same product matching stages used by both service entry points."""

from dataclasses import dataclass
from typing import Any

from app.models import ProductMatchItem, TenderPosition
from app.services.analysis_pipeline import direct_stage
from app.services.catalog import limit_catalog_positions
from app.services.product_characteristics import finalize_position_characteristics
from app.services.product_search_query import enrich_product_search_queries
from app.services.product_validation import validate_final_product_matches, validate_product_candidates


@dataclass
class ProductProcessingResult:
    positions: list[TenderPosition]
    matches: list[ProductMatchItem]
    characteristics: dict[str, Any]
    validation: dict[str, Any]
    search: dict[str, Any]
    final_validation: dict[str, Any]


def process_products(
    llm, catalog, settings, positions, warnings, *,
    run_stage=direct_stage, incomplete_unit_ids=None,
) -> ProductProcessingResult:
    positions, added, characteristics = run_stage(
        "Validate Product Characteristic Associations",
        lambda: finalize_position_characteristics(positions),
    )
    warnings.extend(added)
    candidates, added = limit_catalog_positions(positions, settings.catalog_match_max_positions)
    warnings.extend(added)
    candidates, added, validation = run_stage(
        "Validate Product Candidates", lambda: validate_product_candidates(llm, candidates),
    )
    warnings.extend(added)
    validation["sourcePositionCount"] = len(positions)
    validation["catalogPositionLimit"] = settings.catalog_match_max_positions
    if incomplete_unit_ids is not None:
        validation["consolidationIncompleteUnitIds"] = list(incomplete_unit_ids)
        validation["requiresManualReview"] = bool(validation.get("requiresManualReview") or incomplete_unit_ids)
    candidates, added, search = run_stage(
        "Build Semantic Product Search Queries",
        lambda: enrich_product_search_queries(
            llm, candidates, batch_size=settings.product_characteristic_batch_size,
        ),
    )
    warnings.extend(added)
    matches, added = run_stage("Поиск товаров в каталоге/Qdrant", lambda: catalog.match_all(candidates))
    warnings.extend(added)
    matches, added, final_validation = run_stage(
        "Validate Final Product Rows", lambda: validate_final_product_matches(llm, matches),
    )
    warnings.extend(added)
    return ProductProcessingResult(positions, matches, characteristics, validation, search, final_validation)
