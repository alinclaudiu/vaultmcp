"""Unit tests for path-level ownership rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultmcp.master.errors import ForbiddenError
from vaultmcp.master.ownership import (
    OwnershipConfigError,
    OwnershipRules,
    Rule,
    load_rules,
)

# ---------- glob matching ----------


def test_double_star_matches_any_depth() -> None:
    rules = OwnershipRules([Rule(pattern="apps/webstore/**", producer="webstore")])
    assert rules.matching_rule("apps/webstore/README.md") is not None
    assert rules.matching_rule("apps/webstore/sub/dir/page.md") is not None
    assert rules.matching_rule("apps/admin/README.md") is None


def test_single_star_segment_glob() -> None:
    rules = OwnershipRules([Rule(pattern="integrations/01-*", producer="inventory")])
    assert rules.matching_rule("integrations/01-inventory--pos--orders.md")
    assert rules.matching_rule("integrations/02-foo") is None


def test_first_match_wins() -> None:
    rules = OwnershipRules(
        [
            Rule(pattern="decisions/0001.md", human_only=True),
            Rule(pattern="decisions/**", producer="some-app"),
        ]
    )
    rule = rules.matching_rule("decisions/0001.md")
    assert rule is not None and rule.human_only


# ---------- enforcement ----------


def test_check_write_allows_when_no_rule_matches() -> None:
    rules = OwnershipRules([Rule(pattern="apps/admin/**", producer="admin")])
    rules.check_write(path="random/path.md", app="anyone")  # no raise


def test_check_write_allows_matching_producer() -> None:
    rules = OwnershipRules([Rule(pattern="apps/webstore/**", producer="webstore")])
    rules.check_write(path="apps/webstore/notes.md", app="webstore")  # no raise


def test_check_write_blocks_wrong_producer() -> None:
    rules = OwnershipRules([Rule(pattern="apps/webstore/**", producer="webstore")])
    with pytest.raises(ForbiddenError, match="webstore"):
        rules.check_write(path="apps/webstore/notes.md", app="admin")


def test_check_write_blocks_human_only_for_anyone() -> None:
    rules = OwnershipRules([Rule(pattern="decisions/**", human_only=True)])
    with pytest.raises(ForbiddenError, match="human-only"):
        rules.check_write(path="decisions/0001.md", app="any-app")


# ---------- YAML loader ----------


def test_load_rules_missing_file_returns_empty(tmp_path: Path) -> None:
    rules = load_rules(tmp_path / "no-such-file.yaml")
    assert len(rules) == 0


def test_load_rules_parses_producer_and_human_only(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ownership:\n"
        "  apps/webstore/**: producer=webstore\n"
        "  decisions/**: write=human-only\n",
        encoding="utf-8",
    )
    rules = load_rules(cfg)
    assert len(rules) == 2
    rules.check_write(path="apps/webstore/x.md", app="webstore")
    with pytest.raises(ForbiddenError):
        rules.check_write(path="apps/webstore/x.md", app="admin")
    with pytest.raises(ForbiddenError):
        rules.check_write(path="decisions/0001.md", app="webstore")


def test_load_rules_rejects_unrecognised_expression(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ownership:\n  apps/foo/**: weird=value\n",
        encoding="utf-8",
    )
    with pytest.raises(OwnershipConfigError, match="unrecognised"):
        load_rules(cfg)


def test_load_rules_rejects_consumer_until_supported(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ownership:\n  integrations/01-*: consumer=pos\n",
        encoding="utf-8",
    )
    with pytest.raises(OwnershipConfigError, match="not yet supported"):
        load_rules(cfg)


def test_load_rules_rejects_malformed_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(":\nnot: [valid", encoding="utf-8")
    with pytest.raises(OwnershipConfigError, match="not valid YAML"):
        load_rules(cfg)


def test_load_rules_skips_when_top_level_not_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(OwnershipConfigError, match="mapping"):
        load_rules(cfg)
