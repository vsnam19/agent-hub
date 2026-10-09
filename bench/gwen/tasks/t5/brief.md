# Brief

## Goal

Running `PYTHONPATH=src python -m minilib.cli inventory data/items.csv` from the repository root fails with the
traceback below. Make this command work.

## Input

```
Traceback (most recent call last):
  File "<frozen runpy>", line 203, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/repo/src/minilib/cli.py", line 35, in <module>
    sys.exit(main())
             ~~~~^^
  File "/repo/src/minilib/cli.py", line 27, in main
    items = load_items(args.file)
  File "/repo/src/minilib/inventory.py", line 20, in load_items
    items.append(Item(name=parts[0], qty=int(parts[1]), price=float(parts[2])))
                                         ~~~^^^^^^^^^^
ValueError: invalid literal for int() with base 10: ' large"'
```

## Expected output

- The command above exits 0 and prints the low-stock items.
- The existing tests still pass: `python -m pytest -q`.

## Constraints

- Do not modify `data/items.csv`.

## Gate

Before finishing, run from the repository root and make both pass:

- `ruff check .`
- `python -m pytest -q`
