"""常用 Cypher 查询集合。"""

from __future__ import annotations

# 列出所有角色
LIST_CHARACTERS = "MATCH (c:Character) RETURN c.id, c.name, c.persona"

# 查询某角色认识的所有角色
CHARACTER_KNOWS = (
    "MATCH (c:Character {id: $id})-[r:KNOWS]->(o:Character) "
    "RETURN o.id, o.name, r.relation_type, r.strength"
)

# 查询某角色参与的事件
CHARACTER_EVENTS = (
    "MATCH (c:Character {id: $id})-[r:PARTICIPATED_IN]->(e:Event) "
    "RETURN e.id, e.name, e.description, r.role"
)

# 查询某角色出现过的地点
CHARACTER_LOCATIONS = (
    "MATCH (c:Character {id: $id})-[r:LOCATED_AT]->(l:Location) "
    "RETURN l.id, l.name, l.description"
)
