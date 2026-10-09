import re
import subprocess
import threading
import time


def parse_sample(text: str) -> tuple[int | None, float | None]:
    free_pct = None
    swap_mb = None

    m_pct = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if m_pct:
        free_pct = int(m_pct.group(1))

    m_swap = re.search(r"vm\.swapusage:.*?used\s*=\s*([0-9.]+)M", text)
    if m_swap:
        swap_mb = float(m_swap.group(1))

    if free_pct is None and swap_mb is None:
        return None, None
    return free_pct, swap_mb


class MemSampler:
    def __init__(self, ssh_target: str | None, interval: float = 10.0):
        self.ssh_target = ssh_target
        self.interval = interval
        self.running = False
        self.thread = None
        self.min_free_pct = None
        self.max_swap_mb = None
        self.samples = 0
        self.lock = threading.Lock()

    def start(self) -> None:
        if not self.ssh_target:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            try:
                out = subprocess.run(
                    ["ssh", self.ssh_target, "memory_pressure -Q; sysctl vm.swapusage"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
                free_pct, swap_mb = parse_sample(out)
                with self.lock:
                    if free_pct is not None:
                        if self.min_free_pct is None or free_pct < self.min_free_pct:
                            self.min_free_pct = free_pct
                    if swap_mb is not None:
                        if self.max_swap_mb is None or swap_mb > self.max_swap_mb:
                            self.max_swap_mb = swap_mb
                    self.samples += 1
            except Exception:
                pass
            time.sleep(self.interval)

    def stop(self) -> dict | None:
        if not self.running:
            return None
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

        with self.lock:
            if self.samples == 0:
                return None
            return {"min_free_pct": self.min_free_pct, "max_swap_mb": self.max_swap_mb, "samples": self.samples}
