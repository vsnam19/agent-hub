from minilib.inventory import load_items


def test_hidden_load_items_with_comma(tmp_path):
    p = tmp_path / "items.csv"
    p.write_text('name,qty,price\n"A, B",2,1.5\nC,1,2.0\n')
    items = load_items(str(p))
    assert len(items) == 2
    assert items[0].name == "A, B"
    assert items[0].qty == 2
    assert items[1].name == "C"
