# Brief

## Goal
make this command work

## Input
```
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/workspace/src/minilib/cli.py", line 33, in <module>
    sys.exit(main())
  File "/workspace/src/minilib/cli.py", line 26, in main
    items = load_items(args.file)
            ^^^^^^^^^^^^^^^^^^^^^
  File "/workspace/src/minilib/inventory.py", line 17, in load_items
    items.append(Item(name=parts[0], qty=int(parts[1]), price=float(parts[2])))
                                             ^^^^^^^^
ValueError: invalid literal for int() with base 10: ' large"'
```

## Expected output
- The command succeeds

## Constraints
- Do not modify the csv file
