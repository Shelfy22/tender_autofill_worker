from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.product_matching_evaluation import (  # noqa: E402
    evaluate_product_matching,
    load_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate tender product matching against an offline golden dataset."
    )
    parser.add_argument("--gold", required=True, type=Path, help="Golden JSONL file")
    parser.add_argument(
        "--predictions",
        required=True,
        type=Path,
        help="Tender worker result JSON file",
    )
    parser.add_argument("--output", type=Path, help="Optional metrics JSON output")
    args = parser.parse_args()

    golden = load_jsonl(args.gold)
    with args.predictions.open("r", encoding="utf-8-sig") as stream:
        predictions = json.load(stream)
    report = evaluate_product_matching(golden, predictions)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
