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

        # Only upgrade(). downgrade() re-creates the *previous* form of a
        # widened constraint, and since it sits later in the file a plain
        # ast.walk over the module would let the rollback definition overwrite
        # the live one - silently comparing against the schema as it used to be.
        upgrade_fn = next(
            (
                stmt
                for stmt in tree.body
                if isinstance(stmt, ast.FunctionDef) and stmt.name == "upgrade"
            ),
            None,
        )
        assert upgrade_fn is not None, f"{path.name}: no upgrade()"

        for node in ast.walk(upgrade_fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

            # Two spellings, both in use:
            #   sa.CheckConstraint(<expr>, name=<name>)        - inline in create_table
            #   op.create_check_constraint(<name>, <table>, <expr>) - standalone
            if name == "CheckConstraint":
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
                expr_node = node.args[0] if node.args else None
            elif name == "create_check_constraint":
                if len(node.args) < 3:
                    continue
                first = node.args[0]
                constraint_name = (
                    first.value
                    if isinstance(first, ast.Constant) and isinstance(first.value, str)
                    else None
                )
                expr_node = node.args[2]
            else:
                continue

            if constraint_name is None or expr_node is None:
                continue
            expression = _resolve_str(expr_node, constants)
            assert expression is not None, (
                f"{path.name}: cannot resolve the expression of {constraint_name}. "
                f"Keep CHECK expressions to literals, '+' concatenation, or an "
                f"f-string over a module-level constant."
            )
            # Last definition wins. Widening a CHECK means a later revision
            # drops and re-adds it, so the newest file describes the live
            # database - which is exactly the workflow this module enforces.
            # Files are walked in sorted order, and revision ids are prefixed
            # with a zero-padded sequence, so sort order is apply order.
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


def test_contact_source_matches_check():
    _assert_matches(domain.ContactSource, "contacts_source_check")


def test_pgvector_is_not_a_day_one_dependency():
    """No revision before 0004 may require the vector extension.

    A phase-1 deployment runs `alembic upgrade 0003_interview` and must not
    need pgvector or an embedding provider to get there. Asserting on the
    *position* of 0004 would break every time a later revision lands; the
    invariant that actually matters is which revisions mention the extension.
    """
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "EXTENSION IF NOT EXISTS vector" not in text:
            continue
        assert path.name >= "0004", (
            f"{path.name} requires pgvector, but phase-1 deployments stop at "
            f"0003_interview and must not need it"
        )
