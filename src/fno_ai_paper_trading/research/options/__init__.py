"""Options research stack (Phase 5).

Deterministic, provider-neutral, replay-safe data-quality foundation:

* :class:`~fno_ai_paper_trading.research.options.models.OptionChainSnapshot` /
  :class:`OptionQuote` / :class:`Greeks` — frozen domain models;
* :class:`~fno_ai_paper_trading.research.options.validation.ChainValidator` —
  strict ``VALID / INVALID / UNAVAILABLE`` classification (no fabrication);
* :func:`normalize_chain` — deterministic raw-payload normalization and
  canonical round-trip helpers for replay datasets and fingerprints;
* :class:`OptionChainProvider` / :class:`StaticChainProvider` — read-only data
  boundary.

The stack never places orders, never touches credentials and never substitutes
the underlying spot for an option value.
"""
from fno_ai_paper_trading.research.options.errors import OptionDataError, OptionNormalizationError
from fno_ai_paper_trading.research.options.models import (
    Greeks,
    OptionChainSnapshot,
    OptionQuote,
    OptionSide,
)
from fno_ai_paper_trading.research.options.normalization import (
    chain_fingerprint,
    normalize_chain,
    normalize_quote,
    quote_from_dict,
    quote_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)
from fno_ai_paper_trading.research.options.protocol import (
    OptionChainDataUnavailableError,
    OptionChainProvider,
    StaticChainProvider,
)
from fno_ai_paper_trading.research.options.validation import (
    ChainValidation,
    ChainValidator,
    DataState,
    FieldReport,
    QuoteValidation,
)

__all__ = [
    "ChainValidation",
    "ChainValidator",
    "DataState",
    "FieldReport",
    "Greeks",
    "OptionChainDataUnavailableError",
    "OptionChainProvider",
    "OptionChainSnapshot",
    "OptionDataError",
    "OptionNormalizationError",
    "OptionQuote",
    "OptionSide",
    "QuoteValidation",
    "StaticChainProvider",
    "chain_fingerprint",
    "normalize_chain",
    "normalize_quote",
    "quote_from_dict",
    "quote_to_dict",
    "snapshot_from_dict",
    "snapshot_to_dict",
]