"""分块结果视图：按序返回、与已发布 build 对齐、他人不可见。"""

from __future__ import annotations

import pathlib

import pytest

from knowledge.domain.entities import Actor
from knowledge.domain.errors import NotFound
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SAMPLE = (BACKEND / "testdata" / "engineering" / "handbook-sections.md").read_bytes()


def _publish(session, settings, deps, actor: Actor, *, process: bool = True):
    kb = knowledge_service.create(session, actor, f"手册-{actor.subject_id}")
    upload = document_service.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="handbook-sections.md",
        data=SAMPLE,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    if process:
        assert process_next(session, deps) is True
    return kb, upload


def test_chunks_are_ordered_and_carry_locators(session, settings, deps):
    actor = Actor(subject_id="ui-owner", tenant_id="default")
    _, upload = _publish(session, settings, deps, actor)

    view = document_service.chunks(session, actor, upload.document_id)

    assert view["document_id"] == str(upload.document_id)
    assert view["build_id"]
    assert view["total"] == len(view["items"]) >= 3
    assert view["truncated"] is False
    ordinals = [item["ordinal"] for item in view["items"]]
    assert ordinals == sorted(ordinals)
    spans = [(item["locator"]["char_start"], item["locator"]["char_end"]) for item in view["items"]]
    assert all(end > start for start, end in spans)
    assert all(item["text"].strip() for item in view["items"])
    # 长段落按窗口滑动，相邻分块应共享部分字符，视图才能画出重叠。
    assert any(spans[index + 1][0] < spans[index][1] for index in range(len(spans) - 1))


def test_chunks_respect_limit(session, settings, deps):
    actor = Actor(subject_id="ui-limit", tenant_id="default")
    _, upload = _publish(session, settings, deps, actor)

    view = document_service.chunks(session, actor, upload.document_id, limit=1)

    assert len(view["items"]) == 1
    assert view["total"] > 1
    assert view["truncated"] is True


def test_chunks_are_empty_before_first_publish(session, settings, deps):
    actor = Actor(subject_id="ui-pending", tenant_id="default")
    _, upload = _publish(session, settings, deps, actor, process=False)

    view = document_service.chunks(session, actor, upload.document_id)

    assert view["items"] == []
    assert view["total"] == 0
    assert view["build_id"] is None


def test_chunks_hidden_from_other_subject(session, settings, deps):
    owner = Actor(subject_id="ui-owner-2", tenant_id="default")
    _, upload = _publish(session, settings, deps, owner)

    intruder = Actor(subject_id="ui-intruder", tenant_id="default")
    with pytest.raises(NotFound):
        document_service.chunks(session, intruder, upload.document_id)
