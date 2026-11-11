"""新的 token 预算、表格原始行与 PDF 路由反例。"""

import math

import pytest
from fpdf import FPDF
from pypdf import PageObject

from knowledge.adapters.parser import APPLICATION_PDF, CsvParser, PdfParser, PlainPdfProbe
from knowledge.adapters.ragflow_text import (
    _clean_query,
    _compile_chinese_clauses,
    _tokenized_clauses,
    count_tokens,
    query_terms,
    search_fields,
)
from knowledge.adapters.ragflow_tokenizer import tokenizer
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.ragflow_chunking import TOKEN_CAP, chunk_ragflow
from knowledge.domain.ragflow_query import (
    build_query_terms,
    phrase_queries,
    position_key,
    term_weight,
)


def test_long_chinese_chunk_keeps_every_character_and_heading():
    text = "# 设备巡检\n" + "红色信号灯显示阀门状态" * 180
    locator = Locator(type="md", char_start=0, char_end=len(text))
    chunks = chunk_ragflow([ParsedBlock(text, locator)], count_tokens)
    assert len(chunks) > 1
    assert all(count_tokens(chunk.text) <= TOKEN_CAP for chunk in chunks)
    assert all(chunk.structure["section_path"] == ["# 设备巡检"] for chunk in chunks)
    slices = [
        text[source["locator"]["char_start"]:source["locator"]["char_end"]]
        for chunk in chunks for source in chunk.locator["sources"]
    ]
    assert "".join(slices) == text


def test_csv_indexes_only_same_table_header_and_retains_real_rows():
    blocks = CsvParser(rows_per_block=2).parse(
        "型号,额度\nRAG-001,12\n\nRAG-002,37\n".encode(), "text/csv", "sheet.csv",
    )
    chunks = chunk_ragflow(blocks, count_tokens)
    assert [chunk.locator["row_start"] for chunk in chunks] == [1, 2, 4]
    assert chunks[2].structure["table_header"] == "型号\t额度"
    indexed = search_fields(chunks[2].text, "方案", [], chunks[2].structure["table_header"])
    assert "rag-002" in indexed.terms and "额度" in indexed.fields["important"]
    assert ["rag-002"] in query_terms("ＲＡＧ-００２ 的额度").groups


def test_csv_quoted_line_breaks_and_tabs_keep_their_own_column_and_row():
    source = '型号,说明,额度\nRX-80,"高温\t室内\n恒温",60\nRX-81,"低温\t室外"\nRX-82,夜间,\n'
    blocks = CsvParser(rows_per_block=2).parse(source.encode(), "text/csv", "table.csv")
    chunks = chunk_ragflow(blocks, len)

    assert [chunk.locator["row_start"] for chunk in chunks] == [1, 2, 3, 4]
    multiline, uneven, empty = chunks[1:]
    assert multiline.text == "RX-80\t高温\t室内\n恒温\t60"
    assert blocks[0].text[multiline.locator["char_start"]:multiline.locator["char_end"]] == multiline.text
    assert multiline.structure["table_fields"] == ["型号: RX-80", "说明: 高温\t室内\n恒温", "额度: 60"]
    assert uneven.structure["table_header"] == "" and uneven.structure["table_fields"] == []
    assert empty.structure["table_header"] == "型号\t说明\t额度"
    assert empty.structure["table_fields"] == ["型号: RX-82", "说明: 夜间"]


def test_pdf_probe_rejects_later_page_with_no_text_or_parallel_columns():
    for blank in (True, False):
        pdf = FPDF()
        pdf.set_font("Helvetica", size=12)
        pdf.add_page()
        pdf.text(15, 20, "This page contains a plain single column.")
        pdf.add_page()
        if not blank:
            pdf.text(15, 20, "Left column starts here.")
            pdf.text(115, 20, "Right column starts here.")
        assert not PlainPdfProbe().parse(bytes(pdf.output()), APPLICATION_PDF, "mixed.pdf")


def test_pdf_probe_rejects_mismatched_reading_order_in_one_column():
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    pdf.add_page()
    pdf.text(15, 40, "Second line of the document.")
    pdf.text(15, 20, "First line of the document.")
    data = bytes(pdf.output())

    assert PdfParser().parse(data, APPLICATION_PDF, "single.pdf")[0].text.startswith("Second line")
    assert PlainPdfProbe().parse(data, APPLICATION_PDF, "single.pdf") == []


