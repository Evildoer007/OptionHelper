"""Product-aware retrieval shared by App and Skill; source chapters stay complete."""
from collections.abc import Mapping, Sequence
from pathlib import Path
from functools import lru_cache
import re


def product_relevance(query: str, product_id: str, product: Mapping) -> float:
    query=query.strip().casefold()
    name=str(product.get('identity',{}).get('name_zh','')).casefold()
    if not query or not name:
        return 0
    if re.search(rf'(?<![\d.]){re.escape(product_id)}(?![\d.])',query):
        return 10000
    if name in query:
        return 1000+len(name)
    # Chinese prose need not insert spaces between a product family and its question.
    # Prefer the longest shared name fragment, then its position in the query.
    best=0.0
    for word in re.findall(r'[\u4e00-\u9fff]+|[a-z]+',name):
        for width in range(2,len(word)+1):
            for start in range(len(word)-width+1):
                token=word[start:start+width]
                position=query.find(token)
                if position>=0:
                    best=max(best,width*width+1/(position+1))
    if not best:
        for hints,target in ((('上涨','上行','走高'),'看涨'),(('下跌','下行','走低'),'看跌')):
            if target in name and any(hint in query for hint in hints):
                return 1
    return best


def select_products(products: Mapping, queries: Sequence[str], *, limit: int=3):
    """Cover each question before filling remaining slots with related products."""
    ranked=[]
    for query in queries:
        scores=[(product_relevance(str(query),pid,p),pid,p) for pid,p in products.items() if isinstance(p,Mapping)]
        ranked.append(sorted((row for row in scores if row[0]>0),key=lambda row:-row[0]))
    selected={}
    for rows in ranked:
        if rows and len(selected)<limit:
            _,pid,product=rows[0]
            selected[pid]=product
    for _,pid,product in sorted((row for rows in ranked for row in rows),key=lambda row:-row[0]):
        if len(selected)>=limit:
            break
        selected.setdefault(pid,product)
    matches={pid:product for rows in ranked for _,pid,product in rows}
    return list(selected.items()), list(matches.items())


def product_section(path: Path, product_id: str) -> str | None:
    if not path.is_file():
        return None
    text=path.read_text(encoding='utf-8')
    match=re.search(rf'^###\s+{re.escape(product_id)}\s+.*$',text,re.MULTILINE)
    if match is None:
        return None
    next_heading=re.search(r'^#{1,3}\s+',text[match.end():],re.MULTILINE)
    end=match.end()+next_heading.start() if next_heading else len(text)
    return text[match.start():end]


_PRODUCT_ID = re.compile(r"\d{1,2}\.\d{1,2}")
_NAMED_COMPARISON = re.compile(r"比较|对比|相比|比[^。？?]{0,24}(?:更|划算)|vs\b|versus|二选一|怎么选|如何选|更合适|哪个[更好]|按[^。；]{0,14}(?:亏损|损失)[^。；]{0,10}(?:排|选)|各做一遍", re.I)
_PLAIN_PRODUCT_NAMES = {
    "1.1": ("普通看涨", "买入看涨", "直接买看涨", "认购期权"),
    "1.2": ("普通看跌", "买入看跌", "直接买看跌", "认沽期权"),
    "2.1": ("牛市价差", "看涨牛市价差"),
    "2.3": ("熊市价差", "看跌熊市价差"),
}


@lru_cache(maxsize=1)
def product_names():
    from .registry_loader import load_registry
    registry = load_registry()
    return {product_id: re.sub(r"[（(].*$", "", product["identity"]["name_zh"]).strip()
            for product_id, product in registry["products"].items()}


@lru_cache(maxsize=1)
def _product_labels():
    labels = {}
    for product_id, name in product_names().items():
        if name:
            labels.setdefault(name, []).append(product_id)
        for alias in _PLAIN_PRODUCT_NAMES.get(product_id, ()):
            labels.setdefault(alias, []).append(product_id)
    return {label: ids[0] for label, ids in labels.items() if len(ids) == 1}


@lru_cache(maxsize=1)
def _product_label_pattern():
    labels = sorted(_product_labels(), key=len, reverse=True)
    names = "|".join(re.escape(label) for label in labels)
    return re.compile(rf"(?P<label>{names})|(?:产品|结构|方案)\s*(?P<id>{_PRODUCT_ID.pattern})(?![\d.])"
                      r"|买(?:入)?[^，。；！？]{0,14}行权(?:价)?的(?P<vanilla>看涨|看跌)(?!价差)")


def mentioned_product_ids(text: str) -> tuple[str, ...]:
    """Match complete registered names and common names before shorter labels."""
    return tuple(dict.fromkeys(
        match["id"] or (_product_labels()[match["label"]] if match["label"] else
                        {"看涨": "1.1", "看跌": "1.2"}[match["vanilla"]])
        for match in _product_label_pattern().finditer(text)
        if not match["id"] or match["id"] in product_names()
    ))


def named_comparison_product_ids(text: str) -> tuple[str, ...]:
    if not (_NAMED_COMPARISON.search(text)
            or re.search(r"(?:两种|两笔|两套|两份|两个)(?:看涨|看跌)?(?:结构|合同|方案|产品)", text)):
        return ()
    ids = mentioned_product_ids(text)
    return ids if len(ids) > 1 else ()
