"""归档知识库：列表不可见、不能作为检索范围，但数据保留且可恢复。"""

from __future__ import annotations

import os

import pytest

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.services import knowledge as knowledge_service

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_archived_kb_is_hidden_from_list_and_scope(session):
    actor = Actor(subject_id="archive-user", tenant_id="default")
    keep = knowledge_service.create(session, actor, "保留库")
    archived = knowledge_service.create(session, actor, "归档库")

    kb_repo.mark_archived(session, archived)
    session.flush()

    names = [kb.name for kb in kb_repo.list_owned(session, actor)]
    assert names == ["保留库"]

    # 不能作为检索或上传的范围
    with pytest.raises(NotFound):
        kb_repo.get_active(session, actor, [archived.id])

    # 记录仍在，便于直接改回 active 恢复，不经过删除流程
    assert kb_repo.get_owned(session, actor, archived.id).status == Lifecycle.ARCHIVED

    archived.status = Lifecycle.ACTIVE
    session.flush()
    names = [kb.name for kb in kb_repo.list_owned(session, actor)]
    assert set(names) == {"保留库", "归档库"}
    assert kb_repo.get_active(session, actor, [archived.id])[0].id == archived.id
    assert keep.status == Lifecycle.ACTIVE
