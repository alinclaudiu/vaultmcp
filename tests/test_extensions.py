"""Unit tests for the extensions DDL helpers."""

from __future__ import annotations

import pytest

from vaultmcp.master.errors import ValidationFailed
from vaultmcp.master.extensions import (
    assert_query_is_readonly,
    assert_valid_extension_name,
    build_create_table_sql,
    column_spec_to_sql,
    default_schema_prefix,
)
from vaultmcp.shared.types import ExtColumnSpec


def _col(**kwargs: object) -> ExtColumnSpec:
    return ExtColumnSpec.model_validate(kwargs)


# ---------- default_schema_prefix ----------


def test_default_schema_prefix_is_ext_underscore_name_underscore() -> None:
    assert default_schema_prefix("crm") == "ext_crm_"
    assert default_schema_prefix("incident_tracker") == "ext_incident_tracker_"


# ---------- name validation ----------


def test_assert_valid_extension_name_accepts_lowercase_alphanum() -> None:
    assert_valid_extension_name("crm")
    assert_valid_extension_name("incident_tracker")
    assert_valid_extension_name("a1")


@pytest.mark.parametrize(
    "bad", ["CRM", "1abc", "with-dash", "with space", "", "drop;table"]
)
def test_assert_valid_extension_name_rejects_bad_inputs(bad: str) -> None:
    with pytest.raises(ValidationFailed, match="match"):
        assert_valid_extension_name(bad)


# ---------- column spec ----------


def test_column_spec_basic() -> None:
    sql = column_spec_to_sql(_col(name="id", type="uuid", primary_key=True))
    assert sql == "id uuid PRIMARY KEY"


def test_column_spec_not_null_implied_by_primary_key() -> None:
    # primary_key=True alone — we don't emit NOT NULL because PK already implies it.
    sql = column_spec_to_sql(
        _col(name="id", type="uuid", primary_key=True, not_null=True)
    )
    assert "NOT NULL" not in sql
    assert "PRIMARY KEY" in sql


def test_column_spec_default_expression_is_passed_through() -> None:
    sql = column_spec_to_sql(
        _col(name="created_at", type="timestamptz", not_null=True, default="NOW()")
    )
    assert sql == "created_at timestamptz NOT NULL DEFAULT NOW()"


def test_column_spec_references_well_formed() -> None:
    sql = column_spec_to_sql(
        _col(name="contact_id", type="uuid", references="ext_crm_contacts(id)")
    )
    assert sql == "contact_id uuid REFERENCES ext_crm_contacts(id)"


def test_column_spec_references_malformed_rejected() -> None:
    with pytest.raises(ValidationFailed, match="references"):
        column_spec_to_sql(
            _col(name="x", type="uuid", references="DROP TABLE pages; --")
        )


def test_column_spec_unknown_type_rejected() -> None:
    with pytest.raises(ValidationFailed, match="not in the allowlist"):
        column_spec_to_sql(_col(name="x", type="not_a_real_type"))


# ---------- build_create_table_sql ----------


def test_build_create_table_basic() -> None:
    sql, full = build_create_table_sql(
        schema_prefix="ext_crm_",
        table_name="contacts",
        columns=[
            _col(name="id", type="uuid", primary_key=True),
            _col(name="name", type="text", not_null=True),
            _col(name="email", type="text"),
        ],
    )
    assert full == "ext_crm_contacts"
    assert "CREATE TABLE IF NOT EXISTS ext_crm_contacts" in sql
    assert "id uuid PRIMARY KEY" in sql
    assert "name text NOT NULL" in sql
    assert "email text" in sql


def test_build_create_table_rejects_invalid_table_name() -> None:
    with pytest.raises(ValidationFailed, match="Table name"):
        build_create_table_sql(
            schema_prefix="ext_crm_",
            table_name="DROP TABLE pages",
            columns=[_col(name="id", type="uuid", primary_key=True)],
        )


def test_build_create_table_requires_at_least_one_column() -> None:
    with pytest.raises(ValidationFailed, match="At least one column"):
        build_create_table_sql(
            schema_prefix="ext_crm_", table_name="empty", columns=[]
        )


def test_build_create_table_rejects_duplicate_column_names() -> None:
    with pytest.raises(ValidationFailed, match="Duplicate column"):
        build_create_table_sql(
            schema_prefix="ext_crm_",
            table_name="contacts",
            columns=[
                _col(name="x", type="uuid", primary_key=True),
                _col(name="x", type="text"),
            ],
        )


def test_build_create_table_rejects_multiple_primary_keys() -> None:
    with pytest.raises(ValidationFailed, match="primary_key"):
        build_create_table_sql(
            schema_prefix="ext_crm_",
            table_name="contacts",
            columns=[
                _col(name="a", type="uuid", primary_key=True),
                _col(name="b", type="uuid", primary_key=True),
            ],
        )


# ---------- query read-only check ----------


def test_assert_query_is_readonly_passes_select() -> None:
    assert_query_is_readonly("SELECT * FROM ext_crm_contacts WHERE name LIKE $1")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO ext_crm_contacts (name) VALUES ('x')",
        "UPDATE ext_crm_contacts SET name='x'",
        "DELETE FROM ext_crm_contacts",
        "DROP TABLE ext_crm_contacts",
        "ALTER TABLE ext_crm_contacts ADD COLUMN x text",
        "TRUNCATE ext_crm_contacts",
        "GRANT SELECT ON pages TO public",
        "SELECT * FROM ext_crm_contacts; DROP TABLE pages; --",
    ],
)
def test_assert_query_is_readonly_rejects_mutations(sql: str) -> None:
    with pytest.raises(ValidationFailed, match="read-only"):
        assert_query_is_readonly(sql)
