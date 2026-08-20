"""Renders a scenario run into a self-contained HTML report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, PackageLoader

if TYPE_CHECKING:  # Import for typing only - reporting must not depend on the tool at runtime.
    from tools.dut_logger import LogSession

TEMPLATE_NAME = "report.html.j2"

# Heading shown for each log kind the run produced. Keyed by the kind names
# `LogSession` reports, so a run that never opened a broker session or a shell
# simply has no row for those - rather than a link to an empty file, which
# reads as something having gone wrong with the capture.
LOG_LABELS = {
    "tool": "Tool log",
    "device": "DUT log",
    "mqtt": "MQTT log",
    "cli": "CLI log",
    "combined": "Combined log",
}


@dataclass
class TestResult:
    """One executed command's outcome, ready to render as a report row.

    `group` and `group_id` carry the `!Group` the command was nested in, from
    the wrapper the Parser stamped them onto. They default to None so a
    command outside any group - and any caller predating groups - needs no
    change.
    """

    name: str
    tag: str
    raw_yaml: str
    validation_expected: str | None
    validation_actual: str | None
    duration_s: float
    passed: bool
    error: str | None
    group: str | None = None
    group_id: int | None = None


@dataclass
class ReportSection:
    """A run of consecutive results sharing one `!Group`, or an ungrouped run.

    `name` is None for commands that sat outside any group; the template then
    renders their rows with no heading, so a scenario using no groups at all
    looks exactly as it did before groups existed.
    """

    name: str | None
    results: list[TestResult]
    passed_count: int
    total_count: int


def generate_report(
    scenario_path: Path,
    started_at: datetime,
    total_duration_s: float,
    results: list[TestResult],
    output_path: Path,
    session: "LogSession | None" = None,
    scenario_name: str | None = None,
) -> None:
    """Render `results` into a self-contained HTML file at `output_path`.

    `autoescape` is forced on unconditionally rather than inferred from the
    template's filename: message payloads and validation expressions can
    contain `<`, `>` and `&` (an MQTT payload is untrusted device output, and
    operators like `count > 2` use `>` themselves), so escaping must not
    depend on the template happening to end in `.html`.

    `session`, when given, contributes the run's log filenames so the report
    points at the artefacts sitting beside it. Only the names are rendered,
    not the contents: a chatty DUT would otherwise bloat the HTML, and the
    logs are more useful greppable on disk. Only the logs the run actually
    used are listed - see `_log_file_names`.

    `scenario_name`, when given, is the heading the report is titled with -
    the scenario's own `name:` field. Left None it falls back to the
    scenario's filename, so a scenario that names itself nothing still gets a
    heading rather than a blank one.
    """
    env = Environment(loader=PackageLoader("reporting", "templates"), autoescape=True)
    template = env.get_template(TEMPLATE_NAME)

    html = template.render(
        scenario_name=scenario_name or scenario_path.name,
        started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
        total_duration_s=total_duration_s,
        results=results,
        sections=build_sections(results),
        passed_count=sum(1 for result in results if result.passed),
        failed_count=sum(1 for result in results if not result.passed),
        log_files=_log_file_names(session),
    )

    _write_html(html, output_path)


def build_sections(results: list[TestResult]) -> list[ReportSection]:
    """Split `results` into consecutive runs sharing a `!Group`.

    Split on `group_id` rather than on the name, so two adjacent groups that
    happen to share a name stay two sections - and so a group repeated by a
    `!Loop` renders once per iteration, which is what the ids were made
    distinct for.

    Done here rather than in the template because it is the testable half of
    the grouping: Jinja then only walks the sections it is handed.
    """
    sections: list[ReportSection] = []

    for result in results:
        if not sections or sections[-1].results[0].group_id != result.group_id:
            sections.append(ReportSection(result.group, [], 0, 0))
        section = sections[-1]
        section.results.append(result)
        section.total_count += 1
        section.passed_count += 1 if result.passed else 0

    return sections


def _log_file_names(session: "LogSession | None") -> list[tuple[str, str]]:
    """`(label, filename)` pairs for the logs this run used, empty when none.

    Only the kinds the session reports as used: a scenario with no broker
    session and no shell commands leaves `mqtt.log` and `cli.log` empty on
    disk, and linking them from the report only invites the reader to wonder
    what went wrong with a capture that was never meant to happen.

    Bare filenames rather than full paths, so the links still resolve when the
    report and its logs are copied off the test node together.
    """
    if session is None:
        return []
    return [(LOG_LABELS[kind], path.name) for kind, path in session.log_files()]


def _write_html(html: str, output_path: Path) -> None:
    """Write the rendered report, creating its directory if needed."""
    report_dir = output_path.parent
    if report_dir.exists() and not report_dir.is_dir():
        # mkdir(exist_ok=True) only tolerates an existing directory; a stale
        # file at the same path (e.g. left over from a --report value that
        # used to be written as a literal file) would otherwise surface as a
        # raw FileExistsError with no indication of what to do about it.
        raise NotADirectoryError(
            f"Cannot write the report: '{report_dir}' already exists and is not a directory. "
            f"Remove it, or pass a different --report path."
        )
    report_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
