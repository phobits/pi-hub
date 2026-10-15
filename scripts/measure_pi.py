#!/usr/bin/env python3
"""measure_pi.py — reproducible runtime baseline for the Pi Zero 2 W (Ticket #1).

Runs ON the device, stdlib only, and writes one JSON line per run:

    ssh pi@192.168.178.22 'python3 /opt/smart-display/scripts/measure_pi.py --duration 60'

Collects, in one pass:
  - memory:   MemTotal/MemAvailable/Swap from /proc/meminfo
  - rss:      per-process RSS for the backend, cog and the WPE helper processes
  - cpu:      system-wide busy % over --duration seconds (from /proc/stat), plus
              per-process busy % for the same processes (from /proc/<pid>/stat)
  - disk:     sectors written to the SD card since boot and during the sample
              (/proc/diskstats, device mmcblk0) → bytes
  - latency:  GET /api/state p50/p95 over N requests (loopback)
  - services: NRestarts of both units, journald disk usage, uptime, load,
              SoC temperature, firmware throttle flags, systemd-analyze time

Compare two runs (before/after a change, or t0 and t0+24h) by diffing the
``disk.written_bytes_since_boot`` and watching ``memory.available_mb``.
Output dir defaults to <install>/data/measurements (rsync-excluded, survives
deploys).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import http.client
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


PROCESS_PATTERNS = {
    "backend": re.compile(r"smart_display\.app"),
    "cog": re.compile(r"(^|/)cog(\s|$)"),
    "wpe_web": re.compile(r"WPEWebProcess"),
    "wpe_network": re.compile(r"WPENetworkProcess"),
}
SECTOR_BYTES = 512


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        number = rest.strip().split(" ")[0]
        if number.isdigit():
            values[key] = int(number)
    kb = lambda name: values.get(name, 0)  # noqa: E731
    return {
        "total_mb": round(kb("MemTotal") / 1024, 1),
        "available_mb": round(kb("MemAvailable") / 1024, 1),
        "swap_total_mb": round(kb("SwapTotal") / 1024, 1),
        "swap_free_mb": round(kb("SwapFree") / 1024, 1),
    }


def list_processes() -> list[dict]:
    processes = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmdline = read_text(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        if not cmdline:
            continue
        role = None
        for name, pattern in PROCESS_PATTERNS.items():
            if pattern.search(cmdline):
                role = name
                break
        if role is None:
            continue
        processes.append({"pid": pid, "role": role, "cmdline": cmdline[:120]})
    return processes


def rss_kb(pid: int) -> int:
    for line in read_text(f"/proc/{pid}/status").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return 0


def proc_cpu_ticks(pid: int) -> int:
    stat = read_text(f"/proc/{pid}/stat")
    if not stat:
        return 0
    # Fields after the ")" of comm: index 11/12 are utime/stime (0-based from there).
    tail = stat.rsplit(")", 1)[-1].split()
    try:
        return int(tail[11]) + int(tail[12])
    except (IndexError, ValueError):
        return 0


def system_cpu_ticks() -> tuple[int, int]:
    first = read_text("/proc/stat").splitlines()[0].split()
    values = [int(v) for v in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def disk_written_sectors(device: str) -> int | None:
    for line in read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[2] == device:
            return int(parts[9])
    return None


def latency_ms(port: int, samples: int) -> dict:
    timings: list[float] = []
    errors = 0
    for _ in range(samples):
        started = time.perf_counter()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/state")
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status != 200:
                errors += 1
                continue
        except OSError:
            errors += 1
            continue
        timings.append((time.perf_counter() - started) * 1000)
    if not timings:
        return {"samples": samples, "errors": errors}
    timings.sort()
    p95_index = min(len(timings) - 1, int(round(0.95 * len(timings))) - 1)
    return {
        "samples": samples,
        "errors": errors,
        "p50_ms": round(statistics.median(timings), 2),
        "p95_ms": round(timings[max(0, p95_index)], 2),
        "max_ms": round(timings[-1], 2),
    }


def run(argv: list[str]) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def services() -> dict:
    data: dict = {}
    for unit in ("smart-display", "smart-display-kiosk"):
        restarts = run(["systemctl", "show", unit, "-p", "NRestarts", "--value"])
        active = run(["systemctl", "is-active", unit])
        data[unit] = {
            "active": active or None,
            "restarts": int(restarts) if restarts.isdigit() else None,
        }
    data["journal_disk_usage"] = run(["journalctl", "--disk-usage"]) or None
    data["systemd_analyze_time"] = run(["systemd-analyze", "time"]) or None
    data["throttled"] = run(["vcgencmd", "get_throttled"]) or None
    temp = read_text("/sys/class/thermal/thermal_zone0/temp").strip()
    data["soc_temp_c"] = round(int(temp) / 1000, 1) if temp.isdigit() else None
    uptime = read_text("/proc/uptime").split()
    data["uptime_s"] = int(float(uptime[0])) if uptime else None
    load = read_text("/proc/loadavg").split()
    data["loadavg"] = [float(v) for v in load[:3]] if len(load) >= 3 else None
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--duration", type=int, default=60, help="CPU sample window in seconds")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--device", default="mmcblk0", help="block device for SD write counters")
    parser.add_argument("--latency-samples", type=int, default=50)
    parser.add_argument("--out", default=None, help="directory for the JSON line (default: <install>/data/measurements)")
    parser.add_argument("--label", default="", help="free-text tag, e.g. 'baseline' or 'after-debounce'")
    args = parser.parse_args()

    install_dir = Path(__file__).resolve().parent.parent
    out_dir = Path(args.out) if args.out else install_dir / "data" / "measurements"

    processes = list_processes()
    cpu_total_0, cpu_idle_0 = system_cpu_ticks()
    proc_ticks_0 = {p["pid"]: proc_cpu_ticks(p["pid"]) for p in processes}
    sectors_0 = disk_written_sectors(args.device)

    print(f"Sampling for {args.duration} s …", file=sys.stderr)
    time.sleep(max(1, args.duration))

    cpu_total_1, cpu_idle_1 = system_cpu_ticks()
    sectors_1 = disk_written_sectors(args.device)
    total_delta = max(1, cpu_total_1 - cpu_total_0)
    busy_pct = round(100.0 * (1 - (cpu_idle_1 - cpu_idle_0) / total_delta), 1)
    clk_tck = os.sysconf("SC_CLK_TCK")
    cpu_count = os.cpu_count() or 1

    rss: dict[str, dict] = {}
    for proc in processes:
        pid = proc["pid"]
        ticks = proc_cpu_ticks(pid) - proc_ticks_0.get(pid, 0)
        proc_pct = round(100.0 * ticks / clk_tck / max(1, args.duration) / cpu_count, 1)
        entry = {
            "pid": pid,
            "rss_mb": round(rss_kb(pid) / 1024, 1),
            "cpu_pct": proc_pct,
            "cmdline": proc["cmdline"],
        }
        rss.setdefault(proc["role"], []).append(entry)

    result = {
        "measured_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "label": args.label,
        "duration_s": args.duration,
        "memory": meminfo(),
        "processes": rss,
        "cpu": {"system_busy_pct": busy_pct, "cores": cpu_count},
        "disk": {
            "device": args.device,
            "written_bytes_since_boot": (sectors_1 or 0) * SECTOR_BYTES if sectors_1 is not None else None,
            "written_bytes_during_sample": (
                (sectors_1 - sectors_0) * SECTOR_BYTES
                if sectors_0 is not None and sectors_1 is not None
                else None
            ),
        },
        "latency": latency_ms(args.port, args.latency_samples),
        "services": services(),
    }

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{result['measured_at'].replace(':', '')}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
        result["saved_to"] = str(out_file)
    except OSError as exc:
        result["saved_to"] = f"nicht gespeichert: {exc}"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
