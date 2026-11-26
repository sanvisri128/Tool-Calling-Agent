"""
tools.py – Three custom LangChain tools for the Student Planner Agent.

Tools:
  1. get_upcoming_deadlines  – Read tool
  2. calculate_daily_hours   – Compute tool
  3. draft_study_plan        – Draft tool

Design decisions
----------------
* All tools use typed Pydantic input schemas so LangGraph can pass structured
  arguments safely.
* Input validation is performed before any file I/O or computation.
* CSV contents are NEVER evaluated or executed; they are only parsed as plain
  data values.
* Outputs are kept short (≤ 20 lines) to avoid bloating the context window.
"""

from __future__ import annotations

import csv
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent / "data"
_CSV_PATH = _DATA_DIR / "deadlines.csv"

_REQUIRED_COLUMNS = {"title", "due_date", "hours_needed", "subject"}


def _load_csv() -> list[dict]:
    """Load and minimally validate the deadlines CSV."""
    if not _CSV_PATH.exists():
        raise FileNotFoundError(
            f"deadlines.csv not found at {_CSV_PATH}. "
            "Please ensure the data/ directory is present."
        )
    rows: list[dict] = []
    with _CSV_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("deadlines.csv is empty or has no header row.")
        missing = _REQUIRED_COLUMNS - {c.strip().lower() for c in reader.fieldnames}
        if missing:
            raise ValueError(f"deadlines.csv is missing columns: {missing}")
        for row in reader:
            # Strip whitespace from values – treat CSV as plain data only
            rows.append({k.strip().lower(): v.strip() for k, v in row.items()})
    return rows


# ===========================================================================
# Tool 1 – READ: get_upcoming_deadlines
# ===========================================================================

class UpcomingDeadlinesInput(BaseModel):
    days_ahead: int = Field(
        default=7,
        ge=1,
        le=365,
        description="Number of days from today to look ahead for deadlines (1-365).",
    )
    subject_filter: Optional[str] = Field(
        default=None,
        description="Optional subject name to filter results (case-insensitive).",
    )

    @field_validator("subject_filter", mode="before")
    @classmethod
    def sanitise_subject(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        cleaned = str(v).strip()
        if len(cleaned) > 100:
            raise ValueError("subject_filter is too long (max 100 characters).")
        return cleaned


@tool(args_schema=UpcomingDeadlinesInput)
def get_upcoming_deadlines(days_ahead: int = 7, subject_filter: Optional[str] = None) -> str:
    """
    Read upcoming academic deadlines from the CSV file.

    Returns a formatted list of assignments/exams due within `days_ahead` days.
    Optionally filter by subject. Results are sorted by due date (soonest first).

    Args:
        days_ahead: How many days ahead to look (1-365). Default 7.
        subject_filter: Optional subject name to narrow results.

    Returns:
        A plain-text list of matching deadlines, or a message if none found.
    """
    today = date.today()
    rows = _load_csv()

    results: list[tuple[date, dict]] = []
    for row in rows:
        try:
            due = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
        except ValueError:
            # Skip rows with unparseable dates
            continue
        days_remaining = (due - today).days
        if 0 <= days_remaining <= days_ahead:
            if subject_filter and subject_filter.lower() not in row["subject"].lower():
                continue
            results.append((due, row, days_remaining))  # type: ignore[misc]

    if not results:
        subj_note = f" for subject '{subject_filter}'" if subject_filter else ""
        return f"No deadlines found in the next {days_ahead} day(s){subj_note}."

    results.sort(key=lambda x: x[0])
    lines = [f"Upcoming deadlines (next {days_ahead} day(s)):"]
    for due, row, days_rem in results:  # type: ignore[misc]
        try:
            hrs = float(row["hours_needed"])
        except ValueError:
            hrs = 0.0
        lines.append(
            f"  • {row['title']} | {row['subject']} | Due: {due} "
            f"({days_rem}d left) | {hrs:.0f}h needed"
        )
    return "\n".join(lines)


# ===========================================================================
# Tool 2 – COMPUTE: calculate_daily_hours
# ===========================================================================

class DailyHoursInput(BaseModel):
    title: str = Field(description="Exact assignment title as it appears in the CSV.")
    available_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Override for how many days are available to study. "
            "If omitted, computed from today to the due date."
        ),
    )

    @field_validator("title", mode="before")
    @classmethod
    def sanitise_title(cls, v: str) -> str:
        cleaned = str(v).strip()
        if not cleaned:
            raise ValueError("title must not be empty.")
        if len(cleaned) > 200:
            raise ValueError("title is too long (max 200 characters).")
        return cleaned


