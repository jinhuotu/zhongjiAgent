from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class Workflow(Base):
    """工作流定义（元数据；图在版本表）。"""

    __tablename__ = "workflows"
    __table_args__ = {"comment": "工作流定义"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="工作流名称")
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    domain: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ai", comment="业务域，默认 ai"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建人用户ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )

    versions: Mapped[list[WorkflowVersion]] = relationship(
        "WorkflowVersion",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="noload",
    )


class WorkflowVersion(Base):
    """工作流版本（草稿/已发布；含 graph_json）。"""

    __tablename__ = "workflow_versions"
    __table_args__ = {"comment": "工作流版本"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    workflow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属工作流ID",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", comment="状态：draft/published"
    )
    graph_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="流程图 JSON（nodes/edges）"
    )
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更说明")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建人用户ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    workflow: Mapped[Workflow] = relationship("Workflow", back_populates="versions")
    runs: Mapped[list[WorkflowRun]] = relationship(
        "WorkflowRun",
        back_populates="version",
        lazy="noload",
    )


class WorkflowRun(Base):
    """工作流一次执行记录。"""

    __tablename__ = "workflow_runs"
    __table_args__ = {"comment": "工作流运行记录"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    workflow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属工作流ID",
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="使用的版本ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="状态：pending/running/done/failed",
    )
    trigger: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="触发方式：trial/api 等"
    )
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="输入 JSON")
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="输出 JSON")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="失败原因")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建人用户ID"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    workflow: Mapped[Workflow] = relationship("Workflow", back_populates="runs")
    version: Mapped[WorkflowVersion] = relationship(
        "WorkflowVersion", back_populates="runs"
    )
    steps: Mapped[list[WorkflowRunStep]] = relationship(
        "WorkflowRunStep",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WorkflowRunStep(Base):
    """工作流运行中的单节点步骤。"""

    __tablename__ = "workflow_run_steps"
    __table_args__ = {"comment": "工作流运行步骤"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属运行ID",
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="节点ID")
    node_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="节点类型：start/end/knowledge/llm/agent/mcp"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="状态：pending/running/done/failed",
    )
    detail_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="步骤详情 JSON"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )

    run: Mapped[WorkflowRun] = relationship("WorkflowRun", back_populates="steps")
