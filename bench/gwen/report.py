import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def render(records: list[dict]) -> str:
    tasks = ["t1", "t2", "t3", "t4", "t5", "t6"]

    # model -> harness -> task -> list of results
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for r in records:
        data[r["model"]][r["harness"]][r["task"]].append(r)

    out = []

    for model in sorted(data.keys()):
        out.append(f"## Model: {model}")
        out.append("")

        header = ["Harness"] + tasks + ["Pass Rate", "Wall (s)", "Tokens (P/C)", "Swap (MB)", "Free (%)"]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * len(header)) + "|")

        for harness in sorted(data[model].keys()):
            row = [harness]

            passes = 0
            total = 0
            wall_times = []
            prompt_tokens = []
            completion_tokens = []
            swaps = []
            frees = []

            for t in tasks:
                runs = data[model][harness][t]
                if not runs:
                    row.append("–")
                else:
                    passed = sum(1 for r in runs if r["pass"])
                    passes += passed
                    total += len(runs)
                    row.append(f"{passed}/{len(runs)}")

                    for r in runs:
                        wall_times.append(r.get("wall_s", 0))
                        usage = r.get("usage")
                        if usage:
                            prompt_tokens.append(usage.get("prompt_tokens", 0))
                            completion_tokens.append(usage.get("completion_tokens", 0))
                        mem = r.get("mem")
                        if mem:
                            swaps.append(mem.get("max_swap_mb", 0))
                            frees.append(mem.get("min_free_pct", 0))

            rate = f"{passes / total:.0%}" if total else "–"
            row.append(rate)

            if wall_times:
                wall_times.sort()
                mid = len(wall_times) // 2
                median = wall_times[mid] if len(wall_times) % 2 else (wall_times[mid - 1] + wall_times[mid]) / 2
                row.append(f"{median:.0f}")
            else:
                row.append("–")

            if prompt_tokens and completion_tokens:
                mean_p = sum(prompt_tokens) / len(prompt_tokens)
                mean_c = sum(completion_tokens) / len(completion_tokens)
                row.append(f"{mean_p:.0f}/{mean_c:.0f}")
            else:
                row.append("–")

            if swaps:
                row.append(f"{max(swaps):.0f}")
            else:
                row.append("–")

            if frees:
                row.append(f"{min(frees):.0f}")
            else:
                row.append("–")

            out.append("| " + " | ".join(row) + " |")

        out.append("")

    return "\\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", help="JSONL results files")
    parser.add_argument("-o", "--out", help="Output markdown file")
    args = parser.parse_args(argv)

    records = []
    for path in args.results:
        with open(path) as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))

    md = render(records)

    if args.out:
        Path(args.out).write_text(md)
    else:
        print(md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
