#!/usr/bin/env python3

"""
NANSWAP XMRIG AUTOMATION WRAPPER

Supervises:

    XMRig -> RandomX -> xmrig.nanswap.com:3333
                         -> Nano payout address

Features:
    - launches XMRig
    - monitors stdout/stderr
    - extracts hashrate
    - detects accepted/rejected shares
    - detects connection/errors
    - automatically restarts XMRig
    - exponential restart backoff
    - JSON status file
    - persistent log
    - graceful shutdown
    - optional maximum restart count

IMPORTANT:
    XMRig performs the actual RandomX mining.
    This wrapper does not implement or impersonate RandomX.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

POOL = "xmrig.nanswap.com:3333"
ALGORITHM = "rx"
PASSWORD = "x"

DEFAULT_XMRIG = "./xmrig"

DATA_DIR = Path("xmrig_nanswap")
LOG_FILE = DATA_DIR / "xmrig.log"
STATUS_FILE = DATA_DIR / "status.json"

MAX_BACKOFF = 60
DEFAULT_RESTART_DELAY = 5


# ============================================================
# STATE
# ============================================================

@dataclass
class MinerState:
    running: bool = False
    pid: int | None = None

    hashrate_10s: float = 0.0
    hashrate_60s: float = 0.0
    hashrate_15m: float = 0.0

    accepted: int = 0
    rejected: int = 0

    restarts: int = 0
    errors: int = 0

    last_error: str = ""
    last_message: str = ""

    started_at: float | None = None
    stopped_at: float | None = None


state = MinerState()

shutdown_requested = threading.Event()
state_lock = threading.Lock()


# ============================================================
# REGEX
# ============================================================

HASHRATE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*H/s",
    re.IGNORECASE,
)

ACCEPTED_RE = re.compile(
    r"(accepted|share accepted)",
    re.IGNORECASE,
)

REJECTED_RE = re.compile(
    r"(rejected|share rejected)",
    re.IGNORECASE,
)

ERROR_RE = re.compile(
    r"(error|failed|invalid|connection refused|"
    r"connection closed|timeout|no connection)",
    re.IGNORECASE,
)


# ============================================================
# DIRECTORY / LOGGING
# ============================================================

def prepare_directory() -> None:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def log(message: str) -> None:

    timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    line = (
        f"[{timestamp}] "
        f"{message}"
    )

    print(
        line,
        flush=True,
    )

    with LOG_FILE.open(
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            line + "\n"
        )


# ============================================================
# STATUS
# ============================================================

def save_status() -> None:

    with state_lock:

        snapshot = asdict(state)

    STATUS_FILE.write_text(
        json.dumps(
            snapshot,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def update_status(**kwargs) -> None:

    with state_lock:

        for key, value in kwargs.items():

            if hasattr(state, key):
                setattr(
                    state,
                    key,
                    value,
                )

    save_status()


# ============================================================
# OUTPUT PARSER
# ============================================================

def process_line(line: str) -> None:

    line = line.rstrip()

    if not line:
        return

    update_status(
        last_message=line
    )

    # --------------------------------------------------------
    # HASHRATE
    # --------------------------------------------------------

    matches = HASHRATE_RE.findall(
        line
    )

    if matches:

        try:

            value = float(
                matches[0]
            )

            update_status(
                hashrate_10s=value
            )

        except ValueError:
            pass

    # --------------------------------------------------------
    # ACCEPTED
    # --------------------------------------------------------

    if ACCEPTED_RE.search(line):

        with state_lock:
            state.accepted += 1

        save_status()

        log(
            "SHARE ACCEPTED: "
            + line
        )

    # --------------------------------------------------------
    # REJECTED
    # --------------------------------------------------------

    if REJECTED_RE.search(line):

        with state_lock:
            state.rejected += 1

        save_status()

        log(
            "SHARE REJECTED: "
            + line
        )

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    if ERROR_RE.search(line):

        with state_lock:
            state.errors += 1
            state.last_error = line

        save_status()

        log(
            "XMRIG ERROR: "
            + line
        )


# ============================================================
# OUTPUT READER
# ============================================================

def read_output(
    process: subprocess.Popen,
) -> None:

    if process.stdout is None:
        return

    for raw_line in process.stdout:

        if shutdown_requested.is_set():
            break

        try:

            line = raw_line.decode(
                "utf-8",
                errors="replace",
            )

        except AttributeError:

            line = raw_line

        process_line(line)


# ============================================================
# COMMAND
# ============================================================

def build_command(
    xmrig: str,
    nano_address: str,
    threads: int | None,
) -> list[str]:

    command = [
        xmrig,

        "-o",
        POOL,

        "-a",
        ALGORITHM,

        "-k",

        "-u",
        nano_address,

        "-p",
        PASSWORD,

        "--print-time",
        "10",

        "--health-print-time",
        "30",
    ]

    if threads is not None:

        command.extend(
            [
                "--threads",
                str(threads),
            ]
        )

    return command


# ============================================================
# XMRIG LAUNCH
# ============================================================

def launch_xmrig(
    xmrig: str,
    nano_address: str,
    threads: int | None,
) -> int:

    command = build_command(
        xmrig,
        nano_address,
        threads,
    )

    log(
        "Launching XMRig:"
    )

    log(
        " ".join(command)
    )

    try:

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
        )

    except FileNotFoundError:

        log(
            f"XMRig not found: {xmrig}"
        )

        update_status(
            running=False,
            pid=None,
            errors=state.errors + 1,
            last_error=(
                f"XMRig not found: {xmrig}"
            ),
        )

        return -1

    update_status(
        running=True,
        pid=process.pid,
        started_at=time.time(),
    )

    reader = threading.Thread(
        target=read_output,
        args=(process,),
        daemon=True,
    )

    reader.start()

    return_code = process.wait()

    reader.join(
        timeout=2
    )

    update_status(
        running=False,
        pid=None,
        stopped_at=time.time(),
    )

    return return_code


# ============================================================
# SUPERVISOR
# ============================================================

def supervise(
    xmrig: str,
    nano_address: str,
    threads: int | None,
    max_restarts: int | None,
) -> None:

    backoff = DEFAULT_RESTART_DELAY

    while not shutdown_requested.is_set():

        return_code = launch_xmrig(
            xmrig,
            nano_address,
            threads,
        )

        if shutdown_requested.is_set():
            break

        with state_lock:
            state.restarts += 1
            restart_number = state.restarts

        save_status()

        log(
            f"XMRig exited with code "
            f"{return_code}"
        )

        if (
            max_restarts is not None
            and restart_number >= max_restarts
        ):

            log(
                "Maximum restart count reached."
            )

            break

        log(
            f"Restarting in {backoff} seconds..."
        )

        shutdown_requested.wait(
            backoff
        )

        backoff = min(
            backoff * 2,
            MAX_BACKOFF,
        )

    update_status(
        running=False,
        pid=None,
    )


# ============================================================
# SIGNAL HANDLING
# ============================================================

active_process = None


def request_shutdown(
    signum,
    frame,
) -> None:

    log(
        f"Shutdown signal received: {signum}"
    )

    shutdown_requested.set()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Automatic XMRig supervisor "
            "for Nanswap Nano payouts"
        )
    )

    parser.add_argument(
        "--xmrig",
        default=DEFAULT_XMRIG,
        help=(
            "path to XMRig executable"
        ),
    )

    parser.add_argument(
        "--nano",
        required=True,
        help=(
            "your Nano payout address"
        ),
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help=(
            "number of CPU mining threads"
        ),
    )

    parser.add_argument(
        "--max-restarts",
        type=int,
        default=None,
        help=(
            "maximum automatic restarts"
        ),
    )

    args = parser.parse_args()

    prepare_directory()

    signal.signal(
        signal.SIGINT,
        request_shutdown,
    )

    signal.signal(
        signal.SIGTERM,
        request_shutdown,
    )

    log(
        "======================================"
    )

    log(
        "NANSWAP XMRIG AUTOMATION STARTING"
    )

    log(
        f"Pool: {POOL}"
    )

    log(
        f"Algorithm: {ALGORITHM}"
    )

    log(
        f"Nano payout: {args.nano}"
    )

    log(
        "======================================"
    )

    supervise(
        xmrig=args.xmrig,
        nano_address=args.nano,
        threads=args.threads,
        max_restarts=args.max_restarts,
    )

    log(
        "Supervisor stopped."
    )


if __name__ == "__main__":
    main()