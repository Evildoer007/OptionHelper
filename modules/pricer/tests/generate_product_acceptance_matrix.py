"""Regenerate the checked-in Pricer 65-product acceptance snapshots."""

from __future__ import annotations

import csv
import argparse
import json
from pathlib import Path

from runtime.contracts.contract_api import load_registry

from .product_acceptance_matrix_builder import build_acceptance_rows, csv_fieldnames, csv_rows


FIXTURE_DIR = Path(__file__).with_name("fixtures")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-id", action="append", default=[])
    parser.add_argument("--output-json")
    parser.add_argument("--combine-json", nargs="+")
    args = parser.parse_args()
    if args.combine_json:
        rows = _combine_rows(args.combine_json)
    else:
        rows = build_acceptance_rows(args.product_id or None)
    if args.output_json:
        Path(args.output_json).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    if len(rows) != 65:
        raise ValueError("写入正式65行验收矩阵前必须提供全部65个产品结果")
    _write_fixtures(rows)


def _combine_rows(paths: list[str]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for raw_path in paths:
        for row in json.loads(Path(raw_path).read_text(encoding="utf-8")):
            product_id = str(row["product_id"])
            if product_id in merged:
                raise ValueError(f"重复产品验收结果：{product_id}")
            merged[product_id] = row
    expected = [str(product_id) for product_id, product in load_registry()["products"].items() if product["identity"]["entry_status"]]
    if set(merged) != set(expected):
        raise ValueError("合并结果没有逐一覆盖65个entry_status=True产品")
    return [merged[product_id] for product_id in expected]


def _write_fixtures(rows: list[dict[str, object]]) -> None:
    (FIXTURE_DIR / "product_acceptance_matrix.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    with (FIXTURE_DIR / "product_acceptance_matrix.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=csv_fieldnames())
        writer.writeheader()
        writer.writerows(csv_rows(rows))


if __name__ == "__main__":
    main()
