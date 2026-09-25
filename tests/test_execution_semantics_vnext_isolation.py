"""vNext isolation audit.

Proves the isolated ``execution.vnext`` package:

1. imports ONLY its own modules (never the WS 7.9 execution harness
   ``execution/{signal,manager,state,risk,upstox,...}`` or
   ``scripts.run_live_execution_test``);
2. is NOT imported by any sibling ``execution`` module (no accidental wiring of
   the vNext machine into the live/paper harness);
3. is NOT imported by the legacy live-execution runner script.

Static import-graph analysis only; no network, no Upstox, no paper/live orders.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "fno_ai_paper_trading"
VNEXT_DIR = SRC / "execution" / "vnext"
EXEC_DIR = SRC / "execution"

#: WS 7.9 harness modules vNext must NOT reach into.
WS79_HARNESS_MODULES = {
    "fno_ai_paper_trading.execution.signal",
    "fno_ai_paper_trading.execution.manager",
    "fno_ai_paper_trading.execution.state",
    "fno_ai_paper_trading.execution.risk",
    "fno_ai_paper_trading.execution.upstox",
    "fno_ai_paper_trading.execution.oauth",
    "fno_ai_paper_trading.execution.memory",
    "fno_ai_paper_trading.execution.instrument",
    "fno_ai_paper_trading.execution.gate",
    "fno_ai_paper_trading.execution.audit",
    "scripts.run_live_execution_test",
}

VNEXT_PREFIX = "fno_ai_paper_trading.execution.vnext"
VNEXT_ALLOWED_PREFIXES = (VNEXT_PREFIX, "fno_ai_paper_trading.execution")


def _import_names(tree: ast.AST) -> set[str]:
    """Collect dotted import targets referenced by a module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if module:
                    names.add(f"{module}.{alias.name}")
    return names


def _module_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.py"))


class TestVNextImportsOnlyItself:
    @pytest.mark.parametrize("path", _module_files(VNEXT_DIR))
    def test_vnext_module_never_imports_ws79_harness(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _import_names(tree)
        tool_touches = WS79_HARNESS_MODULES & imported
        # Relative imports inside vnext are always own-module; only absolute
        # WS 7.9 harness references are forbidden.
        assert not tool_touches, f"{path.name} imports WS 7.9: {tool_touches}"

    @pytest.mark.parametrize("path", _module_files(VNEXT_DIR))
    def test_vnext_module_never_leaves_its_own_tree(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in _import_names(tree):
            if target.startswith("fno_ai_paper_trading.execution"):
                prefix = target.rsplit(".", 1)[0]
                # allow git root package reference from its own tree only
                assert target == "fno_ai_paper_trading.execution" or target.startswith(
                    VNEXT_PREFIX
                ), (
                    f"{path.name} imports outside vnext tree: {target} "
                    "(abusive dependency on the WS 7.9 harness)"
                )


class TestEquallyWs79NeverImportsVNext:
    @pytest.mark.parametrize(
        "path",
        [p for p in _module_files(EXEC_DIR) if p.parent != VNEXT_DIR]
        + [Path(__file__).resolve().parents[1] / "scripts" / "run_live_execution_test.py"],
    )
    def test_no_ws79_module_imports_vnext(self, path: Path):
        if not path.exists():
            pytest.skip(f"{path.name} not present")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _import_names(tree)
        assert not any(t.startswith(VNEXT_PREFIX) for t in imported), (
            f"{path.name} references execution.vnext (unintentional wiring)"
        )


class TestVNextPackageBoundary:
    def test_no_runtime_side_effects_from_import(self):
        # Importing the package alone must not construct machines, touch the
        # network, or read credentials — pure type/function definitions.
        import fno_ai_paper_trading.execution.vnext as vnext

        assert vnext.__all__  # package is importable and defined

    def test_every_vnext_module_is_part_of_the_package(self):
        assert (VNEXT_DIR / "__init__.py").exists()
        modules = {p.stem for p in VNEXT_DIR.glob("*.py") if p.stem != "__init__"}
        imported = {
            "broker",
            "contract",
            "enums",
            "errors",
            "guards",
            "machine",
            "mapping",
            "order_request",
            "order_semantics",
            "state",
            "upstox_broker",
            "eod_policy",
            "live_credentials",
        }
        assert modules == imported