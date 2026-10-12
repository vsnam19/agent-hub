import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path

from bench.gwen.awake import KeepAwake
from bench.gwen.memsample import MemSampler
from bench.gwen.proxy import RecordingProxy
from bench.gwen.tasks import SUITES_FILE, Suite, Task, install_hidden, load_suites, load_tasks, prepare

SECRET_WORDS = ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
GATE_TIMEOUT = 300


def _expand_gate(argv: list[str], task: Task, build_dir: Path) -> list[str]:
    """Gate argv with {python}, {build_dir} and the whole-argument {visible_tests} filled in."""
    out = []
    for arg in argv:
        if arg == "{visible_tests}":
            out.extend(task.visible_tests)
        else:
            out.append(arg.replace("{python}", sys.executable).replace("{build_dir}", str(build_dir)))
    return out


def _gate(cmd: list[str], repo: Path) -> tuple[int, str]:
    """Run one gate command; returns (rc, last 20 output lines). A timeout counts as rc 124."""
    try:
        res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=GATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {GATE_TIMEOUT}s"
    return res.returncode, "\n".join((res.stdout + res.stderr).splitlines()[-20:])


def _kill_group(p: subprocess.Popen) -> None:
    """SIGTERM the harness's process group, then SIGKILL whatever is left after 5 s."""
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=grace or None)
            return
        except subprocess.TimeoutExpired:
            continue


def _raise_on_sigterm(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def preflight(base_url: str, api_key: str, model: str, timeout: float = 10.0) -> str | None:
    """None when the server answers and serves `model`, else a one-line reason."""
    req = urllib.request.Request(f"{base_url}/v1/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ids = [m.get("id") for m in json.load(resp).get("data", [])]
    except (OSError, ValueError) as e:
        return f"gwen server not reachable at {base_url}: {e}"
    if model not in ids:
        return f"gwen server does not serve {model} (serves: {', '.join(i for i in ids if i)})"
    return None


def get_harnesses_file():
    return Path(os.environ.get("GWEN_BENCH_HARNESSES") or Path(__file__).parent / "harnesses.toml")


def get_suites_file():
    return Path(os.environ.get("GWEN_BENCH_SUITES") or SUITES_FILE)


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
    suites: dict[str, Suite] | None = None,
) -> dict:

    suites = load_suites(get_suites_file()) if suites is None else suites
    if task.suite not in suites:
        raise ValueError(f"task {task.id} belongs to suite {task.suite!r}, which {get_suites_file()} does not define")

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
    suite = suites[task.suite]

    setup_commit = prepare(task, repo, suite)

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
            .replace("{lint_cmd}", suite.harness.get("lint_cmd", ""))
            .replace("{test_cmd}", suite.harness.get("test_cmd", ""))
        )

    for k, v in cfg.get("env", {}).items():
        env[k] = substitute(v)

    # The agent runs the same python/pytest/ruff the gate uses (the bench's own environment).
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    # Some harnesses (opencode) take their working directory from $PWD, not the real cwd: never inherit ours.
    env["PWD"] = str(repo)
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
        stdin_src = subprocess.PIPE if cfg.get("stdin") else subprocess.DEVNULL
        p = subprocess.Popen(argv, cwd=repo, env=env, stdout=out, stderr=err, stdin=stdin_src, start_new_session=True)
        try:
            if cfg.get("stdin"):
                p.stdin.write(task.brief.encode("utf-8"))
                p.stdin.close()
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p)
        except BaseException:
            # SIGTERM/Ctrl-C of the runner: never leave the harness running on its own.
            _kill_group(p)
            proxy.stop()
            sampler.stop()
            raise

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

    # Fresh build directory for the gate, outside the repository (the agent's own build/ never counts).
    build_dir = run_dir / "gate-build"
    build_dir.mkdir()
    gate = {name: _gate(_expand_gate(argv, task, build_dir), repo) for name, argv in suite.gate.items()}
    (run_dir / "gate.log").write_text("\n\n".join(f"== {k} rc={rc}\n{tail}" for k, (rc, tail) in gate.items()))

    usage = proxy.summary()
    # The model server vanished during the run (Mac asleep, network down): the run says nothing about the model.
    infra_error = usage["upstream_failures"] > 0
    passed = all(rc == 0 for rc, _ in gate.values()) and not scope_violation and not infra_error

    res = {
        "ts": ts,
        "harness": harness,
        "model": model,
        "task": task.id,
        "suite": task.suite,
        "pass": passed,
        "infra_error": infra_error,
        "rc": p.returncode if not timed_out else None,
        "timed_out": timed_out,
        "wall_s": wall_s,
        "gate": {k: rc for k, (rc, _) in gate.items()},
        "scope_violation": scope_violation,
        "changed": changed,
        "usage": usage,
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
    parser.add_argument("--suite", action="append")
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

    suites = load_suites(get_suites_file())
    for s in args.suite or []:
        if s not in suites:
            print(f"Unknown suite: {s}", file=sys.stderr)
            return 2

    tasks_dict = {tid: t for tid, t in load_tasks(suites).items() if not args.suite or t.suite in args.suite}
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

    with KeepAwake(ssh_target):
        return _run_batch(
            args, selected_harnesses, selected_tasks, tasks_dict, api_key, out_root, results_file, ssh_target, suites
        )


def _run_batch(
    args, selected_harnesses, selected_tasks, tasks_dict, api_key, out_root, results_file, ssh_target, suites
) -> int:
    for _ in range(args.repeat):
        for h in selected_harnesses:
            for m in args.model:
                for t in selected_tasks:
                    reason = preflight(args.base_url, api_key, m)
                    if reason:
                        print(f"stopping: {reason}", file=sys.stderr)
                        return 3
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
                        suites=suites,
                    )

                    with open(results_file, "a") as f:
                        f.write(json.dumps(res) + "\n")

                    status = "INFRA" if res["infra_error"] else "PASS" if res["pass"] else "FAIL"
                    print(f"{t} {h} {m} {status} {int(res['wall_s'])}s")
                    if res["infra_error"]:
                        print("stopping: the model server dropped out during the run", file=sys.stderr)
                        return 3

    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    sys.exit(main())
