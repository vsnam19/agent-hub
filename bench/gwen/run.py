import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

from bench.gwen.memsample import MemSampler
from bench.gwen.proxy import RecordingProxy
from bench.gwen.tasks import Task, install_hidden, load_tasks, prepare


SECRET_WORDS = ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
GATE_TIMEOUT = 300


def _gate(cmd: list[str], repo: Path) -> tuple[int, str]:
    """Run one gate command; returns (rc, last 20 output lines). A timeout counts as rc 124."""
    try:
        res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=GATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {GATE_TIMEOUT}s"
    return res.returncode, "\n".join((res.stdout + res.stderr).splitlines()[-20:])


def get_harnesses_file():
    return Path(__file__).parent / "harnesses.toml"


def run_one(
    harness: str,
    model: str,
    task: Task,
    *,
    base_url: str,
    api_key: str,
    out_root: Path,
    timeout: float,
    ssh_target: str | None,
    harnesses_file: Path,
) -> dict:

    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_model = model.replace("/", "_")
    run_dir = out_root / f"{ts}-{uuid.uuid4().hex[:6]}-{harness}-{safe_model}-{task.id}"
    run_dir.mkdir(parents=True)
    home = run_dir / "home"
    home.mkdir()
    repo = run_dir / "repo"

    with open(harnesses_file, "rb") as f:
        harnesses = tomllib.load(f)

    if harness not in harnesses:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(2)

    cfg = harnesses[harness]

    setup_commit = prepare(task, repo)

    proxy_log = run_dir / "proxy.jsonl"
    proxy = RecordingProxy(base_url, proxy_log)
    proxy_url = proxy.start()

    sampler = MemSampler(ssh_target)
    sampler.start()

    brief_path = run_dir / "brief.md"
    brief_path.write_text(task.brief)

    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in SECRET_WORDS)}

    def substitute(s: str) -> str:
        return (
            s.replace("{model}", model)
            .replace("{base_url}", proxy_url)
            .replace("{api_key}", api_key)
            .replace("{brief_path}", str(brief_path))
            .replace("{brief_text}", task.brief)
            .replace("{workdir}", str(repo))
            .replace("{home}", str(home))
        )

    for k, v in cfg.get("env", {}).items():
        env[k] = substitute(v)

    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")

    for fname, content in cfg.get("files", {}).items():
        fpath = home / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(substitute(content))

    argv = [substitute(a) for a in cfg["argv"]]
    argv_redacted = [a.replace(api_key, "***") if api_key else a for a in argv]

    start_t = time.time()
    timed_out = False

    with open(run_dir / "stdout", "wb") as out, open(run_dir / "stderr", "wb") as err:
        try:
            stdin_src = subprocess.PIPE if cfg.get("stdin") else subprocess.DEVNULL
            p = subprocess.Popen(
                argv, cwd=repo, env=env, stdout=out, stderr=err, stdin=stdin_src, start_new_session=True
            )
            if cfg.get("stdin"):
                p.stdin.write(task.brief.encode("utf-8"))
                p.stdin.close()
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(os.getpgid(p.pid), 15)
            time.sleep(5)
            try:
                os.killpg(os.getpgid(p.pid), 9)
            except ProcessLookupError:
                pass

    wall_s = time.time() - start_t

    proxy.stop()
    mem_res = sampler.stop()

    # Changed files: working tree and index, plus anything the agent committed after the setup commit
    status_out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--no-renames"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    changed = [line[3:] for line in status_out.splitlines()]
    diff_out = subprocess.run(
        ["git", "diff", "--name-only", setup_commit], cwd=repo, capture_output=True, text=True
    ).stdout
    for line in diff_out.splitlines():
        if line and line not in changed:
            changed.append(line)

    ignore = cfg.get("ignore", [])
    changed = [c for c in changed if not any(fnmatch.fnmatch(c, g) for g in ignore)]
    scope_violation = []
    for c in changed:
        if not any(fnmatch.fnmatch(c, g) for g in task.paths_allowed):
            scope_violation.append(c)

    install_hidden(task, repo)

    py = [sys.executable, "-m"]
    gate = {
        "ruff": _gate(py + ["ruff", "check", "--no-cache", "."], repo),
        "visible": _gate(py + ["pytest", "-q", "-p", "no:cacheprovider", *task.visible_tests], repo),
        "hidden": _gate(py + ["pytest", "-q", "-p", "no:cacheprovider", "tests/hidden"], repo),
    }
    (run_dir / "gate.log").write_text("\n\n".join(f"== {k} rc={rc}\n{tail}" for k, (rc, tail) in gate.items()))

    passed = all(rc == 0 for rc, _ in gate.values()) and not scope_violation

    res = {
        "ts": ts,
        "harness": harness,
        "model": model,
        "task": task.id,
        "pass": passed,
        "rc": p.returncode if not timed_out else None,
        "timed_out": timed_out,
        "wall_s": wall_s,
        "gate": {k: rc for k, (rc, _) in gate.items()},
        "scope_violation": scope_violation,
        "changed": changed,
        "usage": proxy.summary(),
        "mem": mem_res,
        "run_dir": str(run_dir),
        "argv": argv_redacted,
    }

    with open(run_dir / "result.json", "w") as f:
        json.dump(res, f)

    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", action="append")
    parser.add_argument("--model", action="append")
    parser.add_argument("--task", action="append")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--base-url", default=os.environ.get("GWEN_BASE_URL", "http://100.86.30.90:8642"))
    parser.add_argument("--api-key-file", default=os.path.expanduser("~/.config/gwen/api-key"))
    parser.add_argument("--ssh", default="sammy@sammys-macbook-air")
    parser.add_argument("--no-mem", action="store_true")
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument(
        "--results", default=str(Path(__file__).parent / "results" / f"{time.strftime('%Y-%m-%d')}.jsonl")
    )
    def_out = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")) + "/agent-hub/bench"
    parser.add_argument("--out-root", default=def_out)

    args = parser.parse_args(argv)

    with open(get_harnesses_file(), "rb") as f:
        harnesses = tomllib.load(f)

    selected_harnesses = args.harness if args.harness and "all" not in args.harness else list(harnesses.keys())
    for h in selected_harnesses:
        if h not in harnesses:
            print(f"Unknown harness: {h}", file=sys.stderr)
            return 2

    tasks_dict = load_tasks()
    selected_tasks = args.task if args.task and "all" not in args.task else list(tasks_dict.keys())
    for t in selected_tasks:
        if t not in tasks_dict:
            print(f"Unknown task: {t}", file=sys.stderr)
            return 2

    if not args.model:
        print("At least one --model required", file=sys.stderr)
        return 2

    try:
        api_key = Path(args.api_key_file).read_text().strip()
    except OSError as e:
        print(f"cannot read the API key file: {e}", file=sys.stderr)
        return 2

    out_root = Path(args.out_root)
    results_file = Path(args.results)
    results_file.parent.mkdir(parents=True, exist_ok=True)

    ssh_target = None if args.no_mem else args.ssh

    for _ in range(args.repeat):
        for h in selected_harnesses:
            for m in args.model:
                for t in selected_tasks:
                    res = run_one(
                        h,
                        m,
                        tasks_dict[t],
                        base_url=args.base_url,
                        api_key=api_key,
                        out_root=out_root,
                        timeout=args.timeout,
                        ssh_target=ssh_target,
                        harnesses_file=get_harnesses_file(),
                    )

                    with open(results_file, "a") as f:
                        f.write(json.dumps(res) + "\n")

                    status = "PASS" if res["pass"] else "FAIL"
                    print(f"{t} {h} {m} {status} {int(res['wall_s'])}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
