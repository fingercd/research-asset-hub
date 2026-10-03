from packages.knowledge.query_terms import plan_query
from packages.knowledge.ranking import lexical_rank


def test_title_multiconcept_coverage_beats_related_work_mentions():
    plan = plan_query("扩散采样低预算")
    relevant, reasons = lexical_rank(plan, {"title": "Diffusion sampling with low budget"})
    incidental, _ = lexical_rank(
        plan,
        {"title": "General transformer survey"},
        "Related work discusses diffusion sampling with low budget.",
    )
    assert relevant > incidental
    assert reasons["metadata_concepts_covered"] == 2
    assert reasons["conditions"][0]["needs_evidence_verification"]
    assert not reasons["score_is_probability"]


def test_excluded_terms_in_body_do_not_penalize_or_filter_result():
    plan = plan_query("扩散采样，不要蒸馏")
    score, _ = lexical_rank(plan, {"title": "Diffusion sampling"})
    with_related_work, reasons = lexical_rank(
        plan,
        {"title": "Diffusion sampling"},
        "Related work uses distillation.",
    )
    title_exclusion, title_reasons = lexical_rank(
        plan,
        {"title": "Diffusion sampling with distillation"},
    )
    assert with_related_work == score
    assert title_exclusion == score - 2
    assert reasons["conditions"][0]["weight"] == 0
    assert title_reasons["conditions"][0]["weight"] == -2


def test_repeated_words_do_not_inflate_score():
    plan = plan_query("扩散采样")
    once, _ = lexical_rank(plan, {"title": "diffusion sampling"})
    repeated, _ = lexical_rank(plan, {"title": "diffusion sampling " * 80})
    assert once == repeated


def test_method_and_exact_id_outrank_generic_concepts():
    plan = plan_query("AlphaNet 扩散采样，arXiv: 2601.12345")
    generic, _ = lexical_rank(plan, {"title": "Diffusion Sampling"})
    method, _ = lexical_rank(plan, {"title": "AlphaNet"})
    identifier, reasons = lexical_rank(plan, {"arxiv": "2601.12345"})
    assert identifier > generic and method > generic
    assert reasons["matches"][0]["field"] == "identifier"


def test_exact_title_and_nfkc_alias():
    exact, reasons = lexical_rank(
        plan_query("A Study of Compression"), {"title": "A Study of Compression"}
    )
    broad, _ = lexical_rank(plan_query("A Study of Compression"), {"title": "compression"})
    assert exact > broad
    assert reasons["matches"][0]["kind"] == "exact_title"
    named, _ = lexical_rank(plan_query("S²Q量化"), {"title": "S2Q: Quantization"})
    incidental, _ = lexical_rank(
        plan_query("S²Q量化"), {"title": "Quantization"}, "S2Q in related work"
    )
    assert named > incidental


def test_unknown_explicit_keywords_are_ranked_literally():
    plan = plan_query("未知方案", keywords=["bluewidget"])
    matching, _ = lexical_rank(plan, {"title": "bluewidget"})
    other, _ = lexical_rank(plan, {"title": "unrelated"}, backend_score=500)
    assert matching > other


def test_year_and_condition_only_offer_evidence_hints():
    plan = plan_query("2026年 Lean 定理证明，数据有限")
    score, reasons = lexical_rank(
        plan, {"title": "Lean theorem proving with limited data", "year": 2026}
    )
    assert score > 0
    assert reasons["year_weight"] == 4
    assert reasons["needs_evidence_verification"]
    assert all(condition["needs_evidence_verification"] for condition in reasons["conditions"])


def test_wrapped_explicit_title_gets_exact_title_weight():
    title = "A New Graph Partition Algorithm: Experiments and Analysis"
    plan = plan_query("按英文题名检索：" + title + "。")
    exact, reasons = lexical_rank(plan, {"title": title})
    generic, _ = lexical_rank(plan, {"title": "Graph Partition Algorithm"})
    assert exact > generic + 1000
    assert reasons["matches"][0]["kind"] == "exact_title"


def test_generic_title_case_words_cannot_outscore_multiple_scientific_topics():
    plan = plan_query("Theorem-Provers Self-Correction 随机优化中的证明义务")
    task_only, _ = lexical_rank(plan, {"title": "Theorem-Provers Self-Correction"})
    science, _ = lexical_rank(plan, {"title": "Stochastic Optimization and Proof Obligations"})
    assert science > task_only


def test_summaries_body_tags_authors_urls_and_remote_scores_do_not_change_rank():
    plan = plan_query("扩散采样")
    baseline, reasons = lexical_rank(plan, {"title": "Unrelated"})
    polluted, polluted_reasons = lexical_rank(
        plan,
        {
            "title": "Unrelated",
            "summary": "Diffusion sampling",
            "content": "Diffusion sampling",
            "tags": ["Diffusion sampling"],
            "authors": ["Diffusion sampling"],
            "source_urls": ["Diffusion sampling"],
            "domains": ["Diffusion sampling"],
        },
        snippet="Diffusion sampling",
        backend_score=10000,
    )
    assert polluted == baseline
    assert polluted_reasons == reasons
