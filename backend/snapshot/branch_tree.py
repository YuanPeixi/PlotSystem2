"""分支树数据结构构建。"""

from __future__ import annotations

from backend.models import Branch, BranchTree, BranchTreeNode


def build_branch_tree(project_id: str, branches: list[Branch]) -> BranchTree:
    """从扁平分支列表构建树形结构。"""
    nodes: dict[str, BranchTreeNode] = {
        b.branch_id: BranchTreeNode(branch=b) for b in branches
    }
    roots: list[BranchTreeNode] = []
    for b in branches:
        node = nodes[b.branch_id]
        if b.parent_branch_id and b.parent_branch_id in nodes:
            nodes[b.parent_branch_id].children.append(node)
        else:
            roots.append(node)
    return BranchTree(project_id=project_id, roots=roots)
