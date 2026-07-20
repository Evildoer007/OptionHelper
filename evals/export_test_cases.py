#!/usr/bin/env python3
from pathlib import Path
import collections
import json


ROOT = Path(__file__).resolve().parents[1]
EVALS_PATH = ROOT / "evals" / "evals.json"
OUTPUT_PATH = ROOT / "evals" / "test_cases.md"


def main() -> None:
    data = json.loads(EVALS_PATH.read_text(encoding="utf-8"))
    evals = data["evals"]
    categories = collections.Counter(item["category"] for item in evals)

    lines = [
        "# OptionHelper定性推荐测试集",
        "",
        "用途:用于测试模型能否根据口语化行情观点推荐合适的期权结构。",
        "",
        "## 评分口径",
        "",
        "- 先看推荐结构是否命中标准答案或可接受答案。",
        "- 再看回答是否覆盖必须命中要点。",
        "- 不要求真实行情数据、权利金报价或数值计算。",
        "- 用户输入保持口语化,答案部分用于人工核对。",
        "",
        "## 覆盖范围",
        "",
    ]

    for category, count in categories.items():
        lines.append(f"- {category}:{count}条")

    lines.extend(["", f"合计:{len(evals)}条", "", "## 测试案例", ""])

    for item in evals:
        answer = item.get("answer", {})
        lines.extend(
            [
                f"### {item['id']}.{item['category']} | {item['label']}",
                "",
                "用户输入:",
                "",
                item["prompt"],
                "",
                f"标准答案:{item['expected_label']}",
                "",
                f"可接受答案:{', '.join(item.get('acceptable_labels', [])) or '无'}",
                "",
                "必须命中要点:",
            ]
        )
        for point in answer.get("required_points", []):
            lines.append(f"- {point}")
        lines.extend(["", "答案说明:", "", item.get("expected_output", ""), ""])

    OUTPUT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
