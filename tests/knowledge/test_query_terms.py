import pytest

from packages.knowledge.query_terms import (
    QueryPlan,
    _literal_phrases,
    contains,
    plan_query,
    query_variants,
)


def test_long_question_keeps_topics_conditions_and_raw_question():
    raw = "我想找无需重新训练、数据有限且低预算的扩散采样方法，可以一步生成图像吗？"
    plan = plan_query(raw)
    assert plan.original == raw
    assert len(plan.queries) <= 3
    assert all(len(query) <= 160 for query in plan.queries)
    assert {group.name for group in plan.concept_groups} >= {"diffusion", "sampling", "generation"}
    assert {condition.text for condition in plan.conditions} >= {
        "无需重新训练",
        "数据有限",
        "低预算",
    }
    assert not any("retraining" in query for query in plan.queries)
    assert any("low budget" in query or "limited data" in query for query in plan.queries)
    assert plan.to_dict()["needs_evidence_verification"]


def test_negative_terms_do_not_become_core_queries():
    plan = plan_query("扩散采样，不要蒸馏，排除PruneNet")
    assert "distillation" not in " ".join(plan.queries)
    assert "PruneNet" not in plan.named_terms
    assert "PruneNet" not in " ".join(plan.queries)
    assert all(condition.polarity == "exclude" for condition in plan.conditions)


def test_unknown_terms_are_reported_without_inventing_translations():
    plan = plan_query("未知机制与bluewidget的扩散")
    assert any("未知机制" in term for term in plan.unmapped)
    assert "bluewidget" in plan.unmapped
    assert "bluewidget" in " ".join(plan.queries)
    assert plan.original == "未知机制与bluewidget的扩散"


def test_native_keywords_are_literal_and_constraints_are_preserved():
    plan = plan_query(
        "请帮忙查资料", ["Lean4", "proof search"], ["无需重新训练", "budget below 100"]
    )
    assert "Lean4 proof search" in plan.queries
    assert any(condition.text == "budget below 100" for condition in plan.conditions)
    assert any(condition.text == "无需重新训练" for condition in plan.conditions)
    assert all(condition.needs_evidence_verification for condition in plan.conditions)


@pytest.mark.parametrize("keywords,constraints", [(["x"] * 9, []), ([], ["x"] * 7)])
def test_explicit_input_bounds(keywords: list[str], constraints: list[str]):
    with pytest.raises(ValueError, match="At most"):
        plan_query("query", keywords, constraints)


def test_empty_query_and_keyword_only_queries():
    with pytest.raises(ValueError, match="nonempty"):
        plan_query("  ")
    assert plan_query("", ["proof search"]).queries == ("proof search",)
    assert query_variants("") == [""]


def test_ascii_alias_boundary_and_nfkc_names():
    assert contains("Lean4 proves a theorem", "Lean4")
    assert not contains("Clean architecture", "Lean")
    assert not contains("sailing", "AI")
    plan = plan_query("S²Q量化中的激活异常值")
    assert "S2Q" in plan.named_terms
    assert any(group.name == "activation outliers" for group in plan.concept_groups)
    assert contains("S²Q", "S2Q")
    assert query_variants("DDIM") == ["DDIM"]


def test_ids_and_explicit_year_are_preserved():
    plan = plan_query("2026年，arXiv: 2601.12345v2 和 10.1234/alpha.beta")
    assert plan.year == 2026
    assert "2601.12345v2" in plan.named_terms
    assert "10.1234/alpha.beta" in plan.named_terms


def test_math_reasoning_is_not_implicitly_runtime_inference():
    math = plan_query("Lean中的数学推理和证明搜索")
    assert "inference" not in {group.name for group in math.concept_groups}
    assert {group.name for group in math.concept_groups} >= {
        "mathematical reasoning",
        "proof search",
    }
    assert "inference" not in {group.name for group in plan_query("推理").concept_groups}
    assert "inference" in {group.name for group in plan_query("推理部署").concept_groups}


def test_ai_math_terms_are_generic_and_common_acronyms_are_not_named_methods():
    plan = plan_query("Lean 定理证明 形式化 验证 智能体脚手架 LLM GPU AI RL DiT")
    assert {group.name for group in plan.concept_groups} >= {
        "Lean",
        "theorem proving",
        "formalization",
        "verification",
        "agent scaffolding",
    }
    assert not {"LLM", "GPU", "AI", "RL", "DiT"}.intersection(plan.named_terms)


def test_without_training_data_is_one_exclusion_not_positive_data():
    plan = plan_query("diffusion sampling without training data")
    assert "data" not in {group.name for group in plan.concept_groups}
    assert any(
        condition.polarity == "exclude" and "data" in condition.terms
        for condition in plan.conditions
    )


def test_negative_clauses_stop_before_unrelated_positive_topics():
    plan = plan_query("without distillation and diffusion sampling")
    assert {group.name for group in plan.concept_groups} >= {"diffusion", "sampling"}
    assert "distillation" not in " ".join(plan.queries)
    chinese = plan_query("不要蒸馏的扩散采样")
    assert {group.name for group in chinese.concept_groups} >= {"diffusion", "sampling"}


