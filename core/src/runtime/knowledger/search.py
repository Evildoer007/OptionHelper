"""Product-aware retrieval shared by App and Skill; source chapters stay complete."""
from collections.abc import Mapping, Sequence
from pathlib import Path
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
