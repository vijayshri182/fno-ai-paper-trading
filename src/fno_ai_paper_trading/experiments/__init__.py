"""Isolated experiments.

Nothing here touches the frozen MA(5,21) baseline, the paper-track store
under ``data/paper_trading``, protected out-of-sample data or any live
broker/scheduler/credentials pathway. Each experiment keeps its own
namespace, store and reports.
"""