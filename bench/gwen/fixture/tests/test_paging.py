import pytest

from minilib.paging import page_count, paginate


def test_paginate():
    items = [1, 2, 3, 4, 5]
    assert paginate(items, 1, 2) == [1, 2]
    assert paginate(items, 2, 2) == [3, 4]
    assert paginate(items, 3, 2) == [5]


def test_paginate_bad_page():
    with pytest.raises(ValueError):
        paginate([], 0, 10)


def test_page_count():
    assert page_count(5, 2) == 3
    assert page_count(4, 2) == 2
    assert page_count(0, 2) == 0
