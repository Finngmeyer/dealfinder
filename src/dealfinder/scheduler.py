"""Run the pipeline on a schedule, using what the operating system already has.

Linux and macOS: two lines in the user's crontab. Windows: two tasks in Task Scheduler.
Both call ``dealfinder run``: every day except the weekly day in daily mode, and once a week
in weekly mode. Nothing is installed unless asked for, and everything this adds is tagged so
it can be found and removed again.

A laptop that is asleep at the set time simply misses that run; the next one catches up,
because a run only ever adds what is new.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

from dealfinder.settings import Schedule

TAG = "# dealfinder"
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Entry(BaseModel):
    name: str  # "daily" or "weekly"
    mode: str
    hour: int
    minute: int
    days: list[int]  # cron day numbers, Sunday is 0
    command: str  # what to run, without the scheduling part


def entries(schedule: Schedule, python: str) -> list[Entry]:
    """The two scheduled runs. The daily one skips the weekly day, when the weekly run
    already does everything."""
    weekly_day = DAYS.index(schedule.weekly_on)

    def clock(text: str) -> tuple[int, int]:
        hour, minute = text.split(":")
        return int(hour), int(minute)

    def command(mode: str) -> str:
        return f'"{python}" -m dealfinder.cli run --mode {mode}'

    daily_hour, daily_minute = clock(schedule.daily_at)
    weekly_hour, weekly_minute = clock(schedule.weekly_at)
    return [
        Entry(
            name="daily",
            mode="daily",
            hour=daily_hour,
            minute=daily_minute,
            days=[day for day in range(7) if day != weekly_day],
            command=command("daily"),
        ),
        Entry(
            name="weekly",
            mode="weekly",
            hour=weekly_hour,
            minute=weekly_minute,
            days=[weekly_day],
            command=command("weekly"),
        ),
    ]


def crontab_lines(items: Sequence[Entry], project_dir: Path, log_dir: Path) -> list[str]:
    lines = []
    for item in items:
        days = ",".join(str(day) for day in item.days)
        log = log_dir / f"{item.name}.log"
        lines.append(
            f'{item.minute} {item.hour} * * {days} cd "{project_dir}" && {item.command} '
            f'>> "{log}" 2>&1 {TAG}'
        )
    return lines


def merge_crontab(existing: str, lines: Sequence[str]) -> str:
    """The crontab with this app's lines replaced by ``lines`` and everything else kept."""
    kept = [line for line in existing.splitlines() if not line.rstrip().endswith(TAG)]
    while kept and not kept[-1].strip():
        kept.pop()
    merged = [*kept, *lines]
    return "\n".join(merged) + "\n" if merged else ""


def windows_commands(items: Sequence[Entry], project_dir: Path, log_dir: Path) -> list[list[str]]:
    names = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    commands = []
    for item in items:
        log = log_dir / f"{item.name}.log"
        action = f'cmd /c cd /d "{project_dir}" && {item.command} >> "{log}" 2>&1'
        commands.append(
            [
                "schtasks", "/Create", "/F",
                "/TN", f"dealfinder {item.name}",
                "/SC", "WEEKLY",
                "/D", ",".join(names[day] for day in item.days),
                "/ST", f"{item.hour:02d}:{item.minute:02d}",
                "/TR", action,
            ]
        )  # fmt: skip
    return commands


def describe(items: Sequence[Entry]) -> list[str]:
    lines = []
    for item in items:
        days = (
            "every day"
            if len(item.days) == 7
            else "on " + ", ".join(DAYS[day].capitalize() for day in item.days)
        )
        lines.append(f"{item.name}: {item.hour:02d}:{item.minute:02d} {days}: {item.command}")
    return lines


def install(
    items: Sequence[Entry],
    project_dir: Path,
    log_dir: Path,
    *,
    platform: str = sys.platform,
    run: Runner = subprocess.run,
) -> str:
    """Add the schedule to cron or Task Scheduler. Returns what was done, in a sentence."""
    log_dir.mkdir(parents=True, exist_ok=True)
    if platform.startswith("win"):
        for command in windows_commands(items, project_dir, log_dir):
            done = run(command, capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError(f"Task Scheduler refused: {(done.stderr or '').strip()}")
        return "Added two tasks to Task Scheduler: 'dealfinder daily' and 'dealfinder weekly'."
    current = run(["crontab", "-l"], capture_output=True, text=True)
    existing = current.stdout if current.returncode == 0 else ""  # no crontab yet is fine
    merged = merge_crontab(existing, crontab_lines(items, project_dir, log_dir))
    done = run(["crontab", "-"], input=merged, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"crontab refused: {(done.stderr or '').strip()}")
    return "Added two lines to your crontab. See them with: crontab -l"


def remove(*, platform: str = sys.platform, run: Runner = subprocess.run) -> str:
    """Take this app's schedule out again."""
    if platform.startswith("win"):
        for name in ("daily", "weekly"):
            run(
                ["schtasks", "/Delete", "/F", "/TN", f"dealfinder {name}"],
                capture_output=True,
                text=True,
            )
        return "Removed the dealfinder tasks from Task Scheduler."
    current = run(["crontab", "-l"], capture_output=True, text=True)
    if current.returncode != 0 or TAG not in current.stdout:
        return "There was nothing of dealfinder's in your crontab."
    merged = merge_crontab(current.stdout, [])
    done = run(["crontab", "-"], input=merged, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"crontab refused: {(done.stderr or '').strip()}")
    return "Removed dealfinder's lines from your crontab."
