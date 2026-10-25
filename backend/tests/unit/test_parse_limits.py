import io
import multiprocessing
import time
import zipfile

import pytest

from knowledge.adapters.parse_process import BoundedParser
from knowledge.adapters.parser import DOCX, TEXT_PLAIN, DocxParser, TextParser
from knowledge.domain.errors import ParseFailed


class BlockingParser(TextParser):
    def parse(self, data, media_type, filename):
        time.sleep(60)
        return []


@pytest.mark.parametrize("entries,size_limit", [(1, 1000), (10, 2)])
def test_docx_preflight_rejects_limits(entries, size_limit):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("a", "abc")
        archive.writestr("b", "def")
    with pytest.raises(ParseFailed):
        DocxParser(entries, size_limit).parse(stream.getvalue(), DOCX, "test.docx")


def test_timeout_reaps_child_and_next_parse_succeeds(settings):
    before = {child.pid for child in multiprocessing.active_children()}
    bounded = settings.model_copy(update={"parse_timeout_seconds": 0.3})
    with pytest.raises(ParseFailed):
        BoundedParser(BlockingParser(), bounded).parse(b"hello", TEXT_PLAIN, "a.txt")
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert (
        BoundedParser(TextParser(), settings).parse(b"hello", TEXT_PLAIN, "a.txt")[0].text
        == "hello"
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"parse_memory_bytes": 1},
        {"parse_cpu_seconds": 0.000001},
        {"parse_output_bytes": 64},
    ],
)
def test_process_limits_fail_safely(settings, limits):
    bounded = settings.model_copy(update=limits)
    with pytest.raises(ParseFailed):
        BoundedParser(TextParser(), bounded).parse(b"text" * 100, TEXT_PLAIN, "a.txt")
