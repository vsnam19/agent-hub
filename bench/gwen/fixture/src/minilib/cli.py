import argparse
import sys

from minilib.inventory import load_items
from minilib.textstats import word_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    stats = subparsers.add_parser("stats")
    stats.add_argument("file")

    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("file")
    inventory.add_argument("--min-qty", type=int, default=5)

    args = parser.parse_args(argv)

    if args.cmd == "stats":
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
        print(f"words: {word_count(text)}")
        print(f"chars: {len(text)}")
    elif args.cmd == "inventory":
        items = load_items(args.file)
        for item in items:
            if item.qty < args.min_qty:
                print(f"{item.name} {item.qty}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
