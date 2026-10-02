"""Small bilingual scientific terminology expansion, not semantic embedding search."""

from __future__ import annotations

import re

# Longer scientific phrases are consumed first so their components do not duplicate them.
TERMS = {
    "无需重新训练": "without retraining",
    "不需要重新训练": "without retraining",
    "无需训练": "training free",
    "一致性模型": "consistency models",
    "一致性映射": "consistency",
    "隐式扩散": "implicit diffusion",
    "潜空间": "latent",
    "隐空间": "latent",
    "噪声预测": "noise prediction",
    "异常激活": "activation outliers",
    "激活异常值": "activation outliers",
    "二阶信息": "second order",
    "二阶误差": "second order",
    "无分类器": "classifier free",
    "键值缓存": "KV cache",
    "缓存": "cache",
    "蒸馏": "distillation",
    "量化": "quantization",
    "压缩": "compression",
    "稀疏": "sparse",
    "剪枝": "pruning",
    "扩散": "diffusion",
    "采样": "sampling",
    "少步": "few step",
    "单步": "one step",
    "一步": "one step",
    "视频": "video",
    "图像": "image",
    "编辑": "editing",
    "生成": "generation",
    "显存": "memory",
    "低秩": "low rank",
    "注意力": "attention",
    "端到端延迟": "latency",
}


def query_variants(query: str) -> list[str]:
    remaining = query
    translated = []
    for term in sorted(TERMS, key=len, reverse=True):
        if term in remaining:
            translated.append(TERMS[term])
            remaining = remaining.replace(term, " ")
    if not translated:
        return [query]
    english = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]*", query)
    expansion = " ".join(dict.fromkeys([*english, *translated]))
    return [query, expansion] if expansion.casefold() != query.casefold() else [query]
