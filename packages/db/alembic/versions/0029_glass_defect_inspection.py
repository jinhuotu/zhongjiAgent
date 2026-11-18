"""glass_inspection_tasks + glass_defect_boxes（玻璃瑕疵检测）

Revision ID: 0029_glass_defect_inspection
Revises: 0028_auditor_logs_menu
Create Date: 2026-10-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_glass_defect_inspection"
down_revision: Union[str, None] = "0028_auditor_logs_menu"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "glass_inspection_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_no", sa.String(length=32), nullable=False, comment="业务编号"),
        sa.Column("image_path", sa.String(length=512), nullable=False, comment="图片相对storage_root的路径"),
        sa.Column("original_name", sa.String(length=255), nullable=True, comment="原始文件名"),
        sa.Column("image_width", sa.Integer(), nullable=True, comment="原图宽"),
        sa.Column("image_height", sa.Integer(), nullable=True, comment="原图高"),
        sa.Column("model_version", sa.String(length=32), nullable=True, comment="推理使用的模型版本"),
        sa.Column("model_id", sa.String(length=64), nullable=True, comment="关联模型配置public_id"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="upload", comment="来源：upload/batch"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending", comment="状态：pending/running/success/failed"),
        sa.Column("defect_count", sa.Integer(), nullable=False, server_default="0", comment="瑕疵框数量"),
        sa.Column("max_score", sa.Float(), nullable=True, comment="最高置信度"),
        sa.Column("conclusion", sa.String(length=64), nullable=True, comment="结论（演示版）"),
        sa.Column("grade", sa.String(length=16), nullable=True, comment="等级（演示版恒为空）"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True, comment="推理耗时毫秒"),
        sa.Column("error_message", sa.String(length=512), nullable=True, comment="失败原因"),
        sa.Column("review_status", sa.String(length=16), nullable=False, server_default="unreviewed", comment="复核状态：unreviewed/corrected/rejected"),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True, comment="复核人显示名"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True, comment="复核时间"),
        sa.Column("remark", sa.String(length=512), nullable=True, comment="备注"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        comment="玻璃瑕疵检测任务",
    )
    op.create_index("ix_glass_inspection_tasks_task_no", "glass_inspection_tasks", ["task_no"], unique=True)
    op.create_index("ix_glass_inspection_tasks_status", "glass_inspection_tasks", ["status"], unique=False)
    op.create_index("ix_glass_inspection_tasks_review_status", "glass_inspection_tasks", ["review_status"], unique=False)
    op.create_index("ix_glass_inspection_tasks_created_at", "glass_inspection_tasks", ["created_at"], unique=False)

    op.create_table(
        "glass_defect_boxes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False, comment="所属任务ID"),
        sa.Column("cls_id", sa.Integer(), nullable=True, comment="模型原始类别ID"),
        sa.Column("cls_name_raw", sa.String(length=64), nullable=True, comment="模型原始类别名"),
        sa.Column("defect_type", sa.String(length=32), nullable=True, comment="映射后的标准缺陷类型"),
        sa.Column("mapped", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否映射到标准缺陷类型"),
        sa.Column("confidence", sa.Float(), nullable=False, comment="置信度"),
        sa.Column("x1", sa.Float(), nullable=False, comment="左上X（原图像素）"),
        sa.Column("y1", sa.Float(), nullable=False, comment="左上Y（原图像素）"),
        sa.Column("x2", sa.Float(), nullable=False, comment="右下X（原图像素）"),
        sa.Column("y2", sa.Float(), nullable=False, comment="右下Y（原图像素）"),
        sa.Column("width_mm", sa.Float(), nullable=True, comment="宽（毫米）"),
        sa.Column("height_mm", sa.Float(), nullable=True, comment="高（毫米）"),
        sa.Column("area_mm2", sa.Float(), nullable=True, comment="面积（平方毫米）"),
        sa.Column("is_false_positive", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="人工标记误报"),
        sa.Column("is_missed", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="人工补录漏报"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0", comment="排序"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False, comment="创建时间"),
        sa.ForeignKeyConstraint(["task_id"], ["glass_inspection_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="玻璃瑕疵检测框",
    )
    op.create_index("ix_glass_defect_boxes_task_id", "glass_defect_boxes", ["task_id"], unique=False)


def downgrade() -> None:
    # MySQL 下 task_id 索引被外键占用，不能单独 DROP INDEX，删表时一并清理
    op.drop_table("glass_defect_boxes")
    op.drop_index("ix_glass_inspection_tasks_created_at", table_name="glass_inspection_tasks")
    op.drop_index("ix_glass_inspection_tasks_review_status", table_name="glass_inspection_tasks")
    op.drop_index("ix_glass_inspection_tasks_status", table_name="glass_inspection_tasks")
    op.drop_index("ix_glass_inspection_tasks_task_no", table_name="glass_inspection_tasks")
    op.drop_table("glass_inspection_tasks")