@tool(args_schema=DailyHoursInput)
def calculate_daily_hours(title: str, available_days: Optional[int] = None) -> str:
    """
    Calculate how many study hours per day a student needs for a given assignment.

    Looks up the assignment by title in the CSV, then divides total hours needed
    by the number of available study days. Uses ceiling division so the student
    always has enough time.

    Args:
        title: The assignment title (must match a row in the CSV).
        available_days: Days available to study. Auto-computed from due date if omitted.

    Returns:
        A short summary with hours/day recommendation, or an error message.
    """
    today = date.today()
    rows = _load_csv()

    match = None
    for row in rows:
        if row["title"].lower() == title.lower():
            match = row
            break

    if match is None:
        return (
            f"Assignment '{title}' not found in the CSV. "
            "Please check the title or use get_upcoming_deadlines to list assignments."
        )

    # Parse hours
    try:
        total_hours = float(match["hours_needed"])
        if total_hours <= 0:
            raise ValueError
    except ValueError:
        return f"Invalid hours_needed value for '{title}': '{match['hours_needed']}'."

    # Determine days
    if available_days is not None:
        days = available_days
    else:
        try:
            due = datetime.strptime(match["due_date"], "%Y-%m-%d").date()
        except ValueError:
            return f"Cannot parse due_date '{match['due_date']}' for '{title}'."
        days = (due - today).days
        if days <= 0:
            return (
                f"'{title}' is due today or already past (due {match['due_date']}). "
                f"Total hours needed: {total_hours:.0f}h – start immediately!"
            )

    hours_per_day = math.ceil(total_hours / days)
    return (
        f"Assignment : {match['title']}\n"
        f"Subject    : {match['subject']}\n"
        f"Due        : {match['due_date']}\n"
        f"Total hours: {total_hours:.0f}h\n"
        f"Days left  : {days}\n"
        f"→ Study {hours_per_day}h/day to finish on time."
    )


# ===========================================================================
# Tool 3 – DRAFT: draft_study_plan
# ===========================================================================

class StudyPlanInput(BaseModel):
    student_name: str = Field(description="Student's first name (used in the message).")
    titles: list[str] = Field(
        description="List of assignment titles to include in the plan (max 10).",
        min_length=1,
        max_length=10,
    )
    daily_study_hours: Optional[float] = Field(
        default=4.0,
        ge=0.5,
        le=16.0,
        description="Total hours the student can study per day across all subjects (0.5-16).",
    )

    @field_validator("student_name", mode="before")
    @classmethod
    def sanitise_name(cls, v: str) -> str:
        cleaned = str(v).strip()
        if not cleaned:
            raise ValueError("student_name must not be empty.")
        if len(cleaned) > 60:
            raise ValueError("student_name is too long (max 60 characters).")
        # Allow only letters, spaces, hyphens, apostrophes
        import re
        if not re.match(r"^[A-Za-z\s'\-]+$", cleaned):
            raise ValueError(
                "student_name may only contain letters, spaces, hyphens and apostrophes."
            )
        return cleaned

    @field_validator("titles", mode="before")
    @classmethod
    def sanitise_titles(cls, v: list) -> list:
        cleaned = [str(t).strip() for t in v if str(t).strip()]
        if not cleaned:
            raise ValueError("titles list must contain at least one non-empty title.")
        return cleaned


@tool(args_schema=StudyPlanInput)
def draft_study_plan(
    student_name: str,
    titles: list[str],
    daily_study_hours: Optional[float] = 4.0,
) -> str:
    """
    Generate a concise, personalised study plan message for a student.

    Looks up each assignment in the CSV, computes a daily allocation, and
    drafts a friendly action plan. Assignments not found in the CSV are noted.

    Args:
        student_name: Student's first name.
        titles: List of assignment titles to include.
        daily_study_hours: Hours the student can study per day (default 4.0).

    Returns:
        A formatted study plan message ready to share with the student.
    """
    today = date.today()
    rows = _load_csv()
    index = {row["title"].lower(): row for row in rows}

    plan_lines: list[str] = [
        f"📚 Study Plan for {student_name}",
        f"   Generated: {today.strftime('%B %d, %Y')}",
        f"   Daily study budget: {daily_study_hours:.1f}h",
        "",
    ]

    found: list[dict] = []
    not_found: list[str] = []
    for title in titles:
        row = index.get(title.lower())
        if row:
            found.append(row)
        else:
            not_found.append(title)

    # Sort found items by due date
    def _due(r: dict) -> date:
        try:
            return datetime.strptime(r["due_date"], "%Y-%m-%d").date()
        except ValueError:
            return date.max

    found.sort(key=_due)

    if found:
        plan_lines.append("Priority order (soonest first):")
        for row in found:
            due = _due(row)
            days_left = max((due - today).days, 1)
            try:
                total_hrs = float(row["hours_needed"])
            except ValueError:
                total_hrs = 0.0
            alloc = min(daily_study_hours or 4.0, total_hrs)
            days_needed = math.ceil(total_hrs / alloc) if alloc > 0 else days_left
            plan_lines.append(
                f"  ✅ {row['title']} ({row['subject']})"
                f" – Due {due} ({days_left}d) | Needs ~{days_needed}d of focused study"
            )

    if not_found:
        plan_lines.append("")
        plan_lines.append("⚠️  Assignments not found in CSV (check spelling):")
        for t in not_found:
            plan_lines.append(f"  ✗ {t}")

    plan_lines += [
        "",
        f"💡 Tip: Start with the earliest deadline first.",
        f"   Good luck, {student_name}! 🎯",
    ]

    return "\n".join(plan_lines)
