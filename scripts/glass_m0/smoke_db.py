# -*- coding: utf-8 -*-
"""M1 冒烟测试：验证 glass 两张表的 ORM 插入、默认值、级联删除。运行后可删除。"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from common.config import get_settings
from db.models import GlassDefectBox, GlassInspectionTask

url = get_settings().database_url.replace("mysql+asyncmy://", "mysql+pymysql://")
engine = create_engine(url)

with Session(engine) as s:
    s.execute(text("DELETE FROM glass_inspection_tasks WHERE task_no LIKE 'GD-TEST%'"))
    s.commit()

    task_no = "GD-TEST-" + uuid.uuid4().hex[:6].upper()
    task = GlassInspectionTask(
        task_no=task_no,
        image_path="glass/test.jpg",
        status="success",
        defect_count=1,
        max_score=0.9,
        conclusion="defect_pending_review",
    )
    s.add(task)
    s.flush()

    box = GlassDefectBox(
        task_id=task.id,
        cls_id=2,
        cls_name_raw="scratch",
        defect_type="重划伤",
        confidence=0.9,
        x1=1,
        y1=2,
        x2=3,
        y2=4,
        sort_order=0,
    )
    s.add(box)
    s.commit()

    print("task id:", task.id)
    print("created_at set:", task.created_at is not None)
    print("default review_status:", task.review_status)
    print("default source:", task.source)
    print("default grade:", task.grade)
    print("box default mapped:", box.mapped)
    print("box default is_false_positive:", box.is_false_positive)

    task_id = task.id
    s.execute(text("DELETE FROM glass_inspection_tasks WHERE id=:i"), {"i": task_id})
    s.commit()
    left = s.execute(
        text("SELECT COUNT(*) FROM glass_defect_boxes WHERE task_id=:i"), {"i": task_id}
    ).scalar()
    print("after task delete, boxes left (级联删除应为0):", left)

    remaining = s.execute(
        text("SELECT COUNT(*) FROM glass_inspection_tasks WHERE task_no LIKE 'GD-TEST%'")
    ).scalar()
    print("remaining test tasks:", remaining)
