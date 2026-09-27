"""WP-18 — credential-hygiene scan guards CLI-flag and hardcoded-secret leaks."""

from __future__ import annotations

from pathlib import Path

from scripts.secret_scan import REPO_ROOT, SCAN_DIRS, scan_root


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_flags_credential_cli_flag(tmp_path):
    _write(
        tmp_path,
        Path("scripts") / "leaky_cli.py",
        "parser.add_argument(\"--token\", default=\"\")\n"
        "parser.add_argument(\"--client-secret\", default=\"\")\n"
        "main()\n",
    )
    violations, scanned, _ = scan_root(tmp_path)
    assert len(scanned) == 1
    assert any("--token" in v and "leaky_cli.py" in v for v in violations)
    assert any("--client-secret" in v for v in violations)


def test_scan_flags_hardcoded_secret_value(tmp_path):
    _write(
        tmp_path,
        Path("src") / "pkg" / "api.py",
        "client = Client()\n" 'client_secret = "super-secret-live-key"\n' "run()\n",
    )
    violations, _, _ = scan_root(tmp_path)
    assert any("hardcoded credential value" in v for v in violations)


def test_scan_flags_dead_credential_arg(tmp_path):
    _write(
        tmp_path,
        Path("scripts") / "runner.py",
        "def main():\n" "    token = args.token or os.getenv('A', '')\n",
    )
    violations, _, _ = scan_root(tmp_path)
    assert any("args.token" in v for v in violations)


def test_scan_allows_env_only_token_resolution(tmp_path):
    _write(
        tmp_path,
        Path("scripts") / "ok.py",
        "token = os.getenv(\"FNO_UPSTOX_ACCESS_TOKEN\", \"\")\n"
        "provider = UpstoxHistoricalDataProvider(access_token=token)\n",
    )
    violations, _, _ = scan_root(tmp_path)
    assert violations == []


def test_scan_allows_empty_default_secret_fields(tmp_path):
    _write(
        tmp_path,
        Path("src") / "settings.py",
        "class Settings:\n    client_secret: str = \"\"\n    access_token: str = \"\"\n",
    )
    violations, _, _ = scan_root(tmp_path)
    assert violations == []


def test_live_repo_source_is_clean():
    """The real repo must currently pass the credential scan (WP-18 gate)."""
    violations, scanned, _ = scan_root(REPO_ROOT)
    assert violations == []
    assert any(REPO_ROOT.joinpath(dirname) for dirname in SCAN_DIRS)