def test_native_negative_constraint_prefixes_and_original_methods():
    plan = plan_query("AlphaNet 扩散", ["diffusion"], ["not distillation", "exclude: pruning"])
    assert "AlphaNet" in plan.named_terms
    assert all(condition.polarity == "exclude" for condition in plan.conditions)
    assert "distillation" not in " ".join(plan.queries)


def test_explicit_exclusions_override_conflicting_literal_keywords():
    plan = plan_query(
        "distillation and diffusion", ["distillation", "diffusion"], ["not distillation"]
    )
    assert plan.queries == ("diffusion",)
    assert not any(group.name == "distillation" for group in plan.concept_groups)
    assert plan.original == "distillation and diffusion"


def test_mechanism_phrases_keep_domains_and_longest_actual_match():
    plan = plan_query("请介绍随机优化中的证明义务与证明搜索，再解释错误反馈的作用")
    names = {group.name for group in plan.concept_groups}
    assert names >= {"stochastic optimization", "proof obligation", "proof search", "feedback"}
    assert plan.queries[0].startswith("stochastic optimization")
    assert len(plan.queries) <= 3


def test_evolution_and_program_tasks_are_literal_general_concepts():
    plan = plan_query("算法发现的程序评估与演化搜索有哪些技术？")
    assert {group.name for group in plan.concept_groups} >= {
        "algorithm discovery",
        "program evaluation",
        "evolutionary search",
    }
    assert any("evolutionary search" in query for query in plan.queries)


def test_narrative_step_is_not_sampling_but_explicit_generation_is():
    narrative = plan_query("验证后下一步如何组织证明？")
    assert "one step" not in {group.name for group in narrative.concept_groups}
    mathematical = plan_query("用单步证明解释递归结构")
    assert "one step" not in {group.name for group in mathematical.concept_groups}
    sampling = plan_query("下一步比较一步生成和少步采样")
    assert {"one step", "few step"} <= {group.name for group in sampling.concept_groups}


def test_common_title_case_tasks_do_not_become_unique_methods():
    plan = plan_query("Theorem-Provers Self-Correction KV GPU GraphWidget GraphWidgetPlus S²Q")
    assert not {"Theorem-Provers", "Self-Correction", "KV", "GPU"}.intersection(plan.named_terms)
    assert {"GraphWidget", "GraphWidgetPlus", "S2Q"} <= set(plan.named_terms)
    assert "Plus" not in plan.unmapped


def test_negative_training_disabled_tokens_and_relaxed_restriction():
    plan = plan_query("量化不想再训练网络，禁用 DebugToken，不要求只使用GPU")
    assert "training" not in {group.name for group in plan.concept_groups}
    assert "DebugToken" not in " ".join(plan.queries)
    assert any(condition.polarity == "unspecified" for condition in plan.conditions)
    assert not any(
        condition.text.startswith("不要求") and condition.polarity == "exclude"
        for condition in plan.conditions
    )


def test_explicit_title_is_preserved_and_old_query_plan_constructor_works():
    title = "A New Graph Partition Algorithm: Experiments and Analysis"
    plan = plan_query("按英文题名检索：" + title + "。")
    assert plan.exact_titles == (title,)
    assert title in plan.queries
    assert plan.to_dict()["exact_titles"] == (title,)
    assert QueryPlan(("x",), "x", (), (), (), ()).exact_titles == ()


def test_unknown_chinese_fallback_is_bounded_and_not_a_full_question():
    raw = "如何用拓扑编码描绘复杂星系分布，并解释区域连接模式与异常簇的结构？"
    plan = plan_query(raw)
    assert plan.original == raw and plan.unmapped
    assert all(len(query) <= 74 for query in plan.queries)
    assert all(query != raw for query in plan.queries)


def test_single_generic_concept_does_not_drop_unknown_scientific_phrase():
    raw = "拓扑编码的研究怎样进行验证？"
    plan = plan_query(raw)
    assert "拓扑编码" in " ".join(plan.queries)
    assert plan.unmapped


def test_common_neural_network_mechanisms_do_not_include_residual_grammar():
    raw = "预训练图像生成模型添加空间条件，模块初始化为零，并使用冻结权重，为什么这样设计？"
    plan = plan_query(raw)
    assert {group.name for group in plan.concept_groups} >= {
        "pretrained",
        "spatial conditioning",
        "zero initialization",
        "frozen weights",
        "generation",
    }
    assert plan.queries[0].startswith("image generation")
    assert all(query.isascii() for query in plan.queries)
    assert plan.original == raw and plan.unmapped


def test_literal_fallback_uses_complete_chinese_phrases_and_normalized_punctuation():
    left = _literal_phrases("拓扑编码，区域连接模式；要把新增连接初始化为，空间条件时")
    right = _literal_phrases("拓扑编码,区域连接模式;要把新增连接初始化为,空间条件时")
    assert left == right == ("拓扑编码", "区域连接模式")
    assert all("," not in phrase and ";" not in phrase for phrase in left)
    long_phrase = "研究复杂星系分布异常簇结构"
    assert _literal_phrases(long_phrase) == (long_phrase,)
