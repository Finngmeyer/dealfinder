"""The one-command run and the scheduler."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from dealfinder import scheduler
from dealfinder.cli import app
from dealfinder.http import Fetcher
from dealfinder.pipeline.classify import classify_all
from dealfinder.pipeline.run import run_all, write_log
from dealfinder.settings import Environment, Schedule
from factories import rentcast_record
from test_classify_pipeline import store
from test_funnel import fourplex
from test_rentcast_sweep import make_source, serve

PYTHON = "/opt/py/bin/python"
PROJECT = Path("/home/me/dealfinder")
LOGS = Path("/home/me/dealfinder/data/logs")


@pytest.fixture
def schedule() -> Schedule:
    return Schedule(daily_at="07:05", weekly_on="sunday", weekly_at="06:30")


class TestSchedule:
    def test_entries(self, schedule):
        daily, weekly = scheduler.entries(schedule, PYTHON)
        assert (daily.hour, daily.minute, daily.days) == (7, 5, [1, 2, 3, 4, 5, 6])
        assert (weekly.hour, weekly.minute, weekly.days) == (6, 30, [0])
        assert daily.command == f'"{PYTHON}" -m dealfinder.cli run --mode daily'
        assert weekly.command.endswith("run --mode weekly")

    def test_the_weekly_day_moves_the_daily_gap(self):
        midweek = Schedule(daily_at="20:00", weekly_on="wednesday", weekly_at="21:15")
        daily, weekly = scheduler.entries(midweek, PYTHON)
        assert daily.days == [0, 1, 2, 4, 5, 6] and weekly.days == [3]

    @pytest.mark.parametrize("bad", ["7:00", "24:00", "07:60", "noon"])
    def test_times_are_checked(self, bad):
        with pytest.raises(ValueError):
            Schedule(daily_at=bad, weekly_on="sunday", weekly_at="06:00")

    def test_crontab_lines(self, schedule):
        lines = scheduler.crontab_lines(scheduler.entries(schedule, PYTHON), PROJECT, LOGS)
        assert lines == [
            f'5 7 * * 1,2,3,4,5,6 cd "{PROJECT}" && "{PYTHON}" -m dealfinder.cli run --mode daily '
            f'>> "{LOGS}/daily.log" 2>&1 # dealfinder',
            f'30 6 * * 0 cd "{PROJECT}" && "{PYTHON}" -m dealfinder.cli run --mode weekly '
            f'>> "{LOGS}/weekly.log" 2>&1 # dealfinder',
        ]

    def test_merge_keeps_other_lines_and_replaces_ours(self):
        existing = "MAILTO=me\n0 3 * * * backup.sh\n0 7 * * * old run # dealfinder\n\n"
        merged = scheduler.merge_crontab(existing, ["1 2 * * * new # dealfinder"])
        assert merged == "MAILTO=me\n0 3 * * * backup.sh\n1 2 * * * new # dealfinder\n"
        assert scheduler.merge_crontab(merged, []) == "MAILTO=me\n0 3 * * * backup.sh\n"
        assert scheduler.merge_crontab("", []) == ""

    def test_windows_commands(self, schedule):
        daily, weekly = scheduler.windows_commands(
            scheduler.entries(schedule, PYTHON), PROJECT, LOGS
        )
        assert daily[:6] == ["schtasks", "/Create", "/F", "/TN", "dealfinder daily", "/SC"]
        assert daily[daily.index("/D") + 1] == "MON,TUE,WED,THU,FRI,SAT"
        assert daily[daily.index("/ST") + 1] == "07:05"
        assert weekly[weekly.index("/D") + 1] == "SUN" and "run --mode weekly" in weekly[-1]

    def test_describe(self, schedule):
        lines = scheduler.describe(scheduler.entries(schedule, PYTHON))
        assert lines[0].startswith("daily: 07:05 on Monday, Tuesday")
        assert lines[1].startswith("weekly: 06:30 on Sunday")


class Shell:
    """Stands in for subprocess.run and remembers what was asked of it."""

    def __init__(self, crontab: str | None = "0 3 * * * backup.sh\n", fail: bool = False) -> None:
        self.crontab, self.fail, self.calls = crontab, fail, []

    def __call__(self, command, **options):
        self.calls.append((command, options.get("input")))
        if command == ["crontab", "-l"]:
            if self.crontab is None:
                return subprocess.CompletedProcess(command, 1, "", "no crontab for me")
            return subprocess.CompletedProcess(command, 0, self.crontab, "")
        if self.fail:
            return subprocess.CompletedProcess(command, 1, "", "permission denied")
        if command == ["crontab", "-"]:
            self.crontab = options["input"]
        return subprocess.CompletedProcess(command, 0, "", "")


class TestInstall:
    def items(self, schedule):
        return scheduler.entries(schedule, PYTHON)

    def test_cron_install_then_remove(self, schedule, tmp_path):
        shell = Shell()
        logs = tmp_path / "logs"
        message = scheduler.install(
            self.items(schedule), PROJECT, logs, platform="linux", run=shell
        )
        assert message.startswith("Added two lines to your crontab")
        assert logs.is_dir()
        assert shell.crontab.startswith("0 3 * * * backup.sh\n5 7 * * 1,2,3,4,5,6 cd ")
        assert shell.crontab.count("# dealfinder") == 2
        # Installing twice does not double the lines.
        scheduler.install(self.items(schedule), PROJECT, logs, platform="linux", run=shell)
        assert shell.crontab.count("# dealfinder") == 2

        assert scheduler.remove(platform="darwin", run=shell) == (
            "Removed dealfinder's lines from your crontab."
        )
        assert shell.crontab == "0 3 * * * backup.sh\n"
        assert scheduler.remove(platform="linux", run=shell) == (
            "There was nothing of dealfinder's in your crontab."
        )

    def test_no_crontab_yet(self, schedule, tmp_path):
        shell = Shell(crontab=None)
        scheduler.install(self.items(schedule), PROJECT, tmp_path, platform="linux", run=shell)
        assert shell.crontab.count("\n") == 2

    def test_cron_refusal_is_reported(self, schedule, tmp_path):
        with pytest.raises(RuntimeError, match="crontab refused: permission denied"):
            scheduler.install(
                self.items(schedule), PROJECT, tmp_path, platform="linux", run=Shell(fail=True)
            )

    def test_windows(self, schedule, tmp_path):
        shell = Shell()
        message = scheduler.install(
            self.items(schedule), PROJECT, tmp_path, platform="win32", run=shell
        )
        assert "Task Scheduler" in message
        assert [call[0][:2] for call in shell.calls] == [["schtasks", "/Create"]] * 2
        scheduler.remove(platform="win32", run=shell)
        assert shell.calls[-1][0][:2] == ["schtasks", "/Delete"]
        with pytest.raises(RuntimeError, match="Task Scheduler refused"):
            scheduler.install(
                self.items(schedule), PROJECT, tmp_path, platform="win32", run=Shell(fail=True)
            )

    def test_the_command_only_shows_by_default(self, engine):
        result = CliRunner().invoke(app, ["schedule"], env={"COLUMNS": "250"})
        assert result.exit_code == 0, result.output
        assert "daily: 07:00 on Monday" in result.output
        assert "This is what --install would add:" in result.output
        assert "run --mode weekly" in result.output and "# dealfinder" in result.output

    def test_install_and_remove_cannot_be_combined(self, engine):
        result = CliRunner().invoke(app, ["schedule", "--install", "--remove"])
        assert result.exit_code == 2 and "Choose one" in result.output


class TestRunAll:
    @pytest.fixture
    def offline(self, tmp_path):
        return Fetcher(tmp_path / "offline", offline=True)

    def test_without_rentcast_the_rest_still_runs(self, engine, config, offline, monkeypatch):
        from dealfinder.pipeline import underwrite
        from factories import market_context

        monkeypatch.setattr(
            underwrite, "load_market", lambda zip_code, *_a, **_k: market_context(zip_code)
        )
        store(engine, fourplex(1, 200_000.0), fourplex(2, 900_000.0))
        report = run_all(
            engine, config, Environment(), offline, mode="daily",
            now=datetime(2026, 11, 13, 7, 0),
        )  # fmt: skip
        assert report.ok and [step.name for step in report.steps] == [
            "Listings", "Classify", "Underwrite", "Alerts",
        ]  # fmt: skip
        text = report.text()
        assert text.startswith("Dealfinder daily run, 2026-11-13 07:00")
        assert "RENTCAST_API_KEY is not set" in text
        assert "2 properties stored; 2 read in this run" in text
        assert "Stage 1: 2 listings of the wanted types; 2 underwritten" in text
        assert "== Alerts ==" in text

    def test_with_rentcast_it_sweeps_first(self, engine, config, fetcher, http_mock, monkeypatch):
        from dealfinder.adapters.rentcast import PROPERTIES_URL, RENT_URL
        from dealfinder.pipeline import underwrite
        from factories import market_context
        from test_enrichment_sources import serve_fema

        monkeypatch.setattr(
            underwrite, "load_market", lambda zip_code, *_a, **_k: market_context(zip_code)
        )
        serve(http_mock, {"OH": [rentcast_record(n, price=180_000 + n) for n in (1, 2)]})
        http_mock.get(url__startswith=f"{PROPERTIES_URL}/").mock(return_value=httpx.Response(404))
        http_mock.get(RENT_URL).mock(return_value=httpx.Response(404))
        serve_fema(http_mock)
        source, guard = make_source(engine, config, fetcher)
        report = run_all(
            engine, config, Environment(), fetcher, mode="weekly", states=["OH"],
            rentcast=source, guard=guard, alerts=False,
        )  # fmt: skip
        text = report.text()
        assert "weekly sweep, completed" in text and "Listings seen 2; new 2" in text
        assert "Stage 2: 2 listings enriched." in text
        assert "Alerts skipped, as asked." in text
        assert guard.used() == 1  # the sweep; the 404s that followed are not billed

    def test_switches(self, engine, config, offline):
        config.settings.sources["rentcast"].enabled = False
        off = run_all(engine, config, Environment(), offline, mode="daily", alerts=False)
        assert "RentCast is switched off in settings; no sweep." in off.text()
        skipped = run_all(
            engine, config, Environment(), offline, mode="daily", sweep=False, alerts=False
        )
        assert "Sweep skipped, as asked." in skipped.text()

    def test_a_bad_state_fails_that_step_only(self, engine, config, fetcher):
        source, guard = make_source(engine, config, fetcher)
        report = run_all(
            engine, config, Environment(), fetcher, mode="daily", states=["AK"],
            rentcast=source, guard=guard, alerts=False,
        )  # fmt: skip
        assert not report.ok and "AK not covered" in report.steps[0].text
        assert [step.ok for step in report.steps] == [False, True, True, True]

    def test_log_file(self, engine, config, offline, tmp_path):
        report = run_all(
            engine, config, Environment(), offline, mode="daily", sweep=False, alerts=False,
            now=datetime(2026, 11, 13, 7, 0),
        )  # fmt: skip
        path = write_log(report, tmp_path / "logs")
        write_log(report, tmp_path / "logs")
        assert path.name == "runs-2026-11.log"
        assert path.read_text().count("Dealfinder daily run, 2026-11-13 07:00") == 2

    def test_command(self, engine, config):
        store(engine, fourplex(1, 200_000.0, "Quadplex. All units rented at $1,200/mo."))
        classify_all(engine, config)
        result = CliRunner().invoke(app, ["run", "--mode", "daily"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "Dealfinder daily run" in result.output and "== Underwrite ==" in result.output
        assert "Logged to" in result.output
        bad = CliRunner().invoke(app, ["run", "--mode", "hourly"])
        assert bad.exit_code == 2 and "Use daily or weekly" in bad.output
