"""Guard against drift between app.domain enums and the CHECK constraints.

Why this test exists: a migration must not import from application code (it
would stop describing the database that was actually applied), so the same
string set is written down twice. Two copies drift. This test is the thing
that notices.

It parses the migration files as text rather than importing them - importing
would need alembic and sqlalchemy installed, and this check has to keep
working in a bare environment.

When it fails, you have a decision to make, and the test deliberately does not
make it for you:

  * added an enum value  -> write a NEW migration that drops and re-adds the
                            CHECK constraint, then add the value to the list
                            below. Never edit an applied migration.
  * removed a value      -> same, plus a data migration for existing rows.
  * typo                 -> fix whichever side is wrong.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app import domain

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _resolve_str(node: ast.AST, constants: dict[str, str]) -> str | None:
    """Best-effort evaluation of a string expression in a migration.

    Covers the three forms these constraints are written in: a plain (or
    implicitly concatenated) literal, an explicit ``+`` concatenation, and an
    f-string interpolating a module-level constant. Anything else returns None
    and the caller reports it, rather than guessing.

    Parsing with ast instead of regex matters more than it looks: a regex
    scanning backwards from a constraint name happily matches the IN list of
    the *previous* constraint, which compares the wrong vocabulary and still
    looks like a working test.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_str(node.left, constants)
        right = _resolve_str(node.right, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _resolve_str(value.value, constants)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _check_expressions() -> dict[str, str]:
    """Map every named CHECK constraint to its resolved SQL expression."""
    assert VERSIONS.is_dir(), f"missing migrations directory: {VERSIONS}"
    found: dict[str, str] = {}

    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        constants: dict[str, str] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if isinstance(target, ast.Name):
                    value = _resolve_str(stmt.value, {})
                    if value is not None:
                        constants[target.id] = value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "CheckConstraint":
                continue
            constraint_name = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ),
                None,
            )
            if constraint_name is None or not node.args:
                continue
            expression = _resolve_str(node.args[0], constants)
            assert expression is not None, (
                f"{path.name}: cannot resolve the expression of {constraint_name}. "
                f"Keep CHECK expressions to literals, '+' concatenation, or an "
                f"f-string over a module-level constant."
            )
            assert constraint_name not in found, (
                f"two CHECK constraints are both named {constraint_name}; "
                f"constraint names must be unique across migrations"
            )
            found[constraint_name] = expression

    return found


def _assert_matches(enum_cls, constraint_name: str) -> None:
    expressions = _check_expressions()
    expression = expressions.get(constraint_name)
    assert expression is not None, (
        f"no CHECK constraint named {constraint_name}; "
        f"known: {sorted(expressions)}"
    )
    in_lists = re.findall(r"IN \(([^)]*)\)", expression, re.DOTALL)
    assert in_lists, f"{constraint_name}: expression has no IN (...) list: {expression!r}"
    actual = set(re.findall(r"'([^']+)'", in_lists[-1]))

    expected = set(domain.values(enum_cls))
    assert actual == expected, (
        f"{constraint_name} drifted from {enum_cls.__name__}: "
        f"only in DB={sorted(actual - expected)}, only in code={sorted(expected - actual)}"
    )


def test_source_tier_matches_check():
    _assert_matches(domain.SourceTier, "sources_tier_check")


def test_health_status_matches_check():
    _assert_matches(domain.HealthStatus, "sources_status_check")


def test_contract_type_matches_check():
    _assert_matches(domain.ContractType, "jobs_contract_type_check")


def test_document_kind_matches_check():
    _assert_matches(domain.DocumentKind, "documents_kind_check")


def test_verdict_matches_check():
    _assert_matches(domain.Verdict, "evaluations_verdict_check")


def test_job_mark_matches_check():
    _assert_matches(domain.JobMark, "job_user_state_mark_check")


def test_application_status_matches_check():
    _assert_matches(domain.ApplicationStatus, "applications_status_check")


def test_event_origin_matches_check():
    _assert_matches(domain.EventOrigin, "application_events_origin_check")


def test_artifact_kind_matches_check():
    _assert_matches(domain.ArtifactKind, "artifacts_kind_check")


def test_agent_name_matches_check():
    _assert_matches(domain.AgentName, "agent_configs_agent_check")


def test_chunk_source_kind_matches_check():
    _assert_matches(domain.ChunkSourceKind, "chunks_source_kind_check")


# --- migration hygiene -----------------------------------------------------


def test_revision_chain_is_linear_and_complete():
    """One head, no gaps: a branched chain fails to upgrade with no clear error."""
    revisions: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision = "([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision = (?:"([^"]+)"|None)', text, re.MULTILINE)
        assert rev, f"{path.name}: no revision id"
        assert down, f"{path.name}: no down_revision"
        revisions[rev.group(1)] = down.group(1)

    roots = [rev for rev, down in revisions.items() if down is None]
    assert len(roots) == 1, f"expected exactly one root revision, found {roots}"

    parents = [down for down in revisions.values() if down is not None]
    assert len(parents) == len(set(parents)), "a revision is claimed by two children (branch)"

    heads = set(revisions) - set(parents)
    assert len(heads) == 1, f"expected exactly one head, found {sorted(heads)}"


def test_every_migration_has_a_downgrade():
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "def downgrade()" in text, f"{path.name}: no downgrade()"


def test_migrations_never_import_app_code():
    """The rule this whole test module exists to protect."""
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "from app" not in text and "import app" not in text, (
            f"{path.name} imports application code. A migration is a historical "
            f"record; reading live enums would make it rewrite itself."
        )


def test_retrieval_is_the_last_revision():
    """pgvector must not be a day-one dependency.

    Phase 1 deploys stop at 0003_interview; 0004 is opt-in.
    """
    text = (VERSIONS / "0004_retrieval.py").read_text(encoding="utf-8")
    assert 'down_revision = "0003_interview"' in text
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text
