import subprocess


class KeepAwake:
    """Hold `caffeinate -i` on the Mac for the length of a batch (D-050).

    `ssh -tt` gives the remote command a pty, so when this ssh process is terminated the pty closes, the remote
    shell gets SIGHUP and caffeinate exits with it: nothing is left running on the Mac. Idle sleep only — a closed
    lid or a Mac on battery can still sleep, which the preflight (D-049) then reports.
    """

    def __init__(self, ssh_target: str | None):
        self.ssh_target = ssh_target
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "KeepAwake":
        if self.ssh_target:
            self.proc = subprocess.Popen(
                ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", self.ssh_target, "caffeinate -i"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
