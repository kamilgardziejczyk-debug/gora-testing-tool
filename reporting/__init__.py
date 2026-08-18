"""HTML test report generation for scenario runs."""

from .report import ReportSection, TestResult, build_sections, generate_report

__all__ = ["ReportSection", "TestResult", "build_sections", "generate_report"]
