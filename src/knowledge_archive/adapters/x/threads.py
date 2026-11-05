"""Resolve observed self-reply chains without inferring unseen content.

Conversation IDs alone do not identify a thread: readers' replies share them.
Only explicit reply edges between stable author IDs join the main sequence.
"""

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class Node:
    id: str
    author_id: str | None
    parent_id: str | None
    parent_author_id: str | None

    @classmethod
    def from_record(cls, record: dict) -> "Node":
        values = (
            record["id"],
            record["author"]["id"],
            record["reply_to_id"],
            record["reply_to_author_id"],
        )
        if values[0] is None or any(
            value is not None
            and (
                not isinstance(value, str) or not value.isascii() or not value.isdigit()
            )
            for value in values
        ):
            raise ValueError("X IDs must be decimal strings, not numbers or handles")
        return cls(*values)


@dataclass(frozen=True)
class Thread:
    selected_id: str
    root_id: str
    node_ids: tuple[str, ...]
    context_ids: tuple[str, ...]
    missing_parent_ids: tuple[str, ...]
    unresolved_author_ids: tuple[str, ...]
    coverage: str = "observed_only"

    def to_dict(self) -> dict:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()
        }


class ThreadGraph:
    def __init__(self, records: Iterable[dict]):
        self.nodes: dict[str, Node] = {}
        self.children: dict[str, list[Node]] = {}
        for record in records:
            node = Node.from_record(record)
            previous = self.nodes.get(node.id)
            if previous and previous != node:
                if (
                    previous.parent_id != node.parent_id
                    or (
                        previous.author_id
                        and node.author_id
                        and previous.author_id != node.author_id
                    )
                    or (
                        previous.parent_author_id
                        and node.parent_author_id
                        and previous.parent_author_id != node.parent_author_id
                    )
                ):
                    raise ValueError(
                        f"Conflicting relationship observations for {node.id}"
                    )
                node = Node(
                    node.id,
                    node.author_id or previous.author_id,
                    node.parent_id,
                    node.parent_author_id or previous.parent_author_id,
                )
            self.nodes[node.id] = node
        for node in self.nodes.values():
            if node.parent_id:
                parent = self.nodes.get(node.parent_id)
                if (
                    parent
                    and parent.author_id
                    and node.parent_author_id
                    and parent.author_id != node.parent_author_id
                ):
                    raise ValueError(
                        f"Reply author disagrees with parent record for {node.id}"
                    )
                self.children.setdefault(node.parent_id, []).append(node)
        for children in self.children.values():
            children.sort(key=lambda node: int(node.id))

    def resolve(self, selected_id: str) -> Thread:
        selected = self.nodes[selected_id]
        if not selected.author_id:
            return Thread(
                selected_id, selected_id, (selected_id,), (), (), (selected_id,)
            )
        root = selected
        context, missing, unresolved = [], [], []
        ancestors = {root.id}
        while root.parent_id:
            parent = self.nodes.get(root.parent_id)
            if parent is None:
                missing.append(root.parent_id)
                break
            if parent.id in ancestors:
                raise ValueError(f"Reply cycle involving {parent.id}")
            ancestors.add(parent.id)
            if parent.author_id != selected.author_id:
                context.append(parent.id)
                if parent.author_id is None:
                    unresolved.append(parent.id)
                break
            root = parent

        ordered = []
        stack = [root]
        visited = set()
        while stack:
            node = stack.pop()
            if node.id in visited:
                raise ValueError(f"Reply cycle involving {node.id}")
            visited.add(node.id)
            ordered.append(node.id)
            own_children = []
            for child in self.children.get(node.id, []):
                if child.author_id == selected.author_id:
                    own_children.append(child)
                elif child.author_id is None:
                    unresolved.append(child.id)
            stack.extend(reversed(own_children))
        return Thread(
            selected_id,
            root.id,
            tuple(ordered),
            tuple(context),
            tuple(missing),
            tuple(unresolved),
        )
