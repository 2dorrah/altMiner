#!/usr/bin/env python3
"""
CYTHAN AUTO XNO MINER
=====================

Single-process orchestration around XMRig + Nanswap.

Workflow:
    CPU detection
       |
       v
    XMRig / RandomX
       |
       v
    xmrig.nanswap.com:3333
       |
       v
    XNO payout handled by Nanswap
       |
       +--> XMRig telemetry
       |
       +--> CYTHAN/MIRACLE deterministic telemetry state
       |
       +--> local JSONL settlement/operation log

IMPORTANT:
- This program does not hold private keys.
- It does not directly execute BTC->XNO swaps.
- It does not pretend the CYTHAN/MIRACLE hash is RandomX.
- XMRig performs the actual RandomX pool mining.
- Nanswap determines the pool payout/conversion process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

POOL = "xmrig.nanswap.com:3333"
ALGORITHM = "rx"

API_HOST = "127.0.0.1"
API_PORT = 16000

DEFAULT_INTERVAL = 15

DATA_DIR = Path("data/cythan_xno")
TELEMETRY_FILE = DATA_DIR / "telemetry.jsonl"
MIRACLE_FILE = DATA_DIR / "miracle_states.jsonl"
EVENT_FILE = DATA_DIR / "events.jsonl"


# ============================================================
# GLOBAL STATE
# ============================================================

RUNNING = True
MINER_PROCESS: subprocess.Popen | None = None


# ============================================================
# UTILITIES
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def append_jsonl(
    path: Path,
    payload: dict,
) -> None:

    ensure_data_dir()

    with path.open(
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def emit_event(
    event: str,
    **fields,
) -> None:

    append_jsonl(
        EVENT_FILE,
        {
            "timestamp": utc_now(),
            "event": event,
            **fields,
        },
    )


# ============================================================
# CPU CONTROL
# ============================================================

def detected_threads() -> int:
    return max(
        1,
        os.cpu_count() or 1,
    )


def choose_threads(
    requested: int | None,
) -> int:

    available = detected_threads()

    if requested is None:
        return available

    if requested <= 0:
        raise ValueError(
            "--threads must be greater than zero"
        )

    return min(
        requested,
        available,
    )


# ============================================================
# CYTHAN / MIRACLE ENGINE
# ============================================================

def blake3_compatible(
    data: bytes,
) -> bytes:
    """
    Prefer BLAKE3 when installed.

    The fallback uses BLAKE2b so the orchestration program remains
    runnable without the optional blake3 Python package.

    This is an auxiliary telemetry state, not RandomX PoW.
    """

    try:

        from blake3 import blake3

        return blake3(
            data
        ).digest(length=32)

    except ImportError:

        return hashlib.blake2b(
            data,
            digest_size=32,
        ).digest()


def keccak_256(
    data: bytes,
) -> bytes:

    try:

        from Crypto.Hash import keccak

        h = keccak.new(
            digest_bits=256
        )

        h.update(data)

        return h.digest()

    except ImportError:

        # Explicit fallback.
        # hashlib.sha3_256 is not Keccak-256.
        return hashlib.sha3_256(
            b"KECCAK-FALLBACK|"
            + data
        ).digest()


def cythanize_cast(
    state: bytes,
) -> bytes:

    a = blake3_compatible(
        b"CYTHANIZE|CAST|A|"
        + state
    )

    b = keccak_256(
        b"CYTHANIZE|CAST|B|"
        + a
        + state
    )

    c = blake3_compatible(
        b"CYTHANIZE|CAST|C|"
        + b
        + a[::-1]
    )

    return blake3_compatible(
        b"CYTHANIZE|CAST|FINAL|"
        + a
        + b
        + c
    )


def radiant(
    state: bytes,
) -> bytes:

    lanes = []

    current = state

    for index in range(8):

        lane = keccak_256(
            b"RADIANT|LANE|"
            + index.to_bytes(1, "big")
            + current
        )

        lane = blake3_compatible(
            b"RADIANT|BLAKE|"
            + index.to_bytes(1, "big")
            + lane
            + current
        )

        lanes.append(lane)

        current = lane

    return keccak_256(
        b"RADIANT|FOLD|"
        + b"".join(lanes)
    )


def miracle_code(
    miner_state: bytes,
    label: str = "CYTHANIZE-DREAM",
) -> dict[str, str]:

    label_bytes = label.encode(
        "utf-8"
    )

    cythan = cythanize_cast(
        b"MINER|"
        + label_bytes
        + b"|"
        + miner_state
    )

    keccak_state = keccak_256(
        b"CYTHANIZE|KECCAK|"
        + cythan
        + miner_state
    )

    radiant_state = radiant(
        keccak_state
        + cythan
    )

    miracle = keccak_256(
        b"MIRACLE|SINGLE-CODE|"
        + cythan
        + keccak_state
        + radiant_state
    )

    dream = blake3_compatible(
        b"DREAM|FINAL|"
        + miracle
        + radiant_state
        + keccak_state
        + cythan
    )

    return {
        "cythan": cythan.hex(),
        "keccak": keccak_state.hex(),
        "radiant": radiant_state.hex(),
        "miracle": miracle.hex(),
        "dream": dream.hex(),
    }


def make_miracle_state(
    xno_address: str,
    miner_summary: dict | None,
) -> dict:

    payload = json.dumps(
        {
            "xno": xno_address,
            "summary": miner_summary or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode(
        "utf-8"
    )

    state = miracle_code(
        blake3_compatible(
            b"CYTHAN|MINER-CAST|"
            + payload
        )
    )

    return {
        "timestamp": utc_now(),
        "xno_address": xno_address,
        "source": "CYTHAN-MIRACLE-AUXILIARY",
        "state": state,
    }


# ============================================================
# XMRIG HTTP API
# ============================================================

def api_get(
    path: str,
    token: str | None,
) -> dict | None:

    url = (
        f"http://{API_HOST}:{API_PORT}"
        f"{path}"
    )

    request = urllib.request.Request(
        url
    )

    if token:
        request.add_header(
            "Authorization",
            f"Bearer {token}",
        )

    try:

        with urllib.request.urlopen(
            request,
            timeout=3,
        ) as response:

            return json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ):

        return None


def get_summary(
    token: str | None,
) -> dict | None:

    return api_get(
        "/1/summary",
        token,
    )


# ============================================================
# HASHRATE / SHARE EXTRACTION
# ============================================================

def parse_hashrate(
    summary: dict | None,
) -> tuple[
    float | None,
    float | None,
    float | None,
]:

    if not summary:
        return None, None, None

    hashrate = summary.get(
        "hashrate",
        {},
    )

    total = hashrate.get(
        "total",
        [],
    )

    values = []

    for value in total[:3]:

        try:
            values.append(
                float(value)
            )

        except (
            TypeError,
            ValueError,
        ):

            values.append(None)

    while len(values) < 3:
        values.append(None)

    return (
        values[0],
        values[1],
        values[2],
    )


def parse_results(
    summary: dict | None,
) -> tuple[
    int | None,
    int | None,
]:

    if not summary:
        return None, None

    results = summary.get(
        "results",
        {},
    )

    accepted = results.get(
        "shares_good"
    )

    rejected = results.get(
        "shares_bad"
    )

    return accepted, rejected


# ============================================================
# XMRIG COMMAND
# ============================================================

def build_xmrig_command(
    executable: str,
    xno_address: str,
    threads: int,
    rig_id: str,
    api_token: str | None,
) -> list[str]:

    command = [
        executable,

        "-o",
        POOL,

        "-a",
        ALGORITHM,

        "-k",

        "-u",
        xno_address,

        "-p",
        "x",

        "-t",
        str(threads),

        "--http-host",
        API_HOST,

        "--http-port",
        str(API_PORT),
    ]

    if rig_id:
        command.extend(
            [
                "--rig-id",
                rig_id,
            ]
        )

    if api_token:
        command.extend(
            [
                "--http-access-token",
                api_token,
            ]
        )

    return command


def start_xmrig(
    executable: str,
    xno_address: str,
    threads: int,
    rig_id: str,
    api_token: str | None,
) -> subprocess.Popen:

    command = build_xmrig_command(
        executable,
        xno_address,
        threads,
        rig_id,
        api_token,
    )

    print()
    print(
        "Launching XMRig:"
    )
    print(
        " ".join(command)
    )
    print()

    emit_event(
        "xmrig_start",
        command=command,
        pool=POOL,
        algorithm=ALGORITHM,
        threads=threads,
    )

    return subprocess.Popen(
        command
    )


# ============================================================
# PROCESS CONTROL
# ============================================================

def stop_xmrig() -> None:

    global MINER_PROCESS

    if MINER_PROCESS is None:
        return

    if MINER_PROCESS.poll() is not None:
        return

    print(
        "\nStopping XMRig..."
    )

    emit_event(
        "xmrig_stop_requested"
    )

    try:

        if platform.system() == "Windows":

            MINER_PROCESS.terminate()

        else:

            MINER_PROCESS.send_signal(
                signal.SIGINT
            )

        MINER_PROCESS.wait(
            timeout=10
        )

    except subprocess.TimeoutExpired:

        print(
            "XMRig did not stop cleanly; killing process."
        )

        MINER_PROCESS.kill()

    finally:

        MINER_PROCESS = None


def signal_handler(
    signum,
    frame,
) -> None:

    global RUNNING

    RUNNING = False

    stop_xmrig()


# ============================================================
# TELEMETRY
# ============================================================

def record_telemetry(
    xno_address: str,
    threads: int,
    summary: dict | None,
) -> None:

    (
        hash_10s,
        hash_1m,
        hash_15m,
    ) = parse_hashrate(
        summary
    )

    accepted, rejected = parse_results(
        summary
    )

    uptime = (
        summary.get("uptime")
        if summary
        else None
    )

    payload = {
        "timestamp": utc_now(),

        "pool": POOL,

        "algorithm": ALGORITHM,

        "xno_address": xno_address,

        "threads": threads,

        "hashrate": {
            "10s": hash_10s,
            "1m": hash_1m,
            "15m": hash_15m,
        },

        "shares": {
            "accepted": accepted,
            "rejected": rejected,
        },

        "uptime": uptime,

        "pid": (
            MINER_PROCESS.pid
            if MINER_PROCESS
            else None
        ),
    }

    append_jsonl(
        TELEMETRY_FILE,
        payload,
    )

    miracle = make_miracle_state(
        xno_address,
        summary,
    )

    append_jsonl(
        MIRACLE_FILE,
        miracle,
    )

    print(
        f"[{payload['timestamp']}] "
        f"10s={hash_10s or '-'} H/s | "
        f"1m={hash_1m or '-'} H/s | "
        f"15m={hash_15m or '-'} H/s | "
        f"accepted={accepted if accepted is not None else '-'} | "
        f"rejected={rejected if rejected is not None else '-'}"
    )


# ============================================================
# CONFIGURATION FILE
# ============================================================

def write_runtime_config(
    xno_address: str,
    threads: int,
    rig_id: str,
) -> None:

    ensure_data_dir()

    config = {
        "timestamp": utc_now(),
        "pool": POOL,
        "algorithm": ALGORITHM,
        "xno_address": xno_address,
        "threads": threads,
        "rig_id": rig_id,
        "api": {
            "host": API_HOST,
            "port": API_PORT,
        },
    }

    path = DATA_DIR / "runtime.json"

    path.write_text(
        json.dumps(
            config,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# MAIN AUTOMATION
# ============================================================

def run(
    args: argparse.Namespace,
) -> int:

    global MINER_PROCESS

    if not args.xno.startswith(
        "nano_"
    ):

        print(
            "ERROR: XNO address must begin with nano_.",
            file=sys.stderr,
        )

        return 2

    threads = choose_threads(
        args.threads
    )

    ensure_data_dir()

    write_runtime_config(
        args.xno,
        threads,
        args.rig_id,
    )

    print()
    print("=" * 72)
    print(
        "                 CYTHAN AUTO XNO MINER"
    )
    print("=" * 72)

    print(
        f"Pool       : {POOL}"
    )

    print(
        f"Algorithm  : {ALGORITHM} / RandomX"
    )

    print(
        f"XNO        : {args.xno}"
    )

    print(
        f"CPU        : {threads}/{detected_threads()} threads"
    )

    print(
        f"Worker     : {args.rig_id}"
    )

    print(
        f"Telemetry  : {TELEMETRY_FILE}"
    )

    print(
        f"Miracle    : {MIRACLE_FILE}"
    )

    print(
        f"Events     : {EVENT_FILE}"
    )

    print("=" * 72)

    emit_event(
        "system_start",
        xno_address=args.xno,
        threads=threads,
        pool=POOL,
        algorithm=ALGORITHM,
    )

    MINER_PROCESS = start_xmrig(
        args.xmrig,
        args.xno,
        threads,
        args.rig_id,
        args.api_token,
    )

    # Give XMRig time to initialize its API.
    time.sleep(
        max(
            1,
            args.startup_delay,
        )
    )

    while RUNNING:

        if MINER_PROCESS.poll() is not None:

            exit_code = (
                MINER_PROCESS.returncode
            )

            emit_event(
                "xmrig_exit",
                returncode=exit_code,
            )

            print(
                f"XMRig exited with code {exit_code}."
            )

            if args.restart and RUNNING:

                print(
                    "Restarting XMRig..."
                )

                time.sleep(
                    args.restart_delay
                )

                MINER_PROCESS = start_xmrig(
                    args.xmrig,
                    args.xno,
                    threads,
                    args.rig_id,
                    args.api_token,
                )

                continue

            return (
                exit_code
                if exit_code is not None
                else 1
            )

        summary = get_summary(
            args.api_token
        )

        record_telemetry(
            args.xno,
            threads,
            summary,
        )

        time.sleep(
            max(
                1,
                args.interval,
            )
        )

    stop_xmrig()

    emit_event(
        "system_stop"
    )

    return 0


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description=(
            "Automated CYTHAN + XMRig + Nanswap XNO mining controller"
        )
    )

    parser.add_argument(
        "--xno",
        required=True,
        help="Nano payout address",
    )

    parser.add_argument(
        "--xmrig",
        default="xmrig",
        help="Path to XMRig executable",
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help=(
            "CPU threads to use. "
            "Default: all detected logical CPUs."
        ),
    )

    parser.add_argument(
        "--rig-id",
        default="cythan",
        help="XMRig worker/rig identifier",
    )

    parser.add_argument(
        "--api-token",
        default=None,
        help="Optional XMRig HTTP API token",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Telemetry interval in seconds",
    )

    parser.add_argument(
        "--startup-delay",
        type=int,
        default=3,
        help="Seconds to wait after starting XMRig",
    )

    parser.add_argument(
        "--restart",
        action="store_true",
        help="Automatically restart XMRig after an unexpected exit",
    )

    parser.add_argument(
        "--restart-delay",
        type=int,
        default=5,
        help="Seconds before restarting XMRig",
    )

    return parser


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> int:

    parser = build_parser()

    args = parser.parse_args()

    signal.signal(
        signal.SIGINT,
        signal_handler,
    )

    if hasattr(
        signal,
        "SIGTERM",
    ):

        signal.signal(
            signal.SIGTERM,
            signal_handler,
        )

    try:

        return run(
            args
        )

    except KeyboardInterrupt:

        stop_xmrig()

        return 130

    except Exception as exc:

        emit_event(
            "fatal_error",
            error=str(exc),
        )

        print(
            f"FATAL: {exc}",
            file=sys.stderr,
        )

        stop_xmrig()

        return 1


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
