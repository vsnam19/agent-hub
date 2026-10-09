from minilib.inventory import read_items


def test_read_items_works(tmp_path):
    p = tmp_path / "items.csv"
    p.write_text("name,qty,price\nX,1,1.0\n")
    assert len(read_items(str(p))) == 1


def test_no_alias():
    import minilib.inventory as inv

    assert not hasattr(inv, "load_items")
    import minilib.cli as cli

    assert not hasattr(cli, "load_items")
