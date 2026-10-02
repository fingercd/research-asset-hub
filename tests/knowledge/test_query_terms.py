import pytest

from packages.knowledge.query_terms import contains, plan_query, query_variants


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
