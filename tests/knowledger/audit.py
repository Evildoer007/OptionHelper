"""OptionList、OptionLib与OptionReg的只读一致性审计。

本工具属于开发期Knowledger专项测试，不是运行时合同解释器。它只读取三份
资料库，输出结构化差异；候选流程同样只比较临时候选，不自动修改资料库。
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from runpy import run_path
import sys
from typing import Any, Iterable, Mapping, Sequence


PAYOFF_HEADER = (
    "路径", "判断条件", "分段条件", "定义域", "情景解释", "持有方净损益", "对手方净损益",
)
EXAMPLE_HEADER = (
    "标的数值", "路径", "判断条件", "分段条件", "定义域", "持有方净损益", "对手方净损益",
)
SECTION_TITLES = {
    "1": "条款要素",
    "2": "损益结构",
    "3": "默认示例",
    "4": "适用场景",
    "5": "产品总结",
}
RULE_TERM_KEYS = {"pricing_methods", "monitor", "constraints", "derived_terms"}
NON_MATH_TERM_KEYS = {
    "observation_price", "margin_call", "exercise_style", "settlement", "event_priority",
    "hedge_ki_history_policy", "payoff_figure_basis",
}
FORBIDDEN_REG_KEYS = {
    "pricing_config", "backtest_config", "pricing_spec", "backtest_spec", "payoff_spec",
    "default_payoff_input", "input_fields", "market_data", "page_state", "result",
}
REG_PRODUCT_KEYS = {"identity", "terms", "paths"}
REG_IDENTITY_KEYS = {"product_id", "name_zh", "entry_status"}
REG_PATH_KEYS = {"condition", "cases"}
REG_CASE_KEYS = {"domain", "pnl"}
REG_TERM_DEFINITION_KEYS = {"name_zh", "symbol", "value_type", "unit", "domain"}


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    category: str
    message: str
    source: str
    line: int | None = None
    product_id: str | None = None
    field: str | None = None
    expected: Any = None
    actual: Any = None
    evidence: str | None = None

    @property
    def blocks_regular(self) -> bool:
        return self.severity == "error"

    @property
    def blocks_release(self) -> bool:
        return self.severity in {"error", "release_blocker"}


@dataclass
class AuditReport:
    root: str
    mode: str
    counts: dict[str, int]
    issues: list[Issue] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)

    def exit_code(self, *, strict_release: bool = False) -> int:
        predicate = (lambda issue: issue.blocks_release) if strict_release else (lambda issue: issue.blocks_regular)
        return 1 if any(predicate(issue) for issue in self.issues) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "mode": self.mode,
            "counts": self.counts,
            "coverage": self.coverage,
            "changes": self.changes,
            "summary": {
                "errors": sum(issue.severity == "error" for issue in self.issues),
                "release_blockers": sum(issue.severity == "release_blocker" for issue in self.issues),
                "reviews": sum(issue.severity == "review" for issue in self.issues),
            },
            "issues": [asdict(issue) for issue in self.issues],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        payload = self.to_dict()
        summary = payload["summary"]
        lines = [
            "# Knowledger只读审计报告",
            "",
            f"- 模式：{self.mode}",
            f"- 根目录：`{self.root}`",
            f"- 产品数：OptionList={self.counts.get('optionlist', 0)}，OptionLib={self.counts.get('optionlib', 0)}，OptionReg={self.counts.get('optionreg', 0)}",
            f"- 高置信错误：{summary['errors']}项",
            f"- 严格发布阻断：{summary['release_blockers']}项",
            f"- 人工复核：{summary['reviews']}项",
            "",
        ]
        if self.changes:
            lines.extend(["## 候选变化", "", "```json", json.dumps(self.changes, ensure_ascii=False, indent=2, sort_keys=True), "```", ""])
        if self.coverage:
            lines.extend([
                "## 覆盖边界", "",
                "```json", json.dumps(self.coverage, ensure_ascii=False, indent=2, sort_keys=True), "```", "",
                "高置信错误为0仅表示上述机器规则未发现冲突，不代表65个产品的观察日程、约束和现金流经济语义已被自动证明等价。",
                "",
            ])
        lines.extend(["## 差异明细", ""])
        if not self.issues:
            lines.append("未发现差异。")
            return "\n".join(lines) + "\n"
        lines.extend([
            "| 级别 | 类别 | 编号 | 产品 | 来源 | 字段 | 说明 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        for issue in self.issues:
            source = issue.source + (f":{issue.line}" if issue.line else "")
            values = [
                issue.severity, issue.category, issue.code, issue.product_id or "-", source,
                issue.field or "-", issue.message,
            ]
            lines.append("| " + " | ".join(_escape_markdown(value) for value in values) + " |")
            if issue.expected is not None or issue.actual is not None or issue.evidence:
                details = []
                if issue.expected is not None:
                    details.append(f"期望={issue.expected!r}")
                if issue.actual is not None:
                    details.append(f"实际={issue.actual!r}")
                if issue.evidence:
                    details.append(f"依据={issue.evidence}")
                lines.append(f"|  |  |  |  |  |  | {_escape_markdown('；'.join(details))} |")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ListProduct:
    sequence: int
    name: str
    product_id: str
    category: str
    status: str
    line: int


@dataclass
class LibraryProduct:
    product_id: str
    name: str
    category: str
    line: int
    raw: str
    sections: dict[str, tuple[str, str, int]]
    payoff_rows: list[tuple[list[str], int]]
    example_rows: list[tuple[list[str], int]]
    example_data: tuple[str, int] | None


@dataclass
class RepositorySnapshot:
    root: Path
    list_products: list[ListProduct]
    lib_products: list[LibraryProduct]
    registry: dict[str, Any]
    registry_text: str
    report: AuditReport


def _escape_markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _read_optionlist(path: Path, issues: list[Issue]) -> list[ListProduct]:
    text = path.read_text(encoding="utf-8")
    products: list[ListProduct] = []
    pattern = re.compile(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(\d+\.\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$")
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = pattern.match(line)
        if match:
            products.append(ListProduct(int(match[1]), match[2], match[3], match[4], match[5], line_number))
    if not products:
        issues.append(Issue("list.no_products", "error", "structure", "OptionList未解析出产品表", str(path), 1))
    return products


def _extract_table(
    section_text: str,
    section_start_line: int,
    expected_header: Sequence[str],
    source: Path,
    product_id: str,
    issues: list[Issue],
) -> list[tuple[list[str], int]]:
    lines = section_text.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        if line.startswith("|") and tuple(_split_markdown_row(line)) == tuple(expected_header):
            header_index = index
            break
    if header_index is None:
        issues.append(Issue(
            "lib.table_header", "error", "structure", "未找到标准七列表头", str(source), section_start_line,
            product_id=product_id, expected=list(expected_header),
        ))
        return []
    if header_index + 1 >= len(lines) or not _is_separator_row(_split_markdown_row(lines[header_index + 1])):
        issues.append(Issue(
            "lib.table_separator", "error", "structure", "标准表头后缺少Markdown分隔行", str(source),
            section_start_line + header_index + 1, product_id=product_id,
        ))
        return []
    rows: list[tuple[list[str], int]] = []
    for index in range(header_index + 2, len(lines)):
        line = lines[index]
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = _split_markdown_row(line)
        row_line = section_start_line + index
        if len(cells) != len(expected_header):
            issues.append(Issue(
                "lib.table_width", "error", "structure", "表格数据行不是七列", str(source), row_line,
                product_id=product_id, expected=len(expected_header), actual=len(cells), evidence=line,
            ))
            continue
        rows.append((cells, row_line))
    if not rows:
        issues.append(Issue(
            "lib.table_empty", "error", "structure", "标准表格没有数据行", str(source), section_start_line,
            product_id=product_id,
        ))
    return rows


def _read_optionlib(path: Path, issues: list[Issue]) -> tuple[list[LibraryProduct], str]:
    text = path.read_text(encoding="utf-8")
    categories = list(re.finditer(r"^##\s+\d+\s+(.+?)\s*$", text, re.MULTILINE))
    headings = list(re.finditer(r"^###\s+(\d+\.\d+)\s+(.+?)\s*$", text, re.MULTILINE))
    products: list[LibraryProduct] = []
    for index, match in enumerate(headings):
        product_id = match[1]
        if int(product_id.split(".", 1)[0]) < 2:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        raw = text[match.end():end]
        start_line = _line_number(text, match.start())
        subheadings = list(re.finditer(
            rf"^####\s+{re.escape(product_id)}\.(\d)\s+(.+?)\s*$", raw, re.MULTILINE,
        ))
        sections: dict[str, tuple[str, str, int]] = {}
        for sub_index, sub_match in enumerate(subheadings):
            key = sub_match[1]
            sub_end = subheadings[sub_index + 1].start() if sub_index + 1 < len(subheadings) else len(raw)
            sub_line = start_line + raw.count("\n", 0, sub_match.start()) + 1
            sections[key] = (sub_match[2], raw[sub_match.end():sub_end], sub_line)
        for key, expected_title in SECTION_TITLES.items():
            if key not in sections:
                issues.append(Issue(
                    "lib.section_missing", "error", "structure", f"缺少{product_id}.{key}{expected_title}",
                    str(path), start_line, product_id=product_id,
                ))
            elif sections[key][0] != expected_title:
                issues.append(Issue(
                    "lib.section_title", "error", "structure", "产品小节标题不符合固定名称", str(path),
                    sections[key][2], product_id=product_id, expected=expected_title, actual=sections[key][0],
                ))
        payoff_rows: list[tuple[list[str], int]] = []
        example_rows: list[tuple[list[str], int]] = []
        example_data = None
        if "2" in sections:
            payoff_rows = _extract_table(sections["2"][1], sections["2"][2], PAYOFF_HEADER, path, product_id, issues)
        if "3" in sections:
            example_rows = _extract_table(sections["3"][1], sections["3"][2], EXAMPLE_HEADER, path, product_id, issues)
            data_matches = list(re.finditer(r"^示例数据：(.+?)\s*$", sections["3"][1], re.MULTILINE))
            if len(data_matches) != 1:
                issues.append(Issue(
                    "lib.example_data_count", "error", "structure", "默认示例必须且只能有一行示例数据", str(path),
                    sections["3"][2], product_id=product_id, expected=1, actual=len(data_matches),
                ))
            else:
                data_match = data_matches[0]
                data_line = sections["3"][2] + sections["3"][1].count("\n", 0, data_match.start())
                example_data = (data_match[1], data_line)
        category = next((item[1] for item in reversed(categories) if item.start() < match.start()), "")
        products.append(LibraryProduct(
            product_id, match[2], category, start_line, raw, sections, payoff_rows, example_rows, example_data,
        ))
    if not products:
        issues.append(Issue("lib.no_products", "error", "structure", "OptionLib未解析出产品章节", str(path), 1))
    return products, text


def _load_optionreg(path: Path, issues: list[Issue]) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    _scan_duplicate_literal_keys(path, text, issues)
    try:
        registry = run_path(str(path)).get("REGISTRY")
    except Exception as error:  # noqa: BLE001 - 审计报告必须保留任何加载失败
        issues.append(Issue(
            "reg.load_failed", "error", "structure", f"OptionReg无法加载：{type(error).__name__}: {error}",
            str(path), 1,
        ))
        return {}, text
    if not isinstance(registry, Mapping):
        issues.append(Issue("reg.not_mapping", "error", "structure", "REGISTRY必须为映射", str(path), 1))
        return {}, text
    return dict(registry), text


def _scan_duplicate_literal_keys(path: Path, text: str, issues: list[Issue]) -> None:
    """在执行Python前检查字典字面量重复键，避免run_path静默覆盖。"""
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: dict[object, int] = {}
        for key_node in node.keys:
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, (str, int, float, bool)):
                continue
            key = key_node.value
            if key in seen:
                issues.append(Issue(
                    "reg.duplicate_literal_key", "error", "structure",
                    "OptionReg字典字面量存在重复常量键；Python加载时会静默覆盖前值",
                    str(path), getattr(key_node, "lineno", None), field=str(key),
                    evidence=f"首次定义于第{seen[key]}行",
                ))
            else:
                seen[key] = getattr(key_node, "lineno", 1)


def _product_line(registry_text: str, product_id: str) -> int:
    match = re.search(rf"^\s*['\"]{re.escape(product_id)}['\"]\s*:\s*\{{['\"]identity['\"]", registry_text, re.MULTILINE)
    return _line_number(registry_text, match.start()) if match else 1


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _validate_registry_shape(
    root: Path,
    registry: Mapping[str, Any],
    registry_text: str,
    issues: list[Issue],
) -> None:
    source = root / "references" / "optionreg.py"
    if set(registry) != {"term_catalog", "products"}:
        issues.append(Issue(
            "reg.top_shape", "error", "structure", "OptionReg顶层只能有term_catalog与products", str(source), 1,
            expected=["term_catalog", "products"], actual=sorted(registry),
        ))
        return
    catalog = registry.get("term_catalog")
    products = registry.get("products")
    if not isinstance(catalog, Mapping) or not isinstance(products, Mapping):
        issues.append(Issue("reg.top_types", "error", "structure", "term_catalog与products必须为映射", str(source), 1))
        return
    for key, definition in catalog.items():
        if not isinstance(definition, Mapping) or set(definition) != REG_TERM_DEFINITION_KEYS:
            issues.append(Issue(
                "reg.term_definition_shape", "error", "structure", "term_catalog条目必须严格包含五个字段", str(source),
                product_id=None, field=str(key), expected=sorted(REG_TERM_DEFINITION_KEYS),
                actual=sorted(definition) if isinstance(definition, Mapping) else type(definition).__name__,
            ))
    schedule_selectors: list[str] = []
    for definition in catalog.values():
        if isinstance(definition, Mapping):
            domain = definition.get("domain")
            if isinstance(domain, Mapping) and domain.get("format") == "observation_schedule":
                selectors = domain.get("selectors", [])
                if isinstance(selectors, Sequence) and not isinstance(selectors, (str, bytes)):
                    schedule_selectors.extend(str(item) for item in selectors)
    weekly_selectors = sorted({item for item in schedule_selectors if item.startswith("weekly")})
    if weekly_selectors:
        issues.append(Issue(
            "schedule.weekly_known_migration", "release_blocker", "known_migration",
            "蓝图13.1/13.2已登记weekly为待迁移日程，当前term_catalog仍允许weekly选择器",
            str(source), 21, field="term_catalog.schedule_selector", expected="daily或monthly_*", actual=weekly_selectors,
            evidence="总设计蓝图4.3、13.1、13.2",
        ))
    for product_id, product in products.items():
        line = _product_line(registry_text, str(product_id))
        if not isinstance(product, Mapping) or set(product) != REG_PRODUCT_KEYS:
            issues.append(Issue(
                "reg.product_shape", "error", "structure", "产品块必须严格包含identity、terms、paths", str(source), line,
                product_id=str(product_id), expected=sorted(REG_PRODUCT_KEYS),
                actual=sorted(product) if isinstance(product, Mapping) else type(product).__name__,
            ))
            continue
        identity = product.get("identity")
        terms = product.get("terms")
        paths = product.get("paths")
        if not isinstance(identity, Mapping) or set(identity) != REG_IDENTITY_KEYS:
            issues.append(Issue(
                "reg.identity_shape", "error", "structure", "identity必须严格包含product_id、name_zh、entry_status",
                str(source), line, product_id=str(product_id),
            ))
        else:
            if identity.get("product_id") != product_id:
                issues.append(Issue(
                    "reg.identity_id", "error", "identity", "products外层键与identity.product_id不一致",
                    str(source), line, product_id=str(product_id), expected=product_id, actual=identity.get("product_id"),
                ))
            if not isinstance(identity.get("entry_status"), bool):
                issues.append(Issue(
                    "reg.entry_status_type", "error", "identity", "entry_status必须为布尔值", str(source), line,
                    product_id=str(product_id), actual=type(identity.get("entry_status")).__name__,
                ))
        if not isinstance(terms, Mapping):
            issues.append(Issue("reg.terms_type", "error", "structure", "terms必须为映射", str(source), line, product_id=str(product_id)))
            continue
        forbidden = sorted(set(terms) & FORBIDDEN_REG_KEYS)
        if forbidden:
            issues.append(Issue(
                "reg.module_config_forbidden", "error", "ownership", "OptionReg不得保存模块Config或展示字段",
                str(source), line, product_id=str(product_id), actual=forbidden,
            ))
        unknown_terms = sorted(set(terms) - RULE_TERM_KEYS - set(catalog))
        if unknown_terms:
            issues.append(Issue(
                "reg.term_unknown", "error", "structure", "产品terms含未登记字段", str(source), line,
                product_id=str(product_id), actual=unknown_terms,
            ))
        for key, value in terms.items():
            if isinstance(value, str) and value.startswith("weekly"):
                issues.append(Issue(
                    "schedule.weekly_product_migration", "release_blocker", "known_migration",
                    "产品仍使用蓝图已登记待迁移的weekly观察日程", str(source), line,
                    product_id=str(product_id), field=f"terms.{key}", expected="daily或monthly_*", actual=value,
                    evidence="总设计蓝图13.1、13.2",
                ))
        if not isinstance(paths, list) or not paths:
            issues.append(Issue("reg.paths_type", "error", "structure", "paths必须为非空列表", str(source), line, product_id=str(product_id)))
            continue
        for path_index, path in enumerate(paths, start=1):
            if not isinstance(path, Mapping) or set(path) != REG_PATH_KEYS:
                issues.append(Issue(
                    "reg.path_shape", "error", "structure", "路径必须严格包含condition与cases", str(source), line,
                    product_id=str(product_id), field=f"paths[{path_index}]",
                ))
                continue
            cases = path.get("cases")
            if not isinstance(path.get("condition"), str) or not isinstance(cases, list) or not cases:
                issues.append(Issue(
                    "reg.path_value", "error", "structure", "路径condition必须为字符串且cases必须为非空列表",
                    str(source), line, product_id=str(product_id), field=f"paths[{path_index}]",
                ))
                continue
            for case_index, case in enumerate(cases, start=1):
                if not isinstance(case, Mapping) or set(case) != REG_CASE_KEYS or not all(isinstance(case.get(key), str) for key in REG_CASE_KEYS):
                    issues.append(Issue(
                        "reg.case_shape", "error", "structure", "分段必须严格包含domain与pnl字符串",
                        str(source), line, product_id=str(product_id), field=f"paths[{path_index}].cases[{case_index}]",
                    ))


    core_src = root / "core" / "src"
    if not core_src.is_dir():
        core_src = Path(__file__).resolve().parents[2] / "core" / "src"
    if core_src.is_dir():
        inserted = False
        if str(core_src) not in sys.path:
            sys.path.insert(0, str(core_src))
            inserted = True
        try:
            from runtime.contracts.contract_api import validate_registry  # type: ignore

            runtime_errors = validate_registry(registry)
            for product_id, messages in runtime_errors.items():
                for message in messages:
                    issues.append(Issue(
                        "reg.runtime_validation", "error", "runtime_contract", message, str(source),
                        _product_line(registry_text, str(product_id)), product_id=str(product_id),
                    ))
        except Exception as error:  # noqa: BLE001
            issues.append(Issue(
                "reg.runtime_validator_unavailable", "error", "runtime_contract",
                f"无法调用共享Registry校验器：{type(error).__name__}: {error}", str(source), 1,
            ))
        finally:
            if inserted:
                sys.path.remove(str(core_src))


def _normalize_tex(value: str) -> str:
    text = value
    text = re.sub(r"\\mathcal\s*([A-Za-z])", r"\1", text)
    text = re.sub(r"\\(?:mathrm|text|mathcal)\{([^{}]*)\}", r"\1", text)
    text = text.replace("\\operatorname", "")
    replacements = {
        "\\alpha": "alpha", "\\sigma": "sigma", "\\tau": "tau", "\\ell": "ell",
        "\\Delta": "Delta", "\\Omega": "Omega", "\\infty": "inf", "\\%": "%",
        "\\cdot": "*", "\\times": "*", "\\left": "", "\\right": "", "\\,": "",
        "\\notin": "|", "\\in": "|", "\\le": "|", "\\ge": "|", "\\lt": "|", "\\gt": "|",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("{,}", "")
    text = re.sub(r"_\{([^{}]+)\}", lambda match: "_" + match[1].replace(",", "_"), text)
    text = re.sub(r"\^\{([^{}]+)\}", r"^(\1)", text)
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"\s+", "", text)
    return text


def _symbol_present(symbol: str, text: str) -> bool:
    target = _normalize_tex(symbol)
    normalized = _normalize_tex(text)
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(target)}(?![A-Za-z0-9_])", normalized) is not None


def _term_symbol_declared(key: str, symbol: str, global_text: str, product_text: str) -> bool:
    """接受OptionLib明确声明及已约定的通用观察集合写法。"""
    combined = global_text + product_text
    if _symbol_present(symbol, combined):
        return True
    normalized = _normalize_tex(product_text)
    if key == "hedge_schedule":
        return "H_hedge" in normalized
    if key == "Llock":
        return "锁定期" in product_text or "保股期" in product_text
    schedule_markers = {
        "Otouch": ("触碰观察", "触碰事件", "观察集合与路径", "是否触及"),
        "Oreset": ("重置观察",),
        "Oc": ("派息观察",),
        "Ovar": ("方差观察", "观察口径"),
        "Orange": ("区间观察", "观察集合"),
    }
    markers = schedule_markers.get(key)
    return bool(markers and "O" in _normalize_tex(combined) and any(marker in product_text for marker in markers))


def _safe_numeric(expression: str) -> float | None:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None
    allowed_binary = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}
    allowed_unary = {ast.UAdd: lambda value: value, ast.USub: lambda value: -value}

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_binary:
            return float(allowed_binary[type(node.op)](visit(node.left), visit(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_unary:
            return float(allowed_unary[type(node.op)](visit(node.operand)))
        raise ValueError("unsupported")

    try:
        value = visit(tree)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _parse_rhs(expression: str, known_symbols: Mapping[str, float]) -> float | None:
    normalized = _normalize_tex(expression).rstrip("。；，,")
    normalized = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", normalized)
    normalized = normalized.replace("^", "**")
    for symbol in sorted(known_symbols, key=len, reverse=True):
        normalized = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])",
            f"({known_symbols[symbol]!r})",
            normalized,
        )
    if re.search(r"[A-Za-z_]", normalized):
        return None
    if not re.fullmatch(r"[0-9eE+\-*/().]+", normalized):
        return None
    return _safe_numeric(normalized)


def _extract_default_assignments(
    line: str,
    product_terms: Mapping[str, Any],
    term_catalog: Mapping[str, Any],
) -> tuple[list[tuple[str, float]], list[str]]:
    line = re.sub(r"\$\s+\$", "", line)
    ordinary_terms = {key: value for key, value in product_terms.items() if key not in RULE_TERM_KEYS}
    symbol_to_keys: dict[str, list[str]] = {}
    known_symbols: dict[str, float] = {}
    for key, value in ordinary_terms.items():
        definition = term_catalog.get(key)
        if not isinstance(definition, Mapping):
            continue
        symbol = _normalize_tex(str(definition.get("symbol", "")))
        symbol_to_keys.setdefault(symbol, []).append(str(key))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            known_symbols[symbol] = float(value)
    assignments: list[tuple[str, float]] = []
    unresolved: list[str] = []
    spans = list(re.finditer(r"\$([^$]+)\$", line))
    for span in spans:
        fragment = span[1]
        if "=" not in fragment or any(operator in fragment for operator in ("\\ge", "\\le", "<", ">")):
            continue
        parts = [part.strip() for part in fragment.split("=")]
        if len(parts) < 2:
            continue
        left_symbols: list[str] = []
        for part in parts[:-1]:
            normalized_left = _normalize_tex(part)
            if re.fullmatch(r"[A-Za-z]+(?:_[A-Za-z0-9]+)*", normalized_left):
                left_symbols.append(normalized_left)
        if not left_symbols:
            continue
        value = _parse_rhs(parts[-1], known_symbols)
        if value is None and len(parts) > 2:
            value = _parse_rhs(parts[-2], known_symbols)
        if value is None:
            for symbol in left_symbols:
                if symbol in symbol_to_keys:
                    unresolved.append(fragment)
            continue
        trailing = line[span.end():]
        multiplier = 1.0
        if trailing.startswith("万元"):
            multiplier = 10_000.0
        elif trailing.startswith("亿元"):
            multiplier = 100_000_000.0
        for symbol in left_symbols:
            keys = symbol_to_keys.get(symbol, [])
            if len(keys) == 1:
                assignments.append((keys[0], value * multiplier))
                known_symbols[symbol] = value * multiplier
    duration_month = re.search(r"期限\s*(\d+(?:\.\d+)?)\s*个月", line)
    duration_year = re.search(r"期限\s*(\d+(?:\.\d+)?)\s*年", line)
    if "T" in ordinary_terms:
        if duration_month:
            assignments.append(("T", float(duration_month[1]) / 12.0))
        elif duration_year:
            assignments.append(("T", float(duration_year[1])))
    deduplicated: dict[str, float] = {}
    for key, value in assignments:
        deduplicated[key] = value
    return list(deduplicated.items()), sorted(set(unresolved))


def _validate_cross_sources(
    root: Path,
    list_products: list[ListProduct],
    lib_products: list[LibraryProduct],
    lib_text: str,
    registry: Mapping[str, Any],
    registry_text: str,
    issues: list[Issue],
    *,
    expected_count: int | None,
) -> dict[str, Any]:
    list_path = root / "references" / "optionlist.md"
    lib_path = root / "references" / "optionlib.md"
    reg_path = root / "references" / "optionreg.py"
    products = registry.get("products", {}) if isinstance(registry, Mapping) else {}
    catalog = registry.get("term_catalog", {}) if isinstance(registry, Mapping) else {}
    if not isinstance(products, Mapping):
        products = {}
    if not isinstance(catalog, Mapping):
        catalog = {}
    if expected_count is not None:
        for source, count, path in (
            ("OptionList", len(list_products), list_path),
            ("OptionLib", len(lib_products), lib_path),
            ("OptionReg", len(products), reg_path),
        ):
            if count != expected_count:
                issues.append(Issue(
                    "cross.product_count", "error", "identity", f"{source}产品数不等于当前基线{expected_count}",
                    str(path), 1, expected=expected_count, actual=count,
                ))
    for label, values, path in (
        ("OptionList编号", [item.product_id for item in list_products], list_path),
        ("OptionList名称", [item.name for item in list_products], list_path),
        ("OptionLib编号", [item.product_id for item in lib_products], lib_path),
        ("OptionLib名称", [item.name for item in lib_products], lib_path),
        ("OptionReg编号", [str(item) for item in products], reg_path),
        ("OptionReg名称", [str(product.get("identity", {}).get("name_zh", "")) for product in products.values() if isinstance(product, Mapping)], reg_path),
    ):
        duplicate_values = sorted(_duplicates(values))
        if duplicate_values:
            issues.append(Issue(
                "cross.duplicate", "error", "identity", f"{label}存在重复值", str(path), 1, actual=duplicate_values,
            ))
    list_map = {item.product_id: item for item in list_products}
    lib_map = {item.product_id: item for item in lib_products}
    reg_map = {str(key): value for key, value in products.items()}
    all_ids = sorted(set(list_map) | set(lib_map) | set(reg_map), key=_product_sort_key)
    sequences = [item.sequence for item in list_products]
    expected_sequences = list(range(1, len(list_products) + 1))
    if sequences != expected_sequences:
        issues.append(Issue(
            "list.sequence_invalid", "error", "identity",
            "OptionList序号必须唯一、从1连续并与表格行顺序一致", str(list_path), 1,
            expected=expected_sequences, actual=sequences,
        ))
    for product_id in all_ids:
        missing = [name for name, mapping in (("OptionList", list_map), ("OptionLib", lib_map), ("OptionReg", reg_map)) if product_id not in mapping]
        if missing:
            issues.append(Issue(
                "cross.product_missing", "error", "identity", "产品未同时存在于三份资料库", str(root / "references"),
                product_id=product_id, actual=missing,
            ))
            continue
        list_product = list_map[product_id]
        lib_product = lib_map[product_id]
        reg_product = reg_map[product_id]
        identity = reg_product.get("identity", {}) if isinstance(reg_product, Mapping) else {}
        names = {"OptionList": list_product.name, "OptionLib": lib_product.name, "OptionReg": identity.get("name_zh")}
        if len(set(names.values())) != 1:
            issues.append(Issue(
                "cross.name_mismatch", "error", "identity", "三份资料库中文唯一名称不一致", str(list_path),
                list_product.line, product_id=product_id, actual=names,
            ))
        if list_product.category != lib_product.category:
            issues.append(Issue(
                "cross.category_mismatch", "error", "identity",
                "OptionList所属类别与OptionLib二级章节不一致", str(list_path),
                list_product.line, product_id=product_id, expected=lib_product.category,
                actual=list_product.category,
            ))
        expected_status = list_product.status == "已录入"
        actual_status = identity.get("entry_status")
        if actual_status is not expected_status:
            issues.append(Issue(
                "cross.status_mismatch", "error", "identity", "OptionList入库情况与OptionReg.entry_status不一致",
                str(list_path), list_product.line, product_id=product_id, expected=expected_status, actual=actual_status,
            ))
        terms = reg_product.get("terms", {}) if isinstance(reg_product, Mapping) else {}
        paths = reg_product.get("paths", []) if isinstance(reg_product, Mapping) else []
        lib_path_order: list[str] = []
        payoff_counts: dict[str, int] = {}
        for cells, _line in lib_product.payoff_rows:
            label = cells[0]
            if label not in lib_path_order:
                lib_path_order.append(label)
            payoff_counts[label] = payoff_counts.get(label, 0) + 1
        expected_path_labels = [f"路径{index}" for index in range(1, len(paths) + 1)] if isinstance(paths, list) else []
        if lib_path_order != expected_path_labels:
            issues.append(Issue(
                "cross.path_count", "error", "path_alignment", "OptionLib路径编号与OptionReg路径数量或顺序不一致",
                str(lib_path), lib_product.line, product_id=product_id, expected=expected_path_labels, actual=lib_path_order,
            ))
        if isinstance(paths, list):
            for index, path in enumerate(paths, start=1):
                label = f"路径{index}"
                lib_case_count = payoff_counts.get(label, 0)
                reg_case_count = len(path.get("cases", [])) if isinstance(path, Mapping) and isinstance(path.get("cases"), list) else 0
                if lib_case_count != reg_case_count:
                    issues.append(Issue(
                        "cross.segment_granularity", "review", "content_alignment",
                        "OptionLib分段行与OptionReg case未逐项一一对应；即使合并公式可能经济等价，发布前仍须显式核对",
                        str(lib_path), lib_product.line, product_id=product_id, field=label,
                        expected=lib_case_count, actual=reg_case_count,
                    ))
        example_counts: dict[str, int] = {}
        for cells, row_line in lib_product.example_rows:
            label = cells[1]
            if label not in expected_path_labels:
                issues.append(Issue(
                    "lib.example_unknown_path", "error", "example", "默认示例引用不存在的路径", str(lib_path),
                    row_line, product_id=product_id, actual=label,
                ))
            example_counts[label] = example_counts.get(label, 0) + 1
        for label, segment_count in payoff_counts.items():
            if example_counts.get(label, 0) < segment_count:
                issues.append(Issue(
                    "lib.example_coverage", "error", "example", "默认示例未覆盖该路径的全部损益分段",
                    str(lib_path), lib_product.line, product_id=product_id, field=label,
                    expected=f">={segment_count}", actual=example_counts.get(label, 0),
                ))
        if isinstance(terms, Mapping) and lib_product.example_data and isinstance(catalog, Mapping):
            example_text, example_line = lib_product.example_data
            assignments, unresolved = _extract_default_assignments(example_text, terms, catalog)
            for key, parsed_value in assignments:
                actual_value = terms.get(key)
                if isinstance(actual_value, (int, float)) and not isinstance(actual_value, bool):
                    if not math.isclose(float(actual_value), float(parsed_value), rel_tol=1e-10, abs_tol=1e-10):
                        issues.append(Issue(
                            "cross.default_value_mismatch", "error", "default_data",
                            "OptionLib默认示例参数与OptionReg默认条款不一致", str(lib_path), example_line,
                            product_id=product_id, field=key, expected=actual_value, actual=parsed_value,
                        ))
            for fragment in unresolved:
                issues.append(Issue(
                    "cross.default_value_manual_review", "review", "manual_review",
                    "默认示例含已登记条款但表达式不能可靠自动求值，已保留人工复核而未猜测",
                    str(lib_path), example_line, product_id=product_id, evidence=fragment,
                ))
        if isinstance(terms, Mapping) and "1" in lib_product.sections and isinstance(catalog, Mapping):
            global_text = lib_text[: lib_text.find("### 1.2 损益口径")]
            product_text = lib_product.sections["1"][1]
            for key in terms:
                if key in RULE_TERM_KEYS or key in NON_MATH_TERM_KEYS:
                    continue
                definition = catalog.get(key)
                if not isinstance(definition, Mapping):
                    continue
                symbol = str(definition.get("symbol", ""))
                if symbol and not _term_symbol_declared(key, symbol, global_text, product_text):
                    severity = "review" if key == "Llock" else "error"
                    code = "cross.symbol_manual_review" if severity == "review" else "cross.symbol_not_declared"
                    issues.append(Issue(
                        code, severity, "symbol",
                        "OptionReg条款符号未在OptionLib第1.1或本产品条款要素中明确出现",
                        str(lib_path), lib_product.sections["1"][2], product_id=product_id, field=key, actual=symbol,
                    ))
    list_order = [item.product_id for item in list_products]
    lib_order = [item.product_id for item in lib_products]
    reg_order = [str(item) for item in products]
    if list_order != lib_order or list_order != reg_order:
        issues.append(Issue(
            "cross.product_order", "error", "identity", "三份资料库产品顺序不一致", str(root / "references"),
            expected=list_order, actual={"OptionLib": lib_order, "OptionReg": reg_order},
        ))
    weekly_lib = re.search(r"周度观察仅取实际周度观察日", lib_text)
    if weekly_lib:
        issues.append(Issue(
            "schedule.weekly_optionlib_migration", "release_blocker", "known_migration",
            "OptionLib第1.3仍保留weekly观察口径；蓝图13.1/13.2已将其登记为待迁移项，本任务不改资料内容",
            str(lib_path), _line_number(lib_text, weekly_lib.start()), field="1.3行权与结算",
            evidence="总设计蓝图13.1、13.2",
        ))
    return {
        "machine_verified": {
            "identity_name_category_status_order": len(all_ids),
            "lib_five_sections_and_tables": len(lib_products),
            "reg_schema_and_literal_keys": len(products),
            "path_count_alignment": len(all_ids),
        },
        "default_assignments_checked": sum(
            len(_extract_default_assignments(product.example_data[0], reg_map[product.product_id].get("terms", {}), catalog)[0])
            for product in lib_products
            if product.product_id in reg_map and product.example_data and isinstance(reg_map[product.product_id], Mapping)
        ),
        "products_with_default_data": sum(product.example_data is not None for product in lib_products),
        "manual_review": {
            "segment_granularity_differences": sum(issue.code == "cross.segment_granularity" for issue in issues),
            "unparsed_default_fragments": sum(issue.code == "cross.default_value_manual_review" for issue in issues),
            "symbol_declaration_gaps": sum(issue.code == "cross.symbol_manual_review" for issue in issues),
        },
        "not_fully_proven_cross_source": {
            "monitor_economic_semantics": len(all_ids),
            "pricing_methods_suitability": len(all_ids),
            "constraints_economic_completeness": len(all_ids),
            "condition_domain_pnl_equivalence": len(all_ids),
        },
    }


def _product_sort_key(product_id: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"(\d+)\.(\d+)", product_id)
    return (int(match[1]), int(match[2]), "") if match else (10**9, 10**9, product_id)


def _snapshot(root: Path, *, expected_count: int | None) -> RepositorySnapshot:
    root = root.expanduser().resolve()
    issues: list[Issue] = []
    list_products = _read_optionlist(root / "references" / "optionlist.md", issues)
    lib_products, lib_text = _read_optionlib(root / "references" / "optionlib.md", issues)
    registry, registry_text = _load_optionreg(root / "references" / "optionreg.py", issues)
    if registry:
        _validate_registry_shape(root, registry, registry_text, issues)
    coverage = _validate_cross_sources(
        root, list_products, lib_products, lib_text, registry, registry_text, issues,
        expected_count=expected_count,
    )
    issues.sort(key=lambda issue: (
        0 if issue.severity == "error" else 1 if issue.severity == "release_blocker" else 2,
        _product_sort_key(issue.product_id or ""), issue.code, issue.source, issue.line or 0,
    ))
    report = AuditReport(
        root=str(root), mode="repository", counts={
            "optionlist": len(list_products), "optionlib": len(lib_products),
            "optionreg": len(registry.get("products", {})) if isinstance(registry.get("products", {}), Mapping) else 0,
        }, issues=issues, coverage=coverage,
    )
    return RepositorySnapshot(root, list_products, lib_products, registry, registry_text, report)


def audit_repository(root: str | Path, *, expected_count: int | None = 65) -> AuditReport:
    """只读审计一个Knowledger根目录。"""
    return _snapshot(Path(root), expected_count=expected_count).report


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _source_hashes(root: Path) -> dict[str, str]:
    return {
        name: _file_hash(root / "references" / name)
        for name in ("optionlist.md", "optionlib.md", "optionreg.py")
    }


def _canonical_product(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def audit_candidate(baseline_root: str | Path, candidate_root: str | Path) -> AuditReport:
    """比较临时候选与基线；函数不写入基线或候选三库。"""
    baseline_path = Path(baseline_root).expanduser().resolve()
    candidate_path = Path(candidate_root).expanduser().resolve()
    before = {"baseline": _source_hashes(baseline_path), "candidate": _source_hashes(candidate_path)}
    baseline = _snapshot(baseline_path, expected_count=None)
    candidate = _snapshot(candidate_path, expected_count=None)
    issues = list(candidate.report.issues)
    base_maps = {
        "optionlist": {item.product_id: item for item in baseline.list_products},
        "optionlib": {item.product_id: item for item in baseline.lib_products},
        "optionreg": dict(baseline.registry.get("products", {})),
    }
    candidate_maps = {
        "optionlist": {item.product_id: item for item in candidate.list_products},
        "optionlib": {item.product_id: item for item in candidate.lib_products},
        "optionreg": dict(candidate.registry.get("products", {})),
    }
    additions = {source: sorted(set(candidate_maps[source]) - set(base_maps[source]), key=_product_sort_key) for source in base_maps}
    removals = {source: sorted(set(base_maps[source]) - set(candidate_maps[source]), key=_product_sort_key) for source in base_maps}
    if len({tuple(values) for values in additions.values()}) != 1:
        issues.append(Issue(
            "candidate.addition_incomplete", "error", "candidate_atomicity",
            "新增产品必须以同一编号同时进入OptionList、OptionLib与OptionReg",
            str(candidate_path / "references"), actual=additions,
        ))
    if len({tuple(values) for values in removals.values()}) != 1:
        issues.append(Issue(
            "candidate.removal_incomplete", "error", "candidate_atomicity",
            "删除产品必须在三份资料库形成同一受控变更，本工具不会自动删除",
            str(candidate_path / "references"), actual=removals,
        ))
    common_ids = set.intersection(*(set(mapping) for mapping in candidate_maps.values())) if candidate_maps else set()
    changed = {"optionlist": [], "optionlib": [], "optionreg": []}
    for product_id in sorted(common_ids, key=_product_sort_key):
        if product_id in base_maps["optionlist"]:
            old = base_maps["optionlist"][product_id]
            new = candidate_maps["optionlist"][product_id]
            if (old.name, old.category, old.status) != (new.name, new.category, new.status):
                changed["optionlist"].append(product_id)
        if product_id in base_maps["optionlib"]:
            old = base_maps["optionlib"][product_id]
            new = candidate_maps["optionlib"][product_id]
            if old.name != new.name or old.raw.strip() != new.raw.strip():
                changed["optionlib"].append(product_id)
        if product_id in base_maps["optionreg"]:
            if _canonical_product(base_maps["optionreg"][product_id]) != _canonical_product(candidate_maps["optionreg"][product_id]):
                changed["optionreg"].append(product_id)
    after = {"baseline": _source_hashes(baseline_path), "candidate": _source_hashes(candidate_path)}
    if before != after:
        issues.append(Issue(
            "candidate.source_mutated", "error", "candidate_read_only",
            "候选校验过程修改了资料源，违反只读边界", str(candidate_path / "references"),
            expected=before, actual=after,
        ))
    changes = {
        "added": additions,
        "removed": removals,
        "changed": changed,
        "read_only_hashes_unchanged": before == after,
    }
    issues.sort(key=lambda issue: (
        0 if issue.severity == "error" else 1 if issue.severity == "release_blocker" else 2,
        _product_sort_key(issue.product_id or ""), issue.code,
    ))
    return AuditReport(
        root=str(candidate_path), mode="candidate", counts=candidate.report.counts,
        issues=issues, coverage=candidate.report.coverage, changes=changes,
    )


def _write_report(report: AuditReport, output_format: str, output: str | None) -> str:
    rendered = report.to_markdown() if output_format == "markdown" else report.to_json() + "\n"
    if output:
        Path(output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    return rendered


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledger三库只读审计与候选门禁")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit", help="审计当前三库")
    audit_parser.add_argument("--root", default=".")
    audit_parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    audit_parser.add_argument("--output")
    audit_parser.add_argument("--strict-release", action="store_true")
    candidate_parser = subparsers.add_parser("candidate", help="只读比较临时候选")
    candidate_parser.add_argument("--baseline-root", required=True)
    candidate_parser.add_argument("--candidate-root", required=True)
    candidate_parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    candidate_parser.add_argument("--output")
    candidate_parser.add_argument("--strict-release", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "audit":
        report = audit_repository(args.root)
    else:
        report = audit_candidate(args.baseline_root, args.candidate_root)
    rendered = _write_report(report, args.format, args.output)
    if not args.output:
        print(rendered, end="")
    return report.exit_code(strict_release=args.strict_release)


if __name__ == "__main__":
    raise SystemExit(main())
