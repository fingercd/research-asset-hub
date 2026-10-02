"""Bounded literal query planning without semantic inference."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

# General scientific synonyms only; never paper-to-answer mappings.
LEXICON: tuple[tuple[str, ...], ...] = (
    ("consistency models", "一致性模型", "consistency model"),
    ("consistency", "一致性映射"),
    ("implicit diffusion", "隐式扩散"),
    ("latent", "潜空间", "隐空间"),
    ("noise prediction", "噪声预测"),
    ("activation outliers", "激活异常值", "异常激活", "activation outlier"),
    ("second order", "二阶信息", "二阶误差", "second-order"),
    ("classifier free", "无分类器", "classifier-free"),
    ("KV cache", "键值缓存", "key value cache"),
    ("cache", "缓存"),
    ("distillation", "蒸馏"),
    ("quantization", "量化"),
    ("compression", "压缩"),
    ("sparse", "稀疏", "sparsity"),
    ("pruning", "剪枝"),
    ("diffusion", "扩散"),
    ("sampling", "采样"),
    ("few step", "少步", "few-step"),
    ("one step", "单步", "一步", "one-step"),
    ("video", "视频"),
    ("image", "图像"),
    ("editing", "编辑"),
    ("generation", "生成", "generative"),
    ("spatial conditioning", "空间条件", "spatial condition", "spatial conditions"),
    ("zero initialization", "零初始化", "初始化为零", "zero-initialization", "zero initialized"),
    ("pretrained", "预训练", "pre-trained", "pretraining"),
    ("frozen weights", "冻结权重", "冻结模型权重", "frozen model weights"),
    ("memory", "显存", "内存"),
    ("low rank", "低秩", "low-rank"),
    ("attention", "注意力"),
    ("latency", "端到端延迟", "延迟"),
    ("Lean", "Lean4", "Lean 4"),
    ("theorem proving", "定理证明", "theorem prover"),
    ("interactive proof", "交互式证明", "interactive theorem proving"),
    ("proof obligation", "证明义务", "proof obligations"),
    ("proof state", "证明状态", "tactic state", "tactic states"),
    ("lemma retrieval", "引理检索", "引理库检索", "定理库检索", "premise retrieval"),
    ("proof", "证明", "证明器", "prover", "proofs", "theorem-provers"),
    ("lemma", "引理", "lemmas"),
    ("stochastic optimization", "随机优化", "stochastic optimisation"),
    ("algorithm discovery", "算法发现", "algorithmic discovery"),
    ("program evaluation", "程序评估", "评估候选程序", "程序验证"),
    ("program modification", "程序修改", "代码修改"),
    ("evolutionary search", "演化搜索", "演化式代码搜索", "变异搜索", "evolutionary code search"),
    ("decomposition", "分解", "拆分", "decompose"),
    ("recursive", "递归", "recursion"),
    ("self-correction", "自我修正", "self correction", "修复证明"),
    ("feedback", "反馈", "错误信息", "error feedback"),
    ("backtracking", "回退", "backtrack"),
    ("retrieval", "检索", "retrieve"),
    ("library", "库", "libraries"),
    ("geometry", "几何"),
    ("algorithm", "算法", "algorithms"),
    ("mathematics", "数学", "mathematical"),
    ("proof search", "证明搜索", "证明搜寻"),
    ("formalization", "形式化", "formalisation", "autoformalization"),
    ("verification", "验证", "形式验证", "verifier"),
    ("mathematical reasoning", "数学推理", "数学推导", "math reasoning"),
    ("inference", "推理运行时", "推理部署", "推理速度", "inference runtime"),
    ("budget", "预算", "compute budget", "计算预算"),
    ("cost", "成本", "开销", "费用"),
    ("agent scaffolding", "智能体脚手架", "agent scaffold", "scaffolding"),
    ("agent", "智能体", "agents"),
    ("data", "数据", "dataset", "数据集"),
    ("training", "训练"),
    (
        "LLM",
        "大语言模型",
        "语言模型",
        "large language model",
        "large language models",
        "language model",
    ),
    ("GPU", "图形处理器"),
    ("AI", "人工智能"),
    ("RL", "强化学习", "reinforcement learning"),
    ("DiT", "diffusion transformer", "diffusion transformers"),
    ("KV", "key value", "key-value"),
    ("IMO", "international mathematical olympiad"),
)
CONDITION_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        r"无需(?:重新|再)训练|不需要(?:重新|再)训练|不(?:重新|再)训练"
        r"|不想再训练|不需重新训练|without retraining|no retraining",
        "exclude",
        ("retraining", "重新训练"),
    ),
    (r"无需训练|不需要训练|without training|不想训练", "exclude", ("training", "训练")),
    (r"免训练|training[- ]free", "require", ("training free", "training-free", "免训练")),
    (
        r"(?:低|有限|少量|受限)(?:的)?(?:计算)?预算|(?:计算)?预算(?:有限|受限)"
        r"|low budget|limited budget",
        "require",
        ("low budget", "limited budget", "budget", "预算"),
    ),
    (r"低成本|低开销|low cost|low-cost", "require", ("low cost", "low-cost", "cost", "成本")),
    (
        r"(?:数据|数据集)(?:有限|受限|稀缺)|(?:有限|少量|受限)(?:的)?(?:数据|数据集)"
        r"|limited data|data[- ]limited",
        "require",
        ("limited data", "data-limited", "数据有限"),
    ),
    (
        r"无需(?:校准|标注|训练)?数据|不需要(?:校准|标注|训练)?数据"
        r"|data[- ]free|without (?:calibration |training )?data",
        "exclude",
        ("data", "数据", "calibration data"),
    ),
)
NEGATIVE = re.compile(
    r"(?:排除|避免|不要(?!求)|不使用|不用|不依赖|禁用|禁止|不允许|不可使用|无需|不需要"
    r"|(?:exclude(?:s)?|without|no|not|must not|disable)\b\s*:?[ ]*)\s*"
    r"("
    + "|".join(
        re.escape(term) + (r"(?![a-z0-9_])" if term.isascii() else "")
        for term in sorted({alias for group in LEXICON for alias in group}, key=len, reverse=True)
    )
    + r"|[A-Za-z][A-Za-z0-9_+.-]*|[^\W\d_与且和及的，。！？、；：]{1,12})",
    re.I,
)
IDENTIFIER = re.compile(
    r"10\.\d{4,9}/[^\s，。？?;；]+|(?:arxiv\s*:\s*)?\d{4}\.\d{4,5}(?:v\d+)?", re.I
)
ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*")
COMMON = {
    "what",
    "which",
    "how",
    "why",
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "in",
    "of",
    "to",
    "is",
    "are",
    "can",
    "with",
    "without",
    "not",
    "no",
    "find",
    "papers",
    "paper",
    "methods",
    "method",
    "does",
    "do",
    "using",
    "use",
}
GENERIC = {"llm", "gpu", "ai", "rl", "dit", "kv", "imo"}
TASK_WORDS = {"on", "about", "please", "return", "search", "explain", "compare", "discuss"}
BROAD_CONCEPTS = {
    "verification",
    "agent",
    "training",
    "generation",
    "data",
    "budget",
    "cost",
    "LLM",
    "GPU",
    "AI",
    "RL",
    "KV",
    "IMO",
    "retrieval",
    "library",
}
DOMAIN_CONCEPTS = {
    "theorem proving",
    "interactive proof",
    "proof",
    "Lean",
    "formalization",
    "mathematical reasoning",
    "mathematics",
    "stochastic optimization",
    "algorithm discovery",
    "algorithm",
    "geometry",
    "diffusion",
    "quantization",
    "compression",
    "video",
    "image",
    "DiT",
    "consistency models",
    "generation",
}


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def contains(text: str, term: str) -> bool:
    text, term = normalize(text), normalize(term)
    if not term:
        return False
    return bool(re.search(_term_pattern(term), text))


@dataclass(frozen=True)
class ConceptGroup:
    name: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Condition:
    text: str
    terms: tuple[str, ...]
    polarity: str
    needs_evidence_verification: bool = True


@dataclass(frozen=True)
class QueryPlan:
    queries: tuple[str, ...]
    original: str
    concept_groups: tuple[ConceptGroup, ...]
    conditions: tuple[Condition, ...]
    named_terms: tuple[str, ...]
    unmapped: tuple[str, ...]
    year: int | None = None
    exact_titles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["needs_evidence_verification"] = True
        value["mode"] = "literal_keywords"
        return value


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for value in values:
        if value.strip():
            result.setdefault(normalize(value), value.strip())
    return tuple(result.values())


def _conditions(text: str) -> tuple[list[Condition], str]:
    result: list[Condition] = []
    remaining = unicodedata.normalize("NFKC", text)
    # Relaxing a restriction is neither a required nor an excluded retrieval term.
    relaxation = r"不要求[^，。！？?;；]*"
    for match in re.finditer(relaxation, remaining):
        result.append(Condition(match.group(), (), "unspecified"))
    remaining = re.sub(relaxation, " ", remaining)
    for pattern, polarity, terms in sorted(
        CONDITION_RULES, key=lambda rule: len(rule[0]), reverse=True
    ):
        for match in re.finditer(pattern, remaining, re.I):
            result.append(Condition(match.group(), terms, polarity))
        remaining = re.sub(pattern, " ", remaining, flags=re.I)
    for match in NEGATIVE.finditer(remaining):
        value = match.group(1).strip()
        terms = next(
            (group for group in LEXICON if any(contains(value, t) for t in group)), (value,)
        )
        result.append(Condition(match.group(), terms, "exclude"))
    return result, NEGATIVE.sub(" ", remaining)


def _short_query(terms: Sequence[str]) -> str:
    return " ".join(_unique(terms)[:8])[:160].strip()


def _term_pattern(term: str) -> str:
    pattern = re.escape(term)
    if term == "一步":
        return r"(?<![下进每])一步"
    return r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])" if term.isascii() else pattern


def _titles(query: str) -> tuple[str, ...]:
    if not re.search(r"题名|标题|title", query, re.I):
        return ()
    quoted = re.findall(r'[“"《]([^”"》]+)[”"》]', query)
    if quoted:
        return _unique(quoted)
    match = re.search(r"(?:题名|标题|title)[^：:\n]{0,50}[：:]\s*(.+)", query, re.I)
    return (match.group(1).strip().rstrip("。.!！?？"),) if match else ()


def _literal_phrases(text: str) -> tuple[str, ...]:
    # With no lexicon match, retain bounded input substrings, never an invented translation.
    filler = (
        r"我想|想找|找用|有没有|请|怎样|如何|为什么|可以|能够|需要|针对|实现|并且|以及|的|和|与"
    )
    text = unicodedata.normalize("NFKC", text)
    pieces = re.split(filler, text)
    phrases = [phrase for piece in pieces for phrase in re.findall(r"[\u4e00-\u9fff]+", piece)]
    # Keep complete contiguous input phrases. Punctuation, ASCII, and truncated
    # prefixes/suffixes are never disguised as a scientific keyword.
    phrases = [
        phrase
        for phrase in phrases
        if 2 <= len(phrase) <= 24
        and not re.search(r"^(?:要把|并把|并在|这样|给|让)|(?:时|后|中|前|为|来|下)$", phrase)
    ]
    return _unique(phrases)[:3]


def _named(term: str) -> bool:
    if normalize(term) in GENERIC | COMMON | TASK_WORDS:
        return False
    if any(normalize(term) == normalize(alias) for group in LEXICON for alias in group):
        return False
    return bool(re.search(r"[a-z][A-Z]|[A-Za-z]\d|\d[A-Za-z]", term)) or (
        term.replace("-", "").isupper() and len(term.replace("-", "")) >= 2
    )


def plan_query(
    query: str, keywords: Sequence[str] = (), constraints: Sequence[str] = ()
) -> QueryPlan:
    """Preserve raw questions and conditions, producing at most three literal queries."""
    if len(keywords) > 8 or len(constraints) > 6:
        raise ValueError("At most 8 keywords and 6 constraints are allowed.")
    conditions, positive = _conditions(query)
    exact_titles = _titles(query)
    for text in _unique(constraints):
        parsed, remainder = _conditions(text)
        conditions.extend(parsed)
        if remainder.strip():
            groups = [group for group in LEXICON if any(contains(remainder, t) for t in group)]
            terms = _unique([group[0] for group in groups]) or (remainder.strip(),)
            conditions.append(Condition(text, terms, "require"))
    excluded = [
        term
        for condition in conditions
        if condition.polarity == "exclude"
        for term in condition.terms
    ]
    effective_keywords = list(keywords)
    for term in sorted(excluded, key=len, reverse=True):
        pattern = re.escape(term)
        if term.isascii():
            pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
        positive = re.sub(pattern, " ", positive, flags=re.I)
        effective_keywords = [
            re.sub(pattern, " ", value, flags=re.I).strip() for value in effective_keywords
        ]
    # Explicit native-agent keywords are literal and never automatically translated.
    working = unicodedata.normalize("NFKC", " ".join(effective_keywords) if keywords else positive)
    groups_found: list[ConceptGroup] = []
    remaining = working
    matched_groups = sorted(
        ((terms, alias) for terms in LEXICON for alias in terms if contains(working, alias)),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    for terms, matched_alias in matched_groups:
        if terms[0] in {"one step", "few step"} and not any(
            contains(working, context)
            for context in (
                "采样",
                "生成",
                "扩散",
                "训练",
                "sampling",
                "generation",
                "diffusion",
                "training",
            )
        ):
            continue
        if contains(remaining, matched_alias):
            if not any(group.name == terms[0] for group in groups_found):
                groups_found.append(ConceptGroup(terms[0], terms))
            # Consume only the matched phrase; another alias can overlap a more
            # specific phrase elsewhere in the question.
            remaining = re.sub(_term_pattern(matched_alias), " ", remaining, flags=re.I)
    named = [
        re.sub(r"^arxiv\s*:\s*", "", term, flags=re.I).rstrip(".,")
        for term in IDENTIFIER.findall(positive)
    ]
    for term in ASCII_TERM.findall(IDENTIFIER.sub(" ", positive + " " + working)):
        if _named(term):
            named.append(term)
    named = list(_unique(named))
    for term in sorted(named, key=len, reverse=True):
        remaining = re.sub(_term_pattern(term), " ", remaining, flags=re.I)
    unmapped = _unique(
        [
            word
            for word in ASCII_TERM.findall(remaining)
            if normalize(word) not in COMMON | TASK_WORDS
        ]
        + re.findall(r"[\u4e00-\u9fff]+", remaining)
    )
    # Unknown literal keywords remain searchable and rankable, with no invented aliases.
    unknown_literals = (
        list(_unique(effective_keywords))
        if keywords
        else [word for word in unmapped if word.isascii()]
    )
    if not keywords and not any(group.name not in BROAD_CONCEPTS for group in groups_found):
        unknown_literals += list(_literal_phrases(remaining))
    for term in unknown_literals:
        if not any(contains(term, alias) for group in groups_found for alias in group.terms):
            groups_found.append(ConceptGroup(term, (term,)))
    groups_found.sort(
        key=lambda group: (
            0 if group.name in DOMAIN_CONCEPTS else 2 if group.name in BROAD_CONCEPTS else 1,
            min(
                (
                    working.casefold().find(alias.casefold())
                    for alias in group.terms
                    if contains(working, alias)
                ),
                default=len(working),
            ),
        )
    )
    literal_terms = (
        list(_unique(effective_keywords)) if keywords else [group.name for group in groups_found]
    )
    if not keywords:
        if any(
            term in literal_terms
            for term in ("theorem proving", "interactive proof", "proof search")
        ):
            literal_terms = [term for term in literal_terms if term != "proof"]
        if any(
            term in literal_terms
            for term in ("algorithm discovery", "stochastic optimization", "mathematical reasoning")
        ):
            literal_terms = [term for term in literal_terms if term != "mathematics"]
    queries = [_short_query([title]) for title in exact_titles[:1]]
    if named:
        queries.append(_short_query(named))
    core_terms = literal_terms if keywords else literal_terms[:2]
    core = _short_query(core_terms)
    if core:
        queries.append(core)
    required = [condition.terms[0] for condition in conditions if condition.polarity == "require"]
    if required:
        condition_query = _short_query([*(core_terms or named[:2]), *required[:2]])
        if len(queries) >= 3:
            queries[-1] = condition_query
        else:
            queries.append(condition_query)
    elif not keywords and len(literal_terms) > len(core_terms):
        queries.append(_short_query(literal_terms[:4]))
        if len(literal_terms) > 4:
            queries.append(_short_query([*core_terms, *literal_terms[4:6]]))
    if not queries and positive.strip():
        queries.append(_short_query(_literal_phrases(positive)))
    if not queries:
        raise ValueError("A nonempty question or keyword is required.")
    year_match = re.search(r"\b(20\d{2})(?:\s*年)?\b|(?<!\d)(20\d{2})年", positive)
    year = int(next(value for value in year_match.groups() if value)) if year_match else None
    return QueryPlan(
        _unique(queries)[:3],
        query,
        tuple(groups_found),
        tuple(conditions),
        tuple(named),
        unmapped,
        year,
        exact_titles,
    )


def query_variants(query: str) -> list[str]:
    """Compatibility wrapper; consumers should use the complete query plan."""
    if not query.strip():
        return [query]
    return list(plan_query(query).queries)
