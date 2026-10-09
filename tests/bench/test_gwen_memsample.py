import os

from bench.gwen.memsample import MemSampler, parse_sample


def test_parse_sample():
    text = """System-wide memory free percentage: 41%
vm.swapusage: total = 2048.00M  used = 1093.25M  free = 954.75M  (encrypted)"""
    free_pct, swap_mb = parse_sample(text)
    assert free_pct == 41
    assert swap_mb == 1093.25


def test_parse_garbage_returns_none():
    assert parse_sample("garbage") == (None, None)
    assert parse_sample("System-wide memory free percentage: %") == (None, None)


def test_sampler_with_fake_ssh(tmp_path):
    fake_ssh_dir = tmp_path / "bin"
    fake_ssh_dir.mkdir()
    fake_ssh = fake_ssh_dir / "ssh"
    fake_ssh.write_text("""#!/usr/bin/env python3
print("System-wide memory free percentage: 42%")
print("vm.swapusage: total = 2048.00M  used = 10.50M  free = 2037.50M  (encrypted)")
""")
    fake_ssh.chmod(0o755)

    os.environ["PATH"] = f"{fake_ssh_dir}:{os.environ.get('PATH', '')}"

    sampler = MemSampler("fake@host", interval=0.05)
    sampler.start()
    import time

    time.sleep(0.15)
    res = sampler.stop()

    assert res is not None
    assert res["samples"] >= 2
    assert res["min_free_pct"] == 42
    assert res["max_swap_mb"] == 10.5


def test_disabled_sampler_returns_none():
    sampler = MemSampler(None)
    sampler.start()
    assert sampler.stop() is None
