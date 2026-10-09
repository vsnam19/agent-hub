import csv
from dataclasses import dataclass


@dataclass
class Item:
    name: str
    qty: int
    price: float


def load_items(path: str) -> list[Item]:
    items = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append(Item(name=row["name"], qty=int(row["qty"]), price=float(row["price"])))
    return items


def total_value(items: list[Item]) -> float:
    return sum(item.qty * item.price for item in items)


def low_stock(items: list[Item], threshold: int) -> list[Item]:
    return [item for item in items if item.qty < threshold]
