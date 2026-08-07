"""为全库表/字段补充 MySQL COMMENT（仅注释，不改类型与数据）

Revision ID: 0019_mysql_comments
Revises: 0018_kb_purpose
Create Date: 2026-08-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_mysql_comments"
down_revision: Union[str, None] = "0018_kb_purpose"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 表注释
TABLE_COMMENTS: dict[str, str] = {
    "users": "系统用户",
    "roles": "角色",
    "user_roles": "用户-角色关联",
    "knowledge_bases": "知识库",
    "knowledge_documents": "知识库文档元数据",
    "chat_sessions": "AI 问答会话",
    "chat_messages": "AI 问答消息",
    "model_configs": "模型配置（对话/Embedding）",
    "hot_configs": "全局热配置",
    "hot_config_audits": "热配置变更审计",
    "furnaces": "车式窑台账",
    "kiln_process_samples": "窑炉分钟级工况样本",
    "ai_reports": "AI 智能报告",
    "prod_systems": "生产侧系统（SCADA）",
    "prod_tags": "生产测点定义",
    "prod_samples": "生产测点时序样本",
    "prod_alarms": "生产报警",
    "prod_commands": "生产控制指令",
    "gov_tasks": "数据治理任务",
    "mcp_servers": "MCP 服务配置",
    "mcp_tools": "MCP 工具缓存",
    "prompts": "系统提示词",
    "scenario_agents": "场景智能体配置",
}

# 字段注释：table -> { column: comment }
COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "users": {
        "id": "主键",
        "username": "登录用户名",
        "email": "邮箱",
        "hashed_password": "密码哈希",
        "display_name": "显示名称",
        "department": "部门",
        "phone": "手机号",
        "is_active": "是否启用",
        "is_superuser": "是否超级管理员",
        "remark": "备注",
        "last_login_at": "最近登录时间",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "roles": {
        "id": "主键",
        "code": "角色编码",
        "name": "角色名称",
        "description": "角色说明",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "user_roles": {
        "id": "主键",
        "user_id": "用户ID",
        "role_id": "角色ID",
    },
    "knowledge_bases": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "知识库名称",
        "description": "描述",
        "purpose": "用途：rag=AI知识库",
        "status": "状态",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "knowledge_documents": {
        "id": "主键",
        "public_id": "对外ID",
        "base_id": "所属知识库ID",
        "name": "资料名称",
        "source": "来源：file/text/url",
        "kind": "类型：doc/3d等",
        "file_type": "文件扩展名",
        "size": "文件大小(字节)",
        "url": "来源URL",
        "storage_path": "存储相对路径",
        "file_key": "文件键",
        "preview_url": "预览地址",
        "summary": "摘要",
        "char_count": "字符数",
        "chunk_count": "向量切块数",
        "tags": "标签JSON",
        "uploader": "上传人",
        "status": "状态：parsing/ready/failed",
        "error_msg": "失败原因",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "chat_sessions": {
        "id": "主键",
        "public_id": "对外ID",
        "user_id": "用户ID",
        "title": "会话标题",
        "title_auto": "是否自动生成标题",
        "mode": "模式：fast/deep",
        "summary": "会话摘要",
        "knowledge_base_ids": "关联知识库ID列表",
        "message_count": "消息条数",
        "last_message_at": "最近消息时间",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "chat_messages": {
        "id": "主键",
        "public_id": "对外ID",
        "session_id": "会话ID",
        "role": "角色：user/assistant/tool等",
        "content": "消息正文",
        "mode": "生成模式",
        "refs": "引用片段JSON",
        "stream_msg_id": "Redis Stream消息ID",
        "model_name": "模型名",
        "prompt_tokens": "提示词token数",
        "completion_tokens": "补全token数",
        "total_tokens": "总token数",
        "tool_name": "工具名",
        "tool_input": "工具入参JSON",
        "tool_output": "工具出参JSON",
        "tool_error": "工具错误信息",
        "tool_duration_ms": "工具耗时毫秒",
        "created_at": "创建时间",
    },
    "model_configs": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "配置名称",
        "kind": "类型：llm/embedding",
        "api_base": "API Base URL",
        "api_key": "API Key",
        "model_name": "模型名称",
        "temperature": "温度",
        "timeout_seconds": "超时秒数",
        "embedding_dim": "向量维度",
        "remark": "备注",
        "enabled": "是否启用",
        "scope_fast": "绑定快速对话",
        "scope_deep": "绑定深度对话",
        "scope_embedding": "绑定Embedding",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "hot_configs": {
        "id": "主键",
        "config_key": "配置键",
        "value": "配置值",
        "description": "说明",
        "version": "版本号",
        "enabled": "是否启用",
        "updated_by": "更新人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "hot_config_audits": {
        "id": "主键",
        "config_key": "配置键",
        "old_value": "变更前值",
        "new_value": "变更后值",
        "version": "版本号",
        "action": "动作",
        "operator_id": "操作人用户ID",
        "remark": "备注",
        "created_at": "创建时间",
    },
    "furnaces": {
        "id": "主键",
        "code": "窑炉编码",
        "name": "窑炉名称",
        "kiln_no": "窑号",
        "type": "窑型",
        "workshop": "车间",
        "capacity": "产能说明",
        "status": "状态",
        "remark": "备注",
        "enabled": "是否启用",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "kiln_process_samples": {
        "id": "主键",
        "kiln_code": "窑炉编码",
        "ts": "采样时间",
        "temp_sp": "温度设定",
        "afr_sp": "空燃比设定",
        "z1_temp": "一区温度",
        "z1_gas_flow": "一区燃气流量",
        "z1_air_flow": "一区助燃风流量",
        "z1_air_valve": "一区助燃风阀位",
        "z2_temp": "二区温度",
        "z2_gas_flow": "二区燃气流量",
        "z2_air_flow": "二区助燃风流量",
        "z2_air_valve": "二区助燃风阀位",
        "z3_temp": "三区温度",
        "z3_gas_flow": "三区燃气流量",
        "z3_air_flow": "三区助燃风流量",
        "z3_air_valve": "三区助燃风阀位",
        "z4_temp": "四区温度",
        "z4_gas_flow": "四区燃气流量",
        "z4_air_flow": "四区助燃风流量",
        "z4_air_valve": "四区助燃风阀位",
        "z5_temp": "五区温度",
        "z5_gas_flow": "五区燃气流量",
        "z5_air_flow": "五区助燃风流量",
        "z5_air_valve": "五区助燃风阀位",
        "z6_temp": "六区温度",
        "z6_gas_flow": "六区燃气流量",
        "z6_air_flow": "六区助燃风流量",
        "z6_air_valve": "六区助燃风阀位",
        "furnace_p_sp": "炉压设定",
        "furnace_p_meas": "炉压实测",
        "furnace_p_out": "炉压输出",
        "gas_p_sp": "燃气压力设定",
        "gas_p_meas": "燃气压力实测",
        "gas_p_out": "燃气压力输出",
        "air_p_sp": "助燃风压力设定",
        "air_p_meas": "助燃风压力实测",
        "air_p_out": "助燃风压力输出",
        "gas_flow_instant": "燃气瞬时流量",
        "gas_flow_total": "燃气累计流量",
        "air_flow_instant": "助燃风瞬时流量",
        "air_flow_total": "助燃风累计流量",
        "o2_sp": "氧含量设定",
        "o2_meas": "氧含量实测",
        "source_file": "来源文件",
        "batch_no": "批次号",
        "zone_count": "温区数量",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "ai_reports": {
        "id": "主键",
        "public_id": "对外ID",
        "user_id": "用户ID",
        "report_type": "报告类型",
        "title": "标题",
        "kiln_code": "窑炉编码",
        "kiln_name": "窑炉名称",
        "mode": "生成模式",
        "status": "状态",
        "content": "报告正文",
        "char_count": "字符数",
        "refs": "引用JSON",
        "context_summary": "上下文摘要",
        "error_msg": "错误信息",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "prod_systems": {
        "id": "主键",
        "code": "系统编码",
        "name": "系统名称",
        "kind": "类型：tunnel/batching/shuttle_flue",
        "description": "说明",
        "enabled": "是否启用",
        "meta": "扩展元数据JSON",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "prod_tags": {
        "id": "主键",
        "system_code": "系统编码",
        "tag_code": "测点编码",
        "name": "测点名称",
        "unit": "单位",
        "group_name": "分组",
        "data_type": "数据类型",
        "writable": "是否可写",
        "alarm_lo": "报警下限",
        "alarm_hi": "报警上限",
        "sort_order": "排序",
        "enabled": "是否启用",
    },
    "prod_samples": {
        "id": "主键",
        "system_code": "系统编码",
        "tag_code": "测点编码",
        "ts": "采样时间",
        "value_num": "数值",
        "value_text": "文本值",
    },
    "prod_alarms": {
        "id": "主键",
        "public_id": "对外ID",
        "system_code": "系统编码",
        "tag_code": "测点编码",
        "level": "级别",
        "title": "标题",
        "message": "详情",
        "status": "状态：active/acked/closed",
        "raised_at": "产生时间",
        "acked_at": "确认时间",
        "closed_at": "关闭时间",
        "acked_by": "确认人用户ID",
        "meta": "扩展JSON",
    },
    "prod_commands": {
        "id": "主键",
        "public_id": "对外ID",
        "system_code": "系统编码",
        "tag_code": "测点编码",
        "action": "动作",
        "target_value": "目标数值",
        "target_text": "目标文本",
        "status": "状态",
        "executor": "执行器",
        "result_msg": "执行结果",
        "requested_by": "请求人用户ID",
        "created_at": "创建时间",
        "finished_at": "完成时间",
    },
    "gov_tasks": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "任务名称",
        "description": "任务说明",
        "owner": "负责人",
        "source_type": "数据来源类型",
        "status": "状态",
        "excel_preview": "Excel预览JSON",
        "search_text": "检索拼接文本",
        "file_key": "文件存储键",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "mcp_servers": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "服务名称",
        "transport": "传输：stdio/sse/streamable_http",
        "url": "服务URL",
        "command": "stdio命令",
        "args": "命令参数JSON",
        "env": "环境变量JSON",
        "headers": "请求头JSON",
        "timeout_seconds": "超时秒数",
        "remark": "备注",
        "enabled": "是否启用",
        "last_error": "最近错误",
        "last_checked_at": "最近探测时间",
        "tools_cached_at": "工具缓存时间",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "mcp_tools": {
        "id": "主键",
        "public_id": "对外ID",
        "server_id": "所属MCP服务ID",
        "name": "工具名",
        "description": "工具描述",
        "input_schema": "入参Schema JSON",
        "enabled": "是否启用",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "prompts": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "提示词名称",
        "content": "提示词正文",
        "remark": "备注",
        "enabled": "是否启用",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
    "scenario_agents": {
        "id": "主键",
        "public_id": "对外ID",
        "name": "智能体名称",
        "remark": "备注",
        "enabled": "是否启用",
        "prompt_public_id": "绑定提示词ID",
        "knowledge_base_ids": "知识库ID列表",
        "mode": "模式：fast/deep",
        "mcp_tool_ids": "MCP工具白名单",
        "tools_enabled": "是否启用工具调用",
        "created_by": "创建人用户ID",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    },
}


def _escape_comment(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t LIMIT 1"
        ),
        {"t": table},
    ).first()
    return row is not None


def _set_table_comment(conn, table: str, comment: str) -> None:
    if not _table_exists(conn, table):
        return
    conn.execute(sa.text(f"ALTER TABLE `{table}` COMMENT = '{_escape_comment(comment)}'"))


def _set_column_comment(conn, table: str, column: str, comment: str) -> None:
    row = conn.execute(
        sa.text(
            "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_KEY "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).mappings().first()
    if not row:
        return

    col_type = row["COLUMN_TYPE"]
    nullable = "NULL" if row["IS_NULLABLE"] == "YES" else "NOT NULL"
    extra = (row["EXTRA"] or "").strip()
    default = row["COLUMN_DEFAULT"]

    parts = [f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {col_type} {nullable}"]

    # 保留 DEFAULT（注意：CURRENT_TIMESTAMP 等表达式）
    if default is not None:
        upper = str(default).upper()
        if upper in {"CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()", "NOW()"}:
            parts.append(f"DEFAULT {default}")
        elif upper == "NULL":
            parts.append("DEFAULT NULL")
        else:
            # 数字/布尔原样；字符串加引号
            if isinstance(default, (int, float)) or (
                isinstance(default, str) and default.replace(".", "", 1).isdigit()
            ):
                parts.append(f"DEFAULT {default}")
            else:
                parts.append(f"DEFAULT '{_escape_comment(str(default))}'")

    if "auto_increment" in extra.lower():
        parts.append("AUTO_INCREMENT")

    parts.append(f"COMMENT '{_escape_comment(comment)}'")
    sql = " ".join(parts)
    conn.execute(sa.text(sql))


def upgrade() -> None:
    conn = op.get_bind()
    for table, comment in TABLE_COMMENTS.items():
        _set_table_comment(conn, table, comment)
    for table, cols in COLUMN_COMMENTS.items():
        if not _table_exists(conn, table):
            continue
        for col, comment in cols.items():
            _set_column_comment(conn, table, col, comment)


def downgrade() -> None:
    # 注释回退为空（不删列）
    conn = op.get_bind()
    for table in TABLE_COMMENTS:
        if _table_exists(conn, table):
            conn.execute(sa.text(f"ALTER TABLE `{table}` COMMENT = ''"))
    for table, cols in COLUMN_COMMENTS.items():
        if not _table_exists(conn, table):
            continue
        for col in cols:
            _set_column_comment(conn, table, col, "")
