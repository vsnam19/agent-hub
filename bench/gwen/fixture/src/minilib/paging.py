def paginate(items: list, page: int, size: int) -> list:
    if page < 1:
        raise ValueError("Page must be >= 1")
    start = (page - 1) * size
    return items[start : start + size]


def page_count(n: int, size: int) -> int:
    if n == 0:
        return 0
    return (n + size - 1) // size
