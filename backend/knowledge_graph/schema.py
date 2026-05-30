"""Kuzu 图谱 Schema 定义（对应 CLAUDE.md 5.1）。"""

from __future__ import annotations

# 节点表
NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS Character (
        id STRING,
        name STRING,
        persona STRING,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Location (
        id STRING,
        name STRING,
        description STRING,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Event (
        id STRING,
        name STRING,
        description STRING,
        timestamp_in_story STRING,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Concept (
        id STRING,
        name STRING,
        description STRING,
        PRIMARY KEY (id)
    )
    """,
]

# 关系表
REL_TABLES: list[str] = [
    "CREATE REL TABLE IF NOT EXISTS KNOWS (FROM Character TO Character, relation_type STRING, strength DOUBLE)",
    "CREATE REL TABLE IF NOT EXISTS LOCATED_AT (FROM Character TO Location, time_context STRING)",
    "CREATE REL TABLE IF NOT EXISTS PARTICIPATED_IN (FROM Character TO Event, role STRING)",
    "CREATE REL TABLE IF NOT EXISTS RELATED_TO (FROM Concept TO Concept, relation STRING)",
    "CREATE REL TABLE IF NOT EXISTS MENTIONED_IN (FROM Character TO Concept, context STRING)",
]

# 实体类型 -> 节点表名
ENTITY_TYPE_TO_TABLE = {
    "Character": "Character",
    "Location": "Location",
    "Event": "Event",
    "Concept": "Concept",
}

ALL_DDL = NODE_TABLES + REL_TABLES
