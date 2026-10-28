"""孤儿回收的安全阀：整卷清空只可能是卷接错了库。"""

from __future__ import annotations

from knowledge.services.cleanup import ORPHAN_BATCH_LIMIT, looks_like_foreign_volume


def test_small_orphan_batches_are_reclaimed() -> None:
    assert looks_like_foreign_volume(0, 0) is False
    assert looks_like_foreign_volume(3, 40) is False
    assert looks_like_foreign_volume(ORPHAN_BATCH_LIMIT, ORPHAN_BATCH_LIMIT * 20) is False


def test_foreign_volume_is_detected() -> None:
    # 另一个库的对象全被当成孤儿：数量大且占绝大多数
    assert looks_like_foreign_volume(60, 60) is True
    # 量大但只占少数：正常的库内清理，照常回收
    assert looks_like_foreign_volume(30, 200) is False
