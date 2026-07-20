#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
OPTIONLIB_PATH = ROOT / "references" / "optionlib.md"
EVALS_PATH = ROOT / "evals" / "evals.json"
RESULT_DIR = ROOT / "evals" / "model_results"
CONFIG_PATH = ROOT / "evals" / "model_config.json"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    }
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(read_text(CONFIG_PATH))
    return {}


def resolve_provider(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    config = load_config()
    providers = dict(DEFAULT_PROVIDERS)
    providers.update(config.get("providers", {}))
    provider_name = args.provider or config.get("default_provider") or DEFAULT_PROVIDER
    if provider_name not in providers:
        available = ", ".join(sorted(providers))
        raise SystemExit(f"未知provider:{provider_name};可选:{available}")
    provider = dict(providers[provider_name])
    provider["model"] = args.model or provider.get("model") or DEFAULT_PROVIDERS["deepseek"]["model"]
    provider["base_url"] = provider.get("base_url") or DEFAULT_PROVIDERS["deepseek"]["base_url"]
    return provider_name, provider


def load_api_key(provider_name: str, provider: dict[str, Any]) -> str:
    env_name = str(provider.get("api_key_env", "")).strip()
    key = os.environ.get(env_name, "").strip() if env_name else ""
    if key:
        return key
    key = str(provider.get("api_key", "")).strip()
    if key:
        return key
    key = getpass.getpass(f"请输入{provider_name} API Key: ").strip()
    if not key:
        raise SystemExit(f"未提供{provider_name} API Key")
    return key


def build_system_prompt(use_skill: bool) -> str:
    base = (
        "你是专业量化金融衍生品分析助手。回答必须简洁、准确、可核验。"
        "除非用户要求真实行情报价，否则不要编造市场价格、波动率或权利金。"
    )
    if not use_skill:
        return base

    skill = read_text(SKILL_PATH)
    optionlib = read_text(OPTIONLIB_PATH)
    return (
        f"{base}\n\n"
        "下面是必须遵守的本地skill说明。你需要按其中的工作流、栏目顺序和写作规则回答。\n\n"
        "=== SKILL.md ===\n"
        f"{skill}\n\n"
        "=== references/optionlib.md ===\n"
        f"{optionlib}\n"
    )


def call_model(
    *,
    api_key: str,
    base_url: str,
    messages: list[dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        base_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"模型请求失败:HTTP {exc.code}\n{detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"模型网络请求失败:{exc}") from exc

    obj = json.loads(raw)
    return obj["choices"][0]["message"]["content"].strip()


def load_evals() -> list[dict[str, Any]]:
    data = json.loads(read_text(EVALS_PATH))
    return data["evals"]


def parse_ids(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def select_evals(ids: str | None, limit: int | None) -> list[dict[str, Any]]:
    items = load_evals()
    wanted = parse_ids(ids)
    if wanted is not None:
        items = [item for item in items if int(item["id"]) in wanted]
    if limit is not None:
        items = items[:limit]
    if not items:
        raise SystemExit("没有匹配的测试案例")
    return items


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def expected_structures(item: dict[str, Any]) -> list[str]:
    answer = item.get("answer", {})
    return unique_texts(
        [item.get("expected_label", "")]
        + item.get("acceptable_labels", [])
        + [answer.get("primary_structure", "")]
        + answer.get("acceptable_structures", [])
    )


def find_structure_match(answer: str, item: dict[str, Any]) -> tuple[bool, str]:
    candidates = expected_structures(item)
    normalized_answer = normalize_text(answer)
    for label in candidates:
        if normalize_text(label) in normalized_answer:
            return True, label
    return False, ""


def final_structure_result(
    rule_hit: bool,
    rule_matched: str,
    structured_score: dict[str, Any],
    scoring_enabled: bool,
) -> tuple[bool, str, str]:
    scored_hit = structured_score.get("structure_hit")
    if isinstance(scored_hit, bool):
        return scored_hit, str(structured_score.get("matched_label", "") or rule_matched), "structured_score"
    if scoring_enabled:
        return rule_hit, rule_matched, "rule_fallback"
    return rule_hit, rule_matched, "rule_screen"


def source_label(hit_source: str) -> str:
    labels = {
        "structured_score": "结构化评分",
        "rule_fallback": "规则初筛兜底",
        "rule_screen": "规则初筛",
    }
    return labels.get(hit_source, hit_source)


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def human_review_status(
    *,
    final_hit: bool,
    hit_source: str,
    rule_hit: bool,
    structured_score: dict[str, Any],
    error_text: str,
    review_threshold: int,
) -> tuple[bool, str]:
    reasons: list[str] = []
    scored_hit = structured_score.get("structure_hit")
    score_value = int_or_none(structured_score.get("score"))

    if error_text:
        reasons.append("模型调用失败")
    if hit_source != "structured_score":
        reasons.append("未获得结构化评分结果")
    if isinstance(scored_hit, bool) and scored_hit != rule_hit:
        reasons.append("规则初筛与结构化评分不一致")
    if structured_score.get("needs_human_review") is True:
        reasons.append(str(structured_score.get("review_reason", "") or "结构化评分建议人工复核"))
    if score_value is not None and score_value < review_threshold:
        reasons.append(f"评分低于{review_threshold}")
    if not final_hit:
        reasons.append("最终结构未命中")

    return bool(reasons), "；".join(unique_texts(reasons))


def rule_point_hits(answer: str, item: dict[str, Any]) -> tuple[int, int, list[str]]:
    points = item.get("answer", {}).get("required_points", [])
    normalized_answer = normalize_text(answer)
    hits = []
    for point in points:
        if normalize_text(point) in normalized_answer:
            hits.append(point)
    return len(hits), len(points), hits


def extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def score_answer(
    *,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    timeout: int,
    item: dict[str, Any],
    answer: str,
) -> dict[str, Any]:
    required_points = item.get("answer", {}).get("required_points", [])
    prompt = {
        "测试问题": item["prompt"],
        "标准结构": item["expected_label"],
        "可接受结构": expected_structures(item),
        "必须命中要点": required_points,
        "标准答案说明": item.get("expected_output", ""),
        "模型回答": answer,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是期权结构测试评分员。只根据给定标准评分,不要引入外部标准。"
                "你的任务是判断模型回答的主推荐结构是否正确,不是判断回答里是否出现过标准词。"
                "如果正确结构只出现在替代结构、风险说明、反向比较或否定句中,structure_hit必须为false。"
                "同义表达可以判为命中,例如备兑开仓等同于备兑看涨,但必须确认构造为持有现货并卖出看涨。"
                "垂直价差必须按构造判断:卖出较高行权价看跌并买入较低行权价看跌是牛市看跌价差;"
                "买入较高行权价看跌并卖出较低行权价看跌是熊市看跌价差。"
                "障碍结构必须按障碍方向判断:向上表示触及上方障碍,向下表示触及下方障碍;"
                "敲入表示触及后生效,敲出表示触及后失效。"
                "输出严格JSON,字段为structure_hit、matched_label、primary_structure、"
                "required_points_hit、required_points_total、missing_points、format_hit、risk_hit、"
                "score、confidence、needs_human_review、review_reason、comment。"
                "score为0到100的整数,confidence为0到1的小数。"
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    raw = call_model(
        api_key=api_key,
        base_url=base_url,
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=1200,
        timeout=timeout,
    )
    try:
        return extract_json(raw)
    except Exception:
        return {"raw_score_output": raw}


def run_one(
    *,
    api_key: str,
    base_url: str,
    prompt: str,
    use_skill: bool,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> str:
    messages = [
        {"role": "system", "content": build_system_prompt(use_skill)},
        {"role": "user", "content": prompt},
    ]
    return call_model(
        api_key=api_key,
        base_url=base_url,
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def command_ask(args: argparse.Namespace) -> None:
    provider_name, provider = resolve_provider(args)
    api_key = load_api_key(provider_name, provider)
    eval_item = None
    if args.eval_id is not None:
        matches = [item for item in load_evals() if int(item["id"]) == args.eval_id]
        if not matches:
            raise SystemExit(f"找不到eval_id={args.eval_id}")
        eval_item = matches[0]
        prompt = eval_item["prompt"]
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = input("请输入问题:").strip()
        if not prompt:
            raise SystemExit("问题为空")

    answer = run_one(
        api_key=api_key,
        base_url=provider["base_url"],
        prompt=prompt,
        use_skill=not args.no_skill,
        model=provider["model"],
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    print(f"\n===== {provider_name}:{provider['model']}回答 =====\n")
    print(answer)

    if eval_item:
        hit, matched = find_structure_match(answer, eval_item)
        point_hit, point_total, rule_hits = rule_point_hits(answer, eval_item)
        print("\n===== 标准答案对比 =====\n")
        print(f"标准结构:{eval_item['expected_label']}")
        print(f"可接受结构:{', '.join(eval_item.get('acceptable_labels', [])) or '无'}")
        print(f"规则初筛要点命中:{point_hit}/{point_total}")
        if rule_hits:
            print("规则初筛命中要点:")
            for point in rule_hits:
                print(f"- {point}")
        print("\n标准说明:")
        print(eval_item.get("expected_output", ""))

        if args.score:
            structured_score = score_answer(
                api_key=api_key,
                base_url=provider["base_url"],
                model=provider["model"],
                temperature=0,
                timeout=args.timeout,
                item=eval_item,
                answer=answer,
            )
            final_hit, final_matched, hit_source = final_structure_result(
                hit,
                matched,
                structured_score,
                scoring_enabled=True,
            )
            needs_review, review_reason = human_review_status(
                final_hit=final_hit,
                hit_source=hit_source,
                rule_hit=hit,
                structured_score=structured_score,
                error_text="",
                review_threshold=args.review_threshold,
            )
            print(f"结构命中:{'是' if final_hit else '否'}{f'({final_matched})' if final_matched else ''};判定来源:{source_label(hit_source)}")
            print(f"人工复核:{'是' if needs_review else '否'}{f'；原因:{review_reason}' if review_reason else ''}")
            print(f"\n===== {provider_name}:{provider['model']}评分 =====\n")
            print(json.dumps(structured_score, ensure_ascii=False, indent=2))
        else:
            print(f"结构命中:{'是' if hit else '否'}{f'({matched})' if matched else ''};判定来源:{source_label('rule_screen')}")
            print("人工复核:是；原因:未启用结构化评分,仅为规则初筛")


def safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "-", text).strip("-") or "model"


def write_batch_outputs(rows: list[dict[str, Any]], provider_name: str, model: str) -> tuple[Path, Path, Path]:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{safe_name(provider_name)}_{safe_name(model)}"
    csv_path = RESULT_DIR / f"{prefix}_eval_{stamp}.csv"
    jsonl_path = RESULT_DIR / f"{prefix}_eval_{stamp}.jsonl"
    review_path = RESULT_DIR / f"{prefix}_manual_review_{stamp}.csv"

    fieldnames = [
        "provider",
        "model",
        "id",
        "category",
        "label",
        "mode",
        "expected_label",
        "acceptable_labels",
        "matched_label",
        "structure_hit",
        "final_hit_source",
        "rule_matched_label",
        "rule_structure_hit",
        "manual_review",
        "manual_review_reason",
        "structured_confidence",
        "structured_primary_structure",
        "structured_missing_points",
        "required_points_hit_rule",
        "required_points_total",
        "structured_score",
        "structured_comment",
        "prompt",
        "answer",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    review_rows = [row for row in rows if row.get("manual_review") == "是"]
    with review_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in review_rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})

    return csv_path, jsonl_path, review_path


def summarize(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["mode"], row["category"])
        groups.setdefault(key, []).append(row)

    print("\n===== 汇总 =====\n")
    for (mode, category), items in sorted(groups.items()):
        hit_count = sum(1 for row in items if row.get("structure_hit") == "是")
        total = len(items)
        print(f"{mode} | {category}:结构命中{hit_count}/{total},{hit_count / total:.1%}")

    review_rows = [row for row in rows if row.get("manual_review") == "是"]
    print("\n===== 人工复核 =====\n")
    print(f"需要人工复核:{len(review_rows)}/{len(rows)}")
    for row in review_rows[:20]:
        print(
            f"{row['mode']} | id={row['id']} | {row['category']} | "
            f"结构命中={row['structure_hit']} | 原因:{row.get('manual_review_reason', '')}"
        )
    if len(review_rows) > 20:
        print(f"其余{len(review_rows) - 20}条见人工复核文件")


def command_eval(args: argparse.Namespace) -> None:
    provider_name, provider = resolve_provider(args)
    api_key = load_api_key(provider_name, provider)
    items = select_evals(args.ids, args.limit)
    modes = ["with_skill", "without_skill"] if args.mode == "both" else [args.mode]
    rows: list[dict[str, Any]] = []

    for item in items:
        for mode in modes:
            use_skill = mode == "with_skill"
            print(f"运行:id={item['id']} category={item['category']} mode={mode}")
            started = time.time()
            try:
                answer = run_one(
                    api_key=api_key,
                    base_url=provider["base_url"],
                    prompt=item["prompt"],
                    use_skill=use_skill,
                    model=provider["model"],
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                error_text = ""
            except Exception as exc:
                answer = ""
                error_text = str(exc)

            hit, matched = find_structure_match(answer, item)
            point_hit, point_total, rule_hits = rule_point_hits(answer, item)
            structured_score: dict[str, Any] = {}
            if args.score and answer:
                try:
                    structured_score = score_answer(
                        api_key=api_key,
                        base_url=provider["base_url"],
                        model=provider["model"],
                        temperature=0,
                        timeout=args.timeout,
                        item=item,
                        answer=answer,
                    )
                except Exception as exc:
                    structured_score = {"score_error": str(exc)}
            final_hit, final_matched, hit_source = final_structure_result(
                hit,
                matched,
                structured_score,
                scoring_enabled=args.score,
            )
            needs_review, review_reason = human_review_status(
                final_hit=final_hit,
                hit_source=hit_source,
                rule_hit=hit,
                structured_score=structured_score,
                error_text=error_text,
                review_threshold=args.review_threshold,
            )

            row = {
                "provider": provider_name,
                "model": provider["model"],
                "id": item["id"],
                "category": item["category"],
                "label": item["label"],
                "mode": mode,
                "expected_label": item["expected_label"],
                "acceptable_labels": ",".join(item.get("acceptable_labels", [])),
                "matched_label": final_matched,
                "structure_hit": "是" if final_hit else "否",
                "final_hit_source": source_label(hit_source),
                "rule_matched_label": matched,
                "rule_structure_hit": "是" if hit else "否",
                "manual_review": "是" if needs_review else "否",
                "manual_review_reason": review_reason,
                "structured_confidence": structured_score.get("confidence", ""),
                "structured_primary_structure": structured_score.get("primary_structure", ""),
                "structured_missing_points": "；".join(structured_score.get("missing_points", []))
                if isinstance(structured_score.get("missing_points"), list)
                else structured_score.get("missing_points", ""),
                "required_points_hit_rule": point_hit,
                "required_points_total": point_total,
                "required_points_rule_hits": rule_hits,
                "structured_evaluation": structured_score,
                "structured_score": structured_score.get("score", ""),
                "structured_comment": structured_score.get("comment", ""),
                "prompt": item["prompt"],
                "answer": answer,
                "error": error_text,
                "elapsed_seconds": round(time.time() - started, 2),
            }
            rows.append(row)

            status = "命中" if final_hit else "未命中"
            print(
                f"完成:id={item['id']} mode={mode} 结构{status} "
                f"判定来源{source_label(hit_source)} 人工复核{row['manual_review']} 用时{row['elapsed_seconds']}秒"
            )

    csv_path, jsonl_path, review_path = write_batch_outputs(rows, provider_name, provider["model"])
    summarize(rows)
    print(f"\nCSV结果:{csv_path}")
    print(f"JSONL明细:{jsonl_path}")
    print(f"人工复核:{review_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多模型测试OptionHelper skill")
    parser.add_argument("--provider", help="模型供应商配置名,默认读取evals/model_config.json")
    parser.add_argument("--model", help="覆盖provider配置中的模型名")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--review-threshold", type=int, default=80, help="结构化评分低于该值时标记人工复核")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="手动提问,或指定eval-id提问")
    ask.add_argument("prompt", nargs="?", help="要发送给模型的问题")
    ask.add_argument("--eval-id", type=int, help="使用evals/evals.json中的测试问题")
    ask.add_argument("--no-skill", action="store_true", help="不注入SKILL.md和reference")
    ask.add_argument("--score", action="store_true", help="对eval-id结果做结构化评分")
    ask.set_defaults(func=command_ask)

    ev = sub.add_parser("eval", help="批量运行evals/evals.json")
    ev.add_argument("--ids", help="只运行指定id,例如1,5,14")
    ev.add_argument("--limit", type=int, help="只运行前N条")
    ev.add_argument(
        "--mode",
        choices=["with_skill", "without_skill", "both"],
        default="with_skill",
        help="默认只测with_skill;both会额外跑无skill基线",
    )
    ev.add_argument("--score", action="store_true", help="额外调用当前模型做结构化评分")
    ev.set_defaults(func=command_eval)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
