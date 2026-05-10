"""Path-level ownership rules from a YAML config file.

DESIGN.md §7.2 lays out a ``/srv/vaultmcp/config.yaml`` that maps glob
patterns to producer/consumer/policy rules. v0.3 implements the most
common subset:

- ``producer=<app>`` — only that app is allowed to write.
- ``write=human-only`` — every MCP write is rejected. Humans edit
  through the dashboard or directly in the DB.

Unmatched paths fall through to the default-allow behaviour. Consumer
rules and ``allowed_sections`` (where a consumer may write under a
specific markdown heading) are recognised by the loader but not yet
enforced — they raise a clear error so an operator who configures them
isn't silently ignored.

Example config:

    ownership:
      apps/webstore/**: producer=webstore
      apps/admin/**: producer=admin
      integrations/01-inventory--*: producer=inventory
      decisions/**: write=human-only
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from .errors import ForbiddenError, ToolError

CONFIG_PATH_ENV: Final[str] = "VAULTMCP_CONFIG_FILE"
DEFAULT_CONFIG_PATH: Final[Path] = Path("/srv/vaultmcp/config.yaml")


@dataclass(frozen=True)
class Rule:
    """One row from the ``ownership:`` map.

    ``pattern`` is a glob (``*``, ``**``, ``?``); ``fnmatch`` semantics.
    Exactly one of ``producer`` or ``human_only`` is set.
    """

    pattern: str
    producer: str | None = None
    human_only: bool = False


class OwnershipRules:
    """In-memory rule set loaded from ``config.yaml``."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        # Preserve declaration order; first match wins. This matches the
        # YAML file's natural top-down reading.
        self._rules: tuple[Rule, ...] = tuple(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def matching_rule(self, path: str) -> Rule | None:
        """Return the first rule whose pattern matches ``path``, or None."""
        for rule in self._rules:
            if _glob_match(rule.pattern, path):
                return rule
        return None

    def check_write(self, *, path: str, app: str) -> None:
        """Raise :class:`ForbiddenError` if ``app`` may not write to ``path``.

        No-op when no rule matches (default allow). The auth dependency
        has already verified the caller's app is one of its registered
        apps; this layer adds the path-level constraint on top.
        """
        rule = self.matching_rule(path)
        if rule is None:
            return
        if rule.human_only:
            raise ForbiddenError(
                f"Path {path!r} is configured as human-only "
                f"(rule: {rule.pattern!r}); MCP writes are not permitted"
            )
        if rule.producer is not None and rule.producer != app:
            raise ForbiddenError(
                f"Path {path!r} is owned by app {rule.producer!r} "
                f"(rule: {rule.pattern!r}); app {app!r} cannot write here"
            )


# =============================================================
# Loaders
# =============================================================


class OwnershipConfigError(ToolError):
    """Raised when config.yaml exists but is malformed."""

    status: Final[int] = 500


def load_rules(path: Path | None = None) -> OwnershipRules:
    """Read and parse the ownership config; missing file → empty rule set.

    Resolution order: explicit ``path`` arg, then ``$VAULTMCP_CONFIG_FILE``,
    then the default ``/srv/vaultmcp/config.yaml``.
    """
    if path is None:
        env = os.environ.get(CONFIG_PATH_ENV)
        path = Path(env) if env else DEFAULT_CONFIG_PATH
    if not path.is_file():
        return OwnershipRules(())
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OwnershipConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise OwnershipConfigError(
            f"{path} must be a YAML mapping at the top level"
        )
    section = raw.get("ownership", {}) or {}
    if not isinstance(section, dict):
        raise OwnershipConfigError(
            f"{path}: 'ownership' must be a mapping of pattern -> rule string"
        )
    return OwnershipRules(_parse_rule(p, expr) for p, expr in section.items())


def _parse_rule(pattern: str, expr: object) -> Rule:
    if not isinstance(expr, str):
        raise OwnershipConfigError(
            f"Rule for {pattern!r}: expected a string, got {type(expr).__name__}"
        )
    text = expr.strip()
    if text == "write=human-only":
        return Rule(pattern=pattern, human_only=True)
    if text.startswith("producer="):
        producer = text[len("producer=") :].strip()
        if not producer:
            raise OwnershipConfigError(
                f"Rule for {pattern!r}: producer name is empty"
            )
        return Rule(pattern=pattern, producer=producer)
    # Recognise but reject (rather than silently accept) consumer/policy
    # rules so operators see why they're not taking effect yet.
    if text.startswith("consumer=") or text.startswith("policy="):
        raise OwnershipConfigError(
            f"Rule for {pattern!r}: {text!r} is not yet supported "
            "(consumer + allowed_sections land in a follow-up)"
        )
    raise OwnershipConfigError(
        f"Rule for {pattern!r}: unrecognised expression {text!r}; "
        "use 'producer=<app>' or 'write=human-only'"
    )


# =============================================================
# Glob matching
# =============================================================


def _glob_match(pattern: str, path: str) -> bool:
    """Match ``path`` against ``pattern`` with shell-glob semantics.

    Note that :mod:`fnmatch` already treats ``*`` as greedy across
    ``/``, so ``apps/webstore/**`` and ``apps/webstore/*`` match the
    same set in this engine. We accept the ``**`` form because that's
    what DESIGN.md uses; collapsing it to ``*`` produces the desired
    "any depth" behaviour.
    """
    return fnmatch.fnmatchcase(path, pattern.replace("**", "*"))