def test_pdf_probe_reuses_validated_text_and_preserves_local_locators(monkeypatch):
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for number in (1, 2):
        pdf.add_page()
        pdf.text(15, 20, f"Single column on page {number}.")
    data = bytes(pdf.output())
    expected = PdfParser().parse(data, APPLICATION_PDF, "simple.pdf")
    extract = PageObject.extract_text
    calls = []

    def counted(page, **kwargs):
        calls.append(kwargs.get("extraction_mode", "plain"))
        return extract(page, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", counted)
    assert PlainPdfProbe().parse(data, APPLICATION_PDF, "simple.pdf") == expected
    assert calls == ["layout", "plain", "layout", "plain"]


def test_naive_merge_checks_running_total_before_appending_the_next_section():
    bodies = ["甲" * 300, "乙" * 250, "丙" * 12]
    blocks = [
        ParsedBlock(body, Locator(type="txt", char_start=offset, char_end=offset + len(body)))
        for body, offset in zip(bodies, (0, 302, 554), strict=True)
    ]
    chunks = chunk_ragflow(blocks, len)
    assert [chunk.text for chunk in chunks] == [bodies[0] + "\n" + bodies[1], bodies[2]]
    assert [source["locator"]["char_start"] for source in chunks[0].locator["sources"]] == [0, 302]


def test_short_paragraph_respects_sentence_boundaries_and_coalesces_sources():
    lead = "甲" * 509
    paragraph = "乙。\r\n\r\n丙？！丁  "
    start = 520
    blocks = [
        ParsedBlock(lead, Locator(type="txt", char_start=0, char_end=len(lead))),
        ParsedBlock(paragraph, Locator(type="txt", char_start=start, char_end=start + len(paragraph))),
    ]
    chunks = chunk_ragflow(blocks, len)
    assert [chunk.text for chunk in chunks] == [lead + "\n乙。\r\n\r\n", "丙？！丁  "]
    sources = [chunk.locator["sources"] for chunk in chunks]
    assert [len(items) for items in sources] == [2, 1]
    assert sources[0][1]["locator"]["char_end"] == sources[1][0]["locator"]["char_start"] == start + 6
    for chunk, items in zip(chunks, sources, strict=True):
        for item in items:
            location = item["locator"]
            raw, origin = (lead, 0) if location["char_start"] == 0 else (paragraph, start)
            assert raw[location["char_start"] - origin:location["char_end"] - origin] == (
                chunk.text[item["chunk_start"]:item["chunk_end"]]
            )


def test_sentence_splitting_keeps_non_prose_blocks_atomic():
    lead = "甲" * 500
    code = "first();\r\nsecond();\nthird();"
    chunks = chunk_ragflow([
        ParsedBlock(lead, Locator(type="pdf", page=1, char_start=0, char_end=len(lead))),
        ParsedBlock(code, Locator(type="pdf", page=1, char_start=0, char_end=len(code)), kind="code"),
    ], len)
    assert [chunk.text for chunk in chunks] == [lead, code]
    assert chunks[1].structure["kind"] == "code"


def test_long_code_prefers_line_boundaries_without_reinterpreting_comments():
    first_line = "a" * 270 + "; # 第一行\r\n"
    second_line = "b" * 270 + "; # 第二行\r\n"
    text = "```text\r\n" + first_line + second_line + "```\r\n"
    chunks = chunk_ragflow([ParsedBlock(text, Locator(type="md", char_start=0, char_end=len(text)), kind="code")], len)
    assert [chunk.text for chunk in chunks] == ["```text\r\n" + first_line, second_line + "```\r\n"]
    assert all(chunk.structure["kind"] == "code" and not chunk.structure["section_path"] for chunk in chunks)


def test_ragflow_weights_terms_and_adjacent_pairs_in_one_denominator():
    query = build_query_terms(
        [["alpha"], ["beta"]], [1.0, 3.0], synonyms={"alpha": ("alias",)},
        ranking=[("alpha", 1.0), ("alias", 2.0), ("beta", 3.0), ("alpha", 1.0)],
    )
    # 重复词累计贡献，同义词和两者构成的相邻词组分别占权重。
    assert query.groups[0] == ["alpha", "alias"]
    assert query.coverage(["alpha", "alpha"]) == pytest.approx(0.8 / 7.6)
    assert query.coverage(["alias"]) == pytest.approx(0.8 / 7.6)
    assert query.coverage(["beta", "pair:beta alpha"]) == pytest.approx(3.0 / 7.6)
    assert query.coverage(query.features) == pytest.approx(1.0)


def test_term_weight_uses_recursive_frequency_and_upstream_category_factors():
    frequencies = {"机床": 600, "巡检": 1200, "附件": 3600}
    pieces = {"机床巡检附件": "机床巡检 附件", "机床巡检": "机床 巡检", "pump/value": "pump value"}

    def weight(term, tag="", entity=""):
        return term_weight(
            term, frequency=lambda word: frequencies.get(word, 0),
            fine_grained=lambda word: pieces.get(word, word), tag=tag, entity=entity,
        )

    def base(corpus, document):
        return 0.3 * math.log10(10 + (10_000_000 - corpus + 0.5) / (corpus + 0.5)) + (
            0.7 * math.log10(10 + (1_000_000_000 - document + 0.5) / (document + 0.5))
        )

    assert weight("机床巡检附件") == pytest.approx(base(600 / 36, 3))
    assert weight("未知型号") == pytest.approx(base(10, 3))
    assert weight("pump/value") == pytest.approx(base(50, 50))
    assert weight("42", tag="-1") == pytest.approx(4 * base(3, 5))
    assert weight("机床", tag="ns", entity="corp") == pytest.approx(9 * base(600, 3))
    assert weight("a") == pytest.approx(0.01 * base(300, 300))


def test_ranking_stream_deduplicates_body_and_uses_real_title_and_header():
    indexed = search_fields("plum pear plum fig", "manual.txt", ["method"], "column", row_fields=["column 32"])
    assert "pair:plum fig" in indexed.fields["content"]
    assert "pair:plum fig" not in indexed.fields["ranking"]
    assert {"pair:pear fig", "pair:fig manual", "pair:manual method", "column"} <= set(indexed.fields["ranking"])
    assert "pair:column 32" in indexed.fields["content"]
    assert "32" not in indexed.fields["ranking"]


def test_ranking_preserves_stop_words_between_adjacent_terms():
    separated = search_fields("plum and fig", "", [])
    adjacent = search_fields("plum fig", "", [])
    assert "and" not in separated.fields["content"]
    assert "pair:plum and" in separated.fields["ranking"]
    assert "pair:plum fig" not in separated.fields["ranking"]
    assert "pair:plum fig" in adjacent.fields["ranking"]
    terms = build_query_terms([["plum"], ["fig"]], [1.0, 1.0], ranking=[("plum", 1.0), ("fig", 1.0)])
    assert terms.coverage(separated.fields["ranking"]) == pytest.approx(4 / 7)
    assert terms.coverage(adjacent.fields["ranking"]) == pytest.approx(1)


def test_title_merger_prepends_parent_text_only_to_its_own_children():
    text = "前言\n# 第一章\n总要求\n## A 节\n甲条件\n## B 节\n乙条件\n# 第二章\n其他要求"
    chunks = chunk_ragflow([ParsedBlock(text, Locator(type="md", char_start=0, char_end=len(text)))], len)
    assert [chunk.text.strip() for chunk in chunks] == [
        "前言", "# 第一章\n总要求\n## A 节\n甲条件",
        "# 第一章\n总要求\n## B 节\n乙条件", "# 第二章\n其他要求",
    ]
    assert chunks[2].structure["section_path"] == ["# 第一章", "## B 节"]
    for chunk in chunks:
        for source in chunk.locator["sources"]:
            location = source["locator"]
            assert chunk.text[source["chunk_start"]:source["chunk_end"]] == text[
                location["char_start"]:location["char_end"]
            ]


def test_title_merger_groups_below_third_level_and_emits_parent_table_once():
    values = ["# Parent", "row-a\tvalue", "## Middle", "### Section", "#### First", "#### Second", "## Sibling"]
    blocks = [
        ParsedBlock(
            value, Locator(type="md", char_start=index * 100, char_end=index * 100 + len(value)),
            kind="table" if index == 1 else "text", table_id="table-1" if index == 1 else None,
        )
        for index, value in enumerate(values)
    ]
    chunks = chunk_ragflow(blocks, len)
    assert [chunk.text for chunk in chunks] == [
        "row-a\tvalue", "# Parent\n## Middle\n### Section\n#### First\n#### Second",
        "# Parent\n## Sibling",
    ]
    assert chunks[1].structure["section_path"] == ["# Parent", "## Middle", "### Section"]


def test_title_merger_keeps_long_parent_intro_once():
    intro = "甲" * 530
    text = f"# Parent\n{intro}\n## First\nalpha\n## Second\nbeta"
    chunks = chunk_ragflow([ParsedBlock(text, Locator(type="md", char_start=0, char_end=len(text)))], len)
    assert sum(chunk.text.count("甲") for chunk in chunks) == len(intro)
    assert all(len(chunk.text) <= TOKEN_CAP for chunk in chunks)
    assert [chunk.structure["section_path"] for chunk in chunks[-2:]] == [
        ["# Parent", "## First"], ["# Parent", "## Second"],
    ]


def test_title_merger_keeps_cumulative_intro_once_and_deeper_paths_on_continuations():
    text = (
        f"# 总则\n{'甲' * 270}\n## 中间层\n{'乙' * 270}\n### 范围\n"
        f"#### 温度\n{'丙' * 510}\n#### 压力\n{'丁' * 510}"
    )
    chunks = chunk_ragflow([ParsedBlock(text, Locator(type="md", char_start=0, char_end=len(text)))], len)
    assert all(len(chunk.text) <= TOKEN_CAP for chunk in chunks)
    for marker, expected in (("甲", 270), ("乙", 270), ("丙", 510), ("丁", 510)):
        assert sum(chunk.text.count(marker) for chunk in chunks) == expected
    paths = {marker: next(chunk for chunk in chunks if chunk.text.startswith(marker)) for marker in ("丙", "丁")}
    assert paths["丙"].structure["section_path"] == ["# 总则", "## 中间层", "### 范围", "#### 温度"]
    assert paths["丁"].structure["section_path"] == ["# 总则", "## 中间层", "### 范围", "#### 压力"]
    assert "压力" in search_fields(paths["丁"].text, "文档", paths["丁"].structure["section_path"]).fields["title"]
    for chunk in chunks:
        for source in chunk.locator["sources"]:
            locator = source["locator"]
            assert text[locator["char_start"]:locator["char_end"]] == chunk.text[
                source["chunk_start"]:source["chunk_end"]
            ]


def test_title_merger_uses_real_parents_when_heading_levels_are_skipped():
    text = "# 总则\n### 甲节\n甲正文\n### 乙节\n乙正文\n##### 细则\n细则正文"
    chunks = chunk_ragflow([ParsedBlock(text, Locator(type="md", char_start=0, char_end=len(text)))], len)
    assert [chunk.structure["section_path"] for chunk in chunks] == [
        ["# 总则", "### 甲节"], ["# 总则", "### 乙节", "##### 细则"],
    ]
    assert "### 甲节" not in chunks[1].text


def test_table_chunker_indexes_paired_column_names_without_changing_row_evidence():
    text = "device,temperature\nRX-001,145\nRX-002\n"
    blocks = CsvParser().parse(text.encode(), "text/csv", "table.csv")
    chunks = chunk_ragflow(blocks, len)
    assert chunks[1].text == "RX-001\t145"
    assert chunks[1].structure["table_fields"] == ["device: RX-001", "temperature: 145"]
    assert chunks[2].structure["table_fields"] == []
    indexed = search_fields(
        chunks[1].text, "table.csv", [], chunks[1].structure["table_header"],
        row_fields=chunks[1].structure["table_fields"],
    )
    assert "pair:temperatur 145" in indexed.terms


def test_upstream_dictionary_alias_is_discounted_in_recall_and_independent_in_ranking():
    query = query_terms("阿为特")
    assert len(query.groups) == 1 and "873693" in query.groups[0]
    assert query.recall_features[0][-1] == pytest.approx(0.2)
    indexed = search_fields("设备 873693", "阿为特.txt", [], "设备编号")
    assert 0 < query.coverage(indexed.fields["title"]) < 1
    assert 0 < query.coverage(["873693"]) < 1
    # 候选的正文编号接在标题前，上游重复标题还产生一个标题自身的相邻词组。
    assert set(query.features).intersection(indexed.fields["ranking"]) == {
        "873693", "阿为特", "pair:873693 阿为特", "pair:阿为特 阿为特",
    }
    assert "txt" not in indexed.fields["title"]
    assert "873693" in indexed.fields["content"]


def test_wordnet_fallback_preserves_primary_and_matches_long_aliases_exactly():
    query = query_terms("usa")
    assert query.groups == [["usa", "pair:unit state", "phrase:unit state armi"]]
    assert query.recall_features[0][-1] == pytest.approx(0.2)
    assert query.coverage(["unit state"]) > 0
    assert query.coverage(["pair:unit state", "phrase:unit state armi"]) == 0
    exact = search_fields("United States Army", "report.txt", [])
    separated = search_fields("United States national Army", "report.txt", [])
    assert "phrase:unit state armi" in exact.fields["content"]
    assert "phrase:unit state armi" not in separated.fields["content"]
    assert "car" in query_terms("automobile").groups[0]


def test_infinity_tokenizer_uses_pinned_dictionary_and_normalizes_english():
    analyzer = tokenizer()
    assert analyzer.tokenize("南京市长江大桥") == "南京市 长江大桥"
    assert analyzer.fine_grained_tokenize("南京市 长江大桥") == "南京 市 长江 大桥"
    assert analyzer.freq("南京市") == 6738 and analyzer.tag("南京市") == "ns"
    assert analyzer.tokenize("繁體資料處理") == "繁体 资料 处理"
    indexed = search_fields("Automobiles were inspected with ＲＸ－８４０.", "manual.txt", [])
    assert "automobil" in indexed.fields["content"]
    assert "automobiles" in indexed.fields["content_fine"]
    assert "rx-840" in indexed.fields["content"]
    assert "rx" not in indexed.terms


def test_slop_two_uses_the_span_of_adjusted_positions():
    words = ("长江", "大桥", "维修")
    river, bridge, maintenance = map(position_key, words)
    exact, near = phrase_queries(words)
    assert exact == f"{river} <1> {bridge} <1> {maintenance}"
    assert f"({bridge} <1> {river} <2> {maintenance})" in near
    assert f"({bridge} <2> {river} <1> {maintenance})" not in near


def test_chinese_query_preserves_clause_order_and_limits_optional_expansion():
    cleaned = _clean_query("請問，長江大橋怎麼維修？")
    assert cleaned == "长江大桥维修"
    keywords, _ = _compile_chinese_clauses(_tokenized_clauses(cleaned.split()))
    assert keywords == ["长江大桥维修", "长江大桥", "长江", "大桥", "维修"]
    keywords, compiled = _compile_chinese_clauses(_tokenized_clauses(["长江大桥"] * 12))
    assert keywords == (
        ["长江大桥", "长江大桥", "长江", "大桥"] * 8 + ["长江大桥"] * 4
    )
    assert len(compiled) == 7
    fallback = query_terms("在 " * 32 + "火山")
    assert fallback.groups == [["火山"]] and fallback.has_recall


def test_english_query_limits_single_terms_and_keeps_tail_phrases():
    query = query_terms("alpha " * 256 + "plum fig")
    assert ["plum"] not in query.groups and ["fig"] not in query.groups
    assert ["pair:plum fig"] in query.groups
    assert {"plum", "fig", "pair:plum fig"} <= set(query.features)


def test_query_cleanup_keeps_identifiers_in_mixed_queries():
    query = query_terms("請問，ＲＸ－８４０如何接到ＺＸ／４２？")
    assert ["rx-840"] in query.groups
    assert ["zx/42"] in query.groups
    assert "rx-840" in query.features and "zx/42" in query.features
    assert "请问" not in query.features and "如何" not in query.features
