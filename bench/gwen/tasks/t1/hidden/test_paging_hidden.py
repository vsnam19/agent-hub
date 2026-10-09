from minilib.paging import page_count, paginate


def test_paginate_hidden():
    items = list(range(10))
    assert paginate(items, 1, 3) == [0, 1, 2]
    assert paginate(items, 2, 3) == [3, 4, 5]
    assert paginate(items, 4, 3) == [9]
    assert paginate(items, 5, 3) == []


def test_page_count_hidden():
    assert page_count(0, 3) == 0
    assert page_count(9, 3) == 3
