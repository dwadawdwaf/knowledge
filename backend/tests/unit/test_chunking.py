from __future__ import annotations

from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import Locator, ParsedBlock


def _block(text: str, start: int = 0) -> ParsedBlock:
    return ParsedBlock(
        text=text,
        locator=Locator(type="txt", char_start=start, char_end=start + len(text), precision="char"),
    )


def test_short_block_single_chunk():
    chunks = chunk_blocks([_block("会议室使用后应清理现场。")], max_chars=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].locator["char_start"] == 0


def test_long_block_splits_with_overlap():
    text = "甲" * 250
    chunks = chunk_blocks([_block(text)], max_chars=100, overlap=20)
    assert len(chunks) >= 3
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert chunks[1].locator["char_start"] < chunks[0].locator["char_end"]


def test_offsets_are_absolute_across_blocks():
    blocks = [_block("第一段。", 0), _block("第二段。", 100)]
    chunks = chunk_blocks(blocks, max_chars=50, overlap=5)
    assert chunks[0].locator["char_start"] == 0
    assert chunks[1].locator["char_start"] == 100


def test_blank_blocks_skipped():
    chunks = chunk_blocks([_block("   "), _block("有效内容。")], max_chars=50, overlap=5)
    assert len(chunks) == 1
    assert "有效内容" in chunks[0].text


def test_empty_input():
    assert chunk_blocks([]) == []
