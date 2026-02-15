"""Cluster detection -- find coordinated wallet groups via graph analysis.

Wallets that trade together (same markets, same direction, tight time
windows) may be controlled by a single entity.  Detecting these clusters
is critical because:

  1. A cluster should be treated as *one* signal, not many.
  2. Coordinated buying can manipulate copy-traders into bad positions.

We build an undirected weighted graph where wallets are nodes and edge
weights encode three correlation types:

  * **Funding correlation** -- wallets funded from the same on-chain address.
  * **Temporal correlation** -- trades on the same market within a short
    time window (default 10 seconds).
  * **Portfolio similarity** -- Jaccard similarity of market positions
    above a threshold (0.7).

Connected components of ``CLUSTER_MIN_SIZE`` or more wallets are
extracted using BFS and persisted.

No external graph library is required -- the graph is a plain adjacency
dict ``{wallet: {neighbor: weight, ...}, ...}``.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timezone

import config
from storage.db import execute, query
from utils import to_epoch as _to_epoch
from utils.logger import get_logger

log = get_logger("scoring.cluster")

# Edge-weight thresholds.  Edges below these are pruned.
_TEMPORAL_EDGE_WEIGHT: float = 0.4
_PORTFOLIO_EDGE_WEIGHT: float = 0.3
_FUNDING_EDGE_WEIGHT: float = 0.6
_JACCARD_THRESHOLD: float = 0.7
_EDGE_PRUNE_THRESHOLD: float = 0.3  # minimum weight to keep an edge


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _add_edge(
    graph: dict[str, dict[str, float]],
    a: str,
    b: str,
    weight: float,
) -> None:
    """Add *weight* to the edge between *a* and *b* (undirected)."""
    if a == b:
        return
    graph.setdefault(a, {})
    graph.setdefault(b, {})
    graph[a][b] = graph[a].get(b, 0.0) + weight
    graph[b][a] = graph[b].get(a, 0.0) + weight


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def build_correlation_graph(
    trades: list[dict],
    funding: dict[str, str],
) -> dict[str, dict[str, float]]:
    """Build an undirected weighted correlation graph from trade data.

    Parameters
    ----------
    trades:
        List of trade dicts, each containing at minimum:
        ``wallet``, ``market_id``, ``side``, ``ts`` (ISO timestamp or
        float epoch).
    funding:
        Mapping of ``{wallet_address: funding_source_address}``.

    Returns
    -------
    dict[str, dict[str, float]]
        Adjacency dict.  ``graph[a][b]`` is the aggregate edge weight
        between wallets *a* and *b*.
    """
    graph: dict[str, dict[str, float]] = {}
    time_window = config.CLUSTER_TIME_WINDOW

    # ------------------------------------------------------------------
    # 1. Funding correlation
    # ------------------------------------------------------------------
    source_to_wallets: dict[str, list[str]] = defaultdict(list)
    for wallet, source in funding.items():
        if source:
            source_to_wallets[source].append(wallet)

    for _source, wallets in source_to_wallets.items():
        if len(wallets) < 2:
            continue
        for i in range(len(wallets)):
            for j in range(i + 1, len(wallets)):
                _add_edge(graph, wallets[i], wallets[j], _FUNDING_EDGE_WEIGHT)

    # ------------------------------------------------------------------
    # 2. Temporal correlation
    # ------------------------------------------------------------------
    # Group trades by (market_id, side) then check timestamps
    market_side_trades: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        key = (t["market_id"], t.get("side", "BUY"))
        market_side_trades[key].append(t)

    for _key, group in market_side_trades.items():
        # Sort by timestamp
        group.sort(key=lambda t: _to_epoch(t["ts"]))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ts_i = _to_epoch(group[i]["ts"])
                ts_j = _to_epoch(group[j]["ts"])
                if abs(ts_j - ts_i) <= time_window:
                    _add_edge(
                        graph,
                        group[i]["wallet"],
                        group[j]["wallet"],
                        _TEMPORAL_EDGE_WEIGHT,
                    )
                else:
                    # group is sorted, so all subsequent will be even further
                    break

    # ------------------------------------------------------------------
    # 3. Portfolio similarity
    # ------------------------------------------------------------------
    wallet_markets: dict[str, set[str]] = defaultdict(set)
    for t in trades:
        wallet_markets[t["wallet"]].add(t["market_id"])

    wallets_list = list(wallet_markets.keys())
    for i in range(len(wallets_list)):
        for j in range(i + 1, len(wallets_list)):
            sim = _jaccard(
                wallet_markets[wallets_list[i]],
                wallet_markets[wallets_list[j]],
            )
            if sim >= _JACCARD_THRESHOLD:
                _add_edge(
                    graph,
                    wallets_list[i],
                    wallets_list[j],
                    _PORTFOLIO_EDGE_WEIGHT * sim,
                )

    # Prune weak edges
    for wallet in list(graph.keys()):
        graph[wallet] = {
            nb: w
            for nb, w in graph[wallet].items()
            if w >= _EDGE_PRUNE_THRESHOLD
        }

    return graph


# ---------------------------------------------------------------------------
# Cluster extraction
# ---------------------------------------------------------------------------

def find_clusters(
    graph: dict[str, dict[str, float]],
    min_size: int | None = None,
) -> list[set[str]]:
    """Find connected components in *graph* with at least *min_size* nodes.

    Uses BFS over the adjacency dict.

    Parameters
    ----------
    graph:
        Adjacency dict ``{node: {neighbor: weight}}``.
    min_size:
        Minimum cluster size to return.  Defaults to
        ``config.CLUSTER_MIN_SIZE``.

    Returns
    -------
    list[set[str]]
        Each element is a set of wallet addresses forming a cluster.
    """
    if min_size is None:
        min_size = config.CLUSTER_MIN_SIZE

    visited: set[str] = set()
    clusters: list[set[str]] = []

    for node in graph:
        if node in visited:
            continue

        # BFS from this node
        component: set[str] = set()
        queue: deque[str] = deque([node])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in graph.get(current, {}):
                if neighbor not in visited:
                    queue.append(neighbor)

        if len(component) >= min_size:
            clusters.append(component)

    return clusters


# ---------------------------------------------------------------------------
# Async analysis routines (DuckDB-backed)
# ---------------------------------------------------------------------------

async def detect_temporal_clusters(
    market_id: str,
    window: float | None = None,
) -> list[set[str]]:
    """Find wallets that traded the same market within a tight time window.

    Queries recent trades from DuckDB, builds a temporal-only subgraph,
    and extracts clusters.

    Parameters
    ----------
    market_id:
        The Polymarket market to analyze.
    window:
        Time window in seconds.  Defaults to ``config.CLUSTER_TIME_WINDOW``.
    """
    if window is None:
        window = config.CLUSTER_TIME_WINDOW

    rows = query(
        "SELECT wallet, market_id, side, ts FROM trades "
        "WHERE market_id = ? ORDER BY ts",
        [market_id],
    )

    if not rows:
        return []

    trades = [
        {
            "wallet": r[0],
            "market_id": r[1],
            "side": r[2],
            "ts": r[3],
        }
        for r in rows
    ]

    # Build a graph using only temporal correlation
    graph: dict[str, dict[str, float]] = {}
    trades.sort(key=lambda t: _to_epoch(t["ts"]))
    for i in range(len(trades)):
        for j in range(i + 1, len(trades)):
            ts_i = _to_epoch(trades[i]["ts"])
            ts_j = _to_epoch(trades[j]["ts"])
            if abs(ts_j - ts_i) <= window:
                _add_edge(graph, trades[i]["wallet"], trades[j]["wallet"], _TEMPORAL_EDGE_WEIGHT)
            else:
                break

    return find_clusters(graph)


async def detect_portfolio_clusters(
    wallets: list[str],
) -> list[set[str]]:
    """Find wallets with highly similar portfolio compositions.

    Parameters
    ----------
    wallets:
        List of wallet addresses to compare.
    """
    if len(wallets) < 2:
        return []

    # Fetch positions for all wallets
    placeholders = ", ".join(["?"] * len(wallets))
    rows = query(
        f"SELECT wallet, market_id FROM positions WHERE wallet IN ({placeholders})",
        wallets,
    )

    wallet_markets: dict[str, set[str]] = defaultdict(set)
    for wallet, market_id in rows:
        wallet_markets[wallet].add(market_id)

    # Build portfolio-only subgraph
    graph: dict[str, dict[str, float]] = {}
    wallet_list = list(wallet_markets.keys())
    for i in range(len(wallet_list)):
        for j in range(i + 1, len(wallet_list)):
            sim = _jaccard(wallet_markets[wallet_list[i]], wallet_markets[wallet_list[j]])
            if sim >= _JACCARD_THRESHOLD:
                _add_edge(graph, wallet_list[i], wallet_list[j], _PORTFOLIO_EDGE_WEIGHT * sim)

    return find_clusters(graph)


async def run_cluster_analysis() -> list[dict]:
    """Run full cluster analysis across all watched wallets.

    Combines funding, temporal, and portfolio correlations.  Detected
    clusters are persisted to the DuckDB ``clusters`` table and the
    ``cluster_id`` field on each wallet is updated.

    Returns a list of cluster dicts with keys: id, wallets, correlation,
    confidence.
    """
    # Fetch all trades from the last 7 days
    rows = query(
        "SELECT wallet, market_id, side, ts FROM trades "
        "WHERE ts >= current_timestamp - INTERVAL 7 DAY "
        "ORDER BY ts",
    )
    trades = [
        {"wallet": r[0], "market_id": r[1], "side": r[2], "ts": r[3]}
        for r in rows
    ]

    # Fetch funding data
    funding_rows = query(
        "SELECT address, funding_type FROM wallets WHERE funding_type IS NOT NULL",
    )
    # For cluster purposes, we group wallets by funding_type + a heuristic.
    # True funding-source correlation requires on-chain data we may not have,
    # so we use funding_type as a proxy.
    funding: dict[str, str] = {r[0]: r[1] for r in funding_rows}

    if not trades:
        log.info("cluster_analysis_skipped", reason="no_recent_trades")
        return []

    graph = build_correlation_graph(trades, funding)
    clusters = find_clusters(graph)

    results: list[dict] = []
    for idx, cluster_wallets in enumerate(clusters):
        # Determine dominant correlation type by re-checking edges
        correlation_type = _infer_correlation_type(graph, cluster_wallets)

        # Average edge weight within the cluster as a confidence proxy
        confidence = _cluster_confidence(graph, cluster_wallets)

        cluster_record = {
            "id": idx,
            "wallets": sorted(cluster_wallets),
            "correlation": correlation_type,
            "confidence": round(confidence, 4),
        }
        results.append(cluster_record)

        # Persist to DuckDB
        try:
            execute(
                "INSERT OR REPLACE INTO clusters (id, wallets, correlation, confidence, discovered_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    idx,
                    json.dumps(sorted(cluster_wallets)),
                    correlation_type,
                    confidence,
                    datetime.now(timezone.utc),
                ],
            )
            # Tag individual wallets
            for wallet in cluster_wallets:
                execute(
                    "UPDATE wallets SET cluster_id = ? WHERE address = ?",
                    [idx, wallet],
                )
        except Exception:
            log.exception("cluster_persist_failed", cluster_id=idx)

    log.info("cluster_analysis_complete", clusters_found=len(results))
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _infer_correlation_type(
    graph: dict[str, dict[str, float]],
    cluster: set[str],
) -> str:
    """Heuristic: return the dominant correlation label for a cluster."""
    total_weight = 0.0
    count = 0
    for wallet in cluster:
        for neighbor, weight in graph.get(wallet, {}).items():
            if neighbor in cluster:
                total_weight += weight
                count += 1

    if count == 0:
        return "unknown"

    avg = total_weight / count
    # Higher average weights typically indicate funding-based correlation
    # because _FUNDING_EDGE_WEIGHT (0.6) > _TEMPORAL (0.4) > _PORTFOLIO (0.3)
    if avg >= _FUNDING_EDGE_WEIGHT:
        return "funding"
    elif avg >= _TEMPORAL_EDGE_WEIGHT:
        return "temporal"
    else:
        return "portfolio"


def _cluster_confidence(
    graph: dict[str, dict[str, float]],
    cluster: set[str],
) -> float:
    """Compute average intra-cluster edge weight as a confidence score."""
    total = 0.0
    edges = 0
    seen: set[tuple[str, str]] = set()
    for wallet in cluster:
        for neighbor, weight in graph.get(wallet, {}).items():
            if neighbor in cluster:
                pair = tuple(sorted((wallet, neighbor)))
                if pair not in seen:
                    seen.add(pair)
                    total += weight
                    edges += 1
    return total / edges if edges > 0 else 0.0
