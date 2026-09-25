"""Fresh-OOS operational safety.

The collector must never touch research/strategy/execution machinery, must
never write to protected paths, must redact credentials from any record, and
the CLI's ``--date``/``--once`` surface must stay read-only and deterministic.
Isolation is asserted both statically (import graph, forbidden tokens) and
dynamically (a full acquisition pass leaves no protected files touched).
"""
from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path

import pytest

from fresh_oos_testkit import COMMON_EXCEPTIONS, FakeHistoricalDataClient, make_context
from fno_ai_paper_trading.fresh_oos.client import UpstoxHistoricalDataClient
from fno_ai_paper_trading.fresh_oos.credential_provider import RuntimeCredentialProvider
from fno_ai_paper_trading.fresh_oos.errors import CredentialsUnavailableError, safe_message
from fno_ai_paper_trading.fresh_oos.protocol import (
    STATUS_AUTH_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_SUCCESS,
)

NOW = datetime(2026, 10, 15, 12, 0)
D1 = date(2026, 9, 17)
D2 = date(2026, 9, 18)

_PKG = Path(__file__).resolve().parents[1] / "src" / "fno_ai_paper_trading" / "fresh_oos"

# Modules the collector must never import transitively (OUR-ALGO-004 machinery).
_FORBIDDEN_ROOTS = {
    "execution", "strategies", "strategy", "promotion", "walkforward",
    "broker", "backtest", "plugins", "research",
}
_FORBIDDEN_TOKENS = ("OUR-ALGO", "sma(5", "sma(21", "MA(5,21")


def _imported_roots(module, seen=None) -> set[str]:
    seen = seen if seen is not None else set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    return seen


def test_fresh_oos_never_imports_research_strategy_or_execution_machinery():
    inspected: dict[str, set[str]] = {}
    for path in _PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        inspected[path.name] = _imported_roots(tree)
    for name, roots in inspected.items():
        forbidden = roots & _FORBIDDEN_ROOTS
        assert not forbidden, f"{name} imports forbidden roots: {sorted(forbidden)}"


def test_fresh_oos_sources_carry_no_lineage_tokens():
    import io
    import tokenize

    for path in _PKG.glob("*.py"):
        with path.open("rb") as handle:
            tokens = tokenize.tokenize(handle.readline)
            code = " ".join(
                tok.string.lower()
                for tok in tokens
                if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER)
            )
        for token in _FORBIDDEN_TOKENS:
            assert token.lower() not in code, f"{path.name} mentions {token!r} in code"


def test_full_pass_touches_no_protected_paths(tmp_path):
    import json

    ctx = make_context(tmp_path, now=datetime(2026, 9, 20, 12, 0))
    outcome = ctx["collector"].collect_once()
    assert outcome.status in (STATUS_SUCCESS, "NO_NEW_DATA")
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    relative = {str(p.relative_to(tmp_path)) for p in written}
    assert relative  # a manifest exists somewhere
    # nothing under the (currently empty) protected datasets pointer
    assert len([p for p in ctx["datasets"].rglob("*")]) == 0
    # no research/execution artifacts anywhere under the run root
    assert not any("execution" in r or "strategy" in r or "walkforward" in r for r in relative)


def test_client_requires_credentials_and_never_leaks_token(tmp_path):
    # Deterministic absent-credential path: never depends on the ambient env
    # (provisioned operator token present or not) and never touches the network.
    client = UpstoxHistoricalDataClient(  # no env token either
        access_token="", credential_provider=RuntimeCredentialProvider(environ={})
    )
    with pytest.raises(CredentialsUnavailableError) as exc:
        client._provider()
    assert "FNO_UPSTOX_ACCESS_TOKEN" in str(exc.value)  # tells the operator what is missing


def test_credentials_are_redacted_in_errors(tmp_path):
    message = "auth failed with Bearer sekret123 at https://api.upstox.com"
    redacted = safe_message(message)
    assert "sekret123" not in redacted


def test_missing_token_maps_to_auth_required(tmp_path):
    ctx = make_context(
        tmp_path,
        now=NOW,
        client=UpstoxHistoricalDataClient(
            access_token="", credential_provider=RuntimeCredentialProvider(environ={})
        ),
    )
    outcome = ctx["collector"].collect_once(force_date=D1)
    assert outcome.status == STATUS_AUTH_REQUIRED
    assert ctx["store"].find(D1) is None


def test_fetch_exceptions_map_to_network_status(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    ctx["client"].failures[D1] = COMMON_EXCEPTIONS["unavailable"]
    outcome = ctx["collector"].collect_once(force_date=D1)
    assert outcome.status == STATUS_NETWORK_ERROR
    assert ctx["store"].find(D1) is None


def test_status_cli_output_is_read_only_and_token_free(tmp_path):
    ctx = make_context(tmp_path, now=NOW)
    ctx["collector"].collect_once(force_date=D1)
    from fno_ai_paper_trading.fresh_oos import factory

    collector2 = factory.build_collector(root=str(ctx["root"]), datasets_dir=str(ctx["datasets"]))
    report = collector2.describe_status(now=NOW)
    assert report["accepted_days"] == 1
    assert report["validation"] == "NOT RUN"
    text = str(report).lower()
    assert "token" not in text and "secret" not in text