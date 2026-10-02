from packages.knowledge.query_terms import query_variants


def test_long_scientific_phrases_take_precedence_over_component_words():
    assert query_variants("一致性模型为什么可以一步生成？") == [
        "一致性模型为什么可以一步生成？",
        "consistency models one step generation",
    ]


def test_original_query_is_preserved_and_english_identifiers_are_not_discarded():
    variants = query_variants("DiT 量化时的激活异常值")
    assert variants[0] == "DiT 量化时的激活异常值"
    assert "DiT" in variants[1] and "activation outliers" in variants[1]
    assert query_variants("DDIM") == ["DDIM"]
