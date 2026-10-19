"""Consistency checks for the requirements, traceability, and design documents.

These guard the Stage 01 specification: requirement IDs must be unique and
complete, every requirement must be traced exactly once, and every ID or
relative link referenced anywhere in the docs must resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
REQUIREMENTS = DOCS / "requirements" / "requirements.md"
TRACEABILITY = DOCS / "requirements" / "traceability.md"
COMPONENTS = DOCS / "architecture" / "components.md"

CATEGORIES = ("FR", "NFR", "PLT", "SEC", "REL", "PRF", "CMP", "LIM", "UX", "PKG", "EXC")
ID_PATTERN = re.compile(r"\b(?:" + "|".join(CATEGORIES) + r")-\d{3}\b")
ROW_ID = re.compile(r"^\|\s*((?:" + "|".join(CATEGORIES) + r")-\d{3})\s*\|")
VERIFICATION_CODES = {"U", "I", "S", "F", "M", "R", "D"}
STAGE_CELL = re.compile(r"^(\d{2})(?:-(\d{2}))?$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def table_rows(path: Path) -> list[list[str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if ROW_ID.match(line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def requirement_ids() -> list[str]:
    return [row[0] for row in table_rows(REQUIREMENTS)]


def component_keys() -> set[str]:
    text = COMPONENTS.read_text(encoding="utf-8")
    index = text.split("## Component index", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z]+)` \|", index, flags=re.MULTILINE))


def github_anchor(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\- ]", "", slug)
    return slug.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found = set()
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            found.add(github_anchor(line.lstrip("#")))
    return found


def doc_files() -> list[Path]:
    return sorted([*DOCS.rglob("*.md"), ROOT / "README.md"])


def test_requirement_ids_are_unique_and_well_formed() -> None:
    ids = requirement_ids()
    assert len(ids) > 0
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert duplicates == []


def test_every_requirement_has_criterion_and_verification() -> None:
    for row in table_rows(REQUIREMENTS):
        assert len(row) == 4, f"{row[0]}: expected 4 columns, got {len(row)}"
        req_id, text, criterion, verification = row
        assert text and criterion, f"{req_id}: empty requirement or acceptance criterion"
        codes = {code.strip() for code in verification.split(",")}
        assert codes and codes <= VERIFICATION_CODES, f"{req_id}: bad verification {codes}"


def test_traceability_covers_every_requirement_exactly_once() -> None:
    traced = [row[0] for row in table_rows(TRACEABILITY)]
    duplicates = sorted({i for i in traced if traced.count(i) > 1})
    assert duplicates == []
    assert set(traced) == set(requirement_ids())


def test_traceability_rows_use_known_components_and_stages() -> None:
    keys = component_keys()
    assert {"backend", "storage", "recovery", "gui"} <= keys
    for row in table_rows(TRACEABILITY):
        assert len(row) == 8, f"{row[0]}: expected 8 columns, got {len(row)}"
        req_id, component, stage = row[0], row[1], row[2]
        assert component in keys, f"{req_id}: unknown component {component!r}"
        match = STAGE_CELL.match(stage)
        assert match, f"{req_id}: bad stage {stage!r}"
        numbers = [int(n) for n in match.groups() if n is not None]
        assert all(0 <= n <= 12 for n in numbers) and numbers == sorted(numbers), req_id
        assert all(cell for cell in row[3:]), f"{req_id}: empty cell (use '—')"


@pytest.mark.parametrize(
    "path",
    [*doc_files(), *sorted((ROOT / "scripts").glob("*.py"))],
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_referenced_requirement_ids_exist(path: Path) -> None:
    known = set(requirement_ids())
    referenced = set(ID_PATTERN.findall(path.read_text(encoding="utf-8")))
    assert sorted(referenced - known) == []


@pytest.mark.parametrize("path", doc_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(path: Path) -> None:
    broken = []
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if re.match(r"^[a-z]+:", target):
            continue  # external URL
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            broken.append(target)
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            broken.append(target)
    assert broken == []
