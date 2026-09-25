"""Builder wiring the collector together from configuration and environment.

Centralises construction so the ``collect`` CLI, the ``status`` CLI, the
scheduler loop and any future operator tooling share identical wiring.

Environment (all optional; CLI flags override):

* ``FNO_FRESH_OOS_ROOT`` -- immutable store root (default ``data/fresh_oos``);
* ``FNO_FRESH_OOS_DATASETS_DIR`` -- established ``datasets/`` pool (default ``datasets``);
* ``FNO_FRESH_OOS_MAX_DAYS_PER_RUN`` -- cap on dates per pass (default unlimited);
* ``FNO_FRESH_OOS_SCHEDULE_SECONDS`` -- scheduler interval (default 900).

The Upstox analytics/data token (``FNO_UPSTOX_ACCESS_TOKEN``) is read only by
the client at fetch time; ``build_collector`` never reads or prints it.
"""
from __future__ import annotations

import os
from pathlib import Path

from fno_ai_paper_trading.fresh_oos.client import UpstoxHistoricalDataClient
from fno_ai_paper_trading.fresh_oos.collector import FreshOosCollector, FreshOosCollectorConfig
from fno_ai_paper_trading.fresh_oos.store import (
    DEFAULT_DATASETS_DIR,
    DEFAULT_STORE_ROOT,
    FreshOosStore,
)


def resolve_store_root(override: str | None) -> Path:
    return Path(override or os.getenv("FNO_FRESH_OOS_ROOT", "") or DEFAULT_STORE_ROOT)


def resolve_datasets_dir(override: str | None) -> Path:
    return Path(override or os.getenv("FNO_FRESH_OOS_DATASETS_DIR", "") or DEFAULT_DATASETS_DIR)


def resolve_max_days(override: str | None) -> int:
    value = override or os.getenv("FNO_FRESH_OOS_MAX_DAYS_PER_RUN", "") or "0"
    try:
        return int(value)
    except ValueError:
        return 0


def build_collector(*, root: str | None = None, datasets_dir: str | None = None,
                    max_days: str | None = None, client=None,
                    ) -> FreshOosCollector:
    """Build a production collector wired to its defaults (no credentials read).

    A manifest already on disk (a previously accepted pool) is loaded, so a
    rebuilt collector continues from existing coverage -- restart-safe.
    ``client`` is optional and defaults to the real Upstox adapter (tests
    inject a fake).
    """
    root_path = resolve_store_root(root)
    datasets_path = resolve_datasets_dir(datasets_dir)
    config = FreshOosCollectorConfig(
        root=root_path,
        datasets_dir=datasets_path,
        max_days_per_run=resolve_max_days(max_days),
    )
    store = FreshOosStore(root_path, namespace=config.namespace)
    collector = FreshOosCollector(
        config=config,
        client=client if client is not None else UpstoxHistoricalDataClient(),
        store=store,
    )
    return collector