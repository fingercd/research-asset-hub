"""Give the first interactive request a brief window before background work."""

import runpy
import time


def run() -> None:
    # The durable queue can wait briefly. Keep a second ORM/model import out of
    # the homepage's first request; this process is already supervised by Rust.
    time.sleep(3)
    runpy.run_module("apps.worker.main", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    run()
