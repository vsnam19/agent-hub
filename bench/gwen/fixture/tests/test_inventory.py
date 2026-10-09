from minilib.inventory import Item, load_items, low_stock, total_value


def test_total_value():
    items = [Item("A", 2, 1.5), Item("B", 1, 5.0)]
    assert total_value(items) == 8.0


def test_low_stock():
    items = [Item("A", 2, 1.5), Item("B", 10, 5.0)]
    assert low_stock(items, 5) == [items[0]]


def test_load_items(tmp_path):
    p = tmp_path / "items.csv"
    p.write_text("name,qty,price\nA,2,1.5\n")
    items = load_items(str(p))
    assert len(items) == 1
    assert items[0] == Item("A", 2, 1.5)
