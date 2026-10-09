import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from bench.gwen.memsample import MemSampler
from bench.gwen.proxy import RecordingProxy
from bench.gwen.tasks import Task, install_hidden, load_tasks, prepare


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
    run_dir = out_root / f"{ts}-{harness}-{model}-{task.id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    home = run_dir / "home"
    home.mkdir()
    repo = run_dir / "repo"

    with open(harnesses_file, "rb") as f:
        harnesses = tomllib.load(f)

    if harness not in harnesses:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(2)

    cfg = harnesses[harness]

    prepare(task, repo)

    proxy_log = run_dir / "proxy.jsonl"
    proxy = RecordingProxy(base_url, proxy_log)
    proxy_url = proxy.start()

    sampler = MemSampler(ssh_target)
    sampler.start()

    brief_path = run_dir / "brief.md"
    brief_path.write_text(task.brief)

    env = os.environ.copy()
    keys_to_remove = []
    for k in env:
        upper = k.upper()
        if "TOKEN" in upper or "SECRET" in upper or "API_KEY" in upper or "PASSWORD" in upper:
            keys_to_remove.append(k)
    for k in keys_to_remove:
        del env[k]

    def substitute(s: str) -> str:
        base_u = (
            proxy_url.replace("http://", "").replace("/v1", "").split("/")[0]
            if "/v1" not in proxy_url
            else proxy_url.replace("/v1", "")
        )  # noqa: E501
        return (
            s.replace("{model}", model)
            .replace("{base_url}", base_u)
            .replace("{api_key}", api_key)
            .replace("{brief_path}", str(brief_path))
            .replace("{brief_text}", task.brief)
            .replace("{home}", str(home))
        )  # noqa: E501

    for k, v in cfg.get("env", {}).items():
        env[k] = substitute(v)

    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")

    # Simple substitution for base_url without /v1 which is requested by some harnesses
    base_url_no_v1 = proxy_url
    if proxy_url.endswith("/v1"):
        base_url_no_v1 = proxy_url[:-3]

    for k, v in cfg.get("env", {}).items():
        val = v.replace("{model}", model)
        val = val.replace("{base_url}", base_url_no_v1)
        val = val.replace("{api_key}", api_key)
        val = val.replace("{brief_path}", str(brief_path))
        val = val.replace("{brief_text}", task.brief)
        val = val.replace("{home}", str(home))
        env[k] = val

    for fname, content in cfg.get("files", {}).items():
        fpath = home / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        val = content.replace("{model}", model)
        val = val.replace("{base_url}", base_url_no_v1)
        val = val.replace("{api_key}", api_key)
        val = val.replace("{brief_path}", str(brief_path))
        val = val.replace("{brief_text}", task.brief)
        val = val.replace("{home}", str(home))
        fpath.write_text(val)

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

    # Changed files
    setup_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    status_out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, capture_output=True, text=True
    ).stdout
    changed = [line[3:] for line in status_out.splitlines()]
    diff_out = subprocess.run(
        ["git", "diff", "--name-only", setup_commit], cwd=repo, capture_output=True, text=True
    ).stdout
    for line in diff_out.splitlines():
        if line and line not in changed:
            changed.append(line)

    scope_violation = []
    for c in changed:
        if not any(fnmatch.fnmatch(c, g) for g in task.paths_allowed):
            scope_violation.append(c)

    install_hidden(task, repo)

    ruff = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "."], cwd=repo, capture_output=True)
    vis = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *task.visible_tests],
        cwd=repo,
        capture_output=True,
    )
    hid = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/hidden"], cwd=repo, capture_output=True
    )

    passed = ruff.returncode == 0 and vis.returncode == 0 and hid.returncode == 0 and not scope_violation

    res = {
        "ts": ts,
        "harness": harness,
        "model": model,
        "task": task.id,
        "pass": passed,
        "rc": p.returncode if not timed_out else None,
        "timed_out": timed_out,
        "wall_s": wall_s,
        "gate": {"ruff": ruff.returncode, "visible": vis.returncode, "hidden": hid.returncode},
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
    parser.add_argument("--results", default=f"bench/gwen/results/{time.strftime('%Y-%m-%d')}.jsonl")
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
        with open(args.api_key_file) as f:
            api_key = f.read().strip()
    except Exception:
        api_key = "dummy" if not os.path.exists(args.api_key_file) else ""

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
