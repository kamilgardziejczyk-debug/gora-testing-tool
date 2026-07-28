"""Renders a scenario run into a self-contained HTML report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader

TEMPLATE_NAME = "report.html.j2"


@dataclass
class TestResult:
    """One executed command's outcome, ready to render as a report row."""

    name: str
    tag: str
    raw_yaml: str
    validation_expected: str | None
    validation_actual: str | None
    duration_s: float
    passed: bool
    error: str | None


def generate_report(
    scenario_path: Path,
    started_at: datetime,
    total_duration_s: float,
    results: list[TestResult],
    output_path: Path,
) -> None:
    """Render `results` into a self-contained HTML file at `output_path`.

    `autoescape` is forced on unconditionally rather than inferred from the
    template's filename: message payloads and validation expressions can
    contain `<`, `>` and `&` (an MQTT payload is untrusted device output, and
    operators like `count > 2` use `>` themselves), so escaping must not
    depend on the template happening to end in `.html`.
    """
    env = Environment(loader=PackageLoader("reporting", "templates"), autoescape=True)
    template = env.get_template(TEMPLATE_NAME)

    html = template.render(
        scenario_name=scenario_path.name,
        started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
        total_duration_s=f"{total_duration_s:.2f}",
        results=results,
        passed_count=sum(1 for result in results if result.passed),
        failed_count=sum(1 for result in results if not result.passed),
    )

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
