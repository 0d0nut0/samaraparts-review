"""
Дерево каталога: 6 разделов (корневые категории с иконкой) → группы → подгруппы.
Количество товаров считается по видимым товарам витрины и кэшируется в памяти процесса.
"""
import time
from dataclasses import dataclass, field

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, MobaPrice, Product

CACHE_TTL = 300  # секунд


@dataclass
class Node:
    id: int
    name: str
    slug: str
    icon: str | None
    position: int
    parent_id: int | None
    own_count: int = 0
    count: int = 0  # вместе с потомками
    children: list["Node"] = field(default_factory=list)


@dataclass
class Tree:
    sections: list[Node]
    by_slug: dict[str, Node]
    by_id: dict[int, Node]

    def descendant_ids(self, node: Node) -> list[int]:
        ids, stack = [], [node]
        while stack:
            n = stack.pop()
            ids.append(n.id)
            stack.extend(n.children)
        return ids

    def path(self, node: Node) -> list[Node]:
        out = []
        while node:
            out.append(node)
            node = self.by_id.get(node.parent_id) if node.parent_id else None
        return list(reversed(out))


_cache: tuple[float, Tree] | None = None


def invalidate() -> None:
    global _cache
    _cache = None


async def get_tree(db: AsyncSession) -> Tree:
    global _cache
    if _cache and time.monotonic() - _cache[0] < CACHE_TTL:
        return _cache[1]

    cats = (await db.execute(select(Category))).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(Product.category_id, func.count(Product.id))
                .join(MobaPrice, MobaPrice.sku == Product.sku)
                .where(Product.is_active == 1)
                .group_by(Product.category_id)
            )
        ).all()
    )

    by_id = {c.id: Node(c.id, c.name, c.slug, c.icon, c.position, c.parent_id, counts.get(c.id, 0)) for c in cats}
    for n in by_id.values():
        if n.parent_id in by_id:
            by_id[n.parent_id].children.append(n)

    def total(n: Node) -> int:
        n.count = n.own_count + sum(total(c) for c in n.children)
        n.children = sorted((c for c in n.children if c.count), key=lambda c: (c.position, c.name))
        return n.count

    roots = [n for n in by_id.values() if n.parent_id is None]
    for r in roots:
        total(r)
    # Разделы — корневые категории с иконкой; пустые технические корни moba не показываем
    sections = sorted((r for r in roots if r.icon), key=lambda r: r.position)
    tree = Tree(sections, {n.slug: n for n in by_id.values()}, by_id)
    _cache = (time.monotonic(), tree)
    return tree
