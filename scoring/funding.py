"""Funding source analysis -- classify wallet funding origins.

Since we rely on the Polymarket Data API rather than direct on-chain
queries, true funding-source analysis (tracing ETH/USDC inflows back
to a CEX hot wallet, mixer contract, or bridge) is **limited**.

What we *can* do:
  - Match known addresses from a curated set (CEX hot wallets, mixer
    contracts, bridge contracts).
  - Apply behavioural heuristics: a wallet whose first trade is
    unusually large, or that suddenly appears with high activity, is
    flagged as potentially funded through a privacy-preserving path.

If the project later integrates an on-chain indexer (e.g. Dune,
Transpose, or a direct RPC node), the ``classify_funding`` function
should be extended to inspect actual transaction histories.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import config
from storage.db import execute, query
from utils import to_epoch as _to_epoch
from utils.logger import get_logger

log = get_logger("scoring.funding")


# ---------------------------------------------------------------------------
# Known address patterns (curated, non-exhaustive)
# ---------------------------------------------------------------------------

# Centralized exchange hot wallets (Ethereum mainnet, Polygon).
# In production these would be loaded from a maintained registry;
# below are representative examples.
_KNOWN_CEX_ADDRESSES: set[str] = {
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 16
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",  # Binance 17
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43",  # Coinbase 10
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",  # Coinbase 11
    "0x503828976d22510aad0201ac7ec88293211d23da",  # Coinbase 2
    "0xfbb1b73c4f0bda4f67dca266ce6ef42f520fbb98",  # Bittrex
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",  # Kraken 13
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0",  # Kraken 4
}

# Privacy mixer contracts
_KNOWN_MIXER_ADDRESSES: set[str] = {
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",  # Tornado Cash Router
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",  # Tornado Cash Proxy
    "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc",  # Tornado Cash 0.1 ETH
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",  # Tornado Cash 1 ETH
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",  # Tornado Cash 10 ETH
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # Tornado Cash 100 ETH
}

# Bridge contracts (L1/L2 and cross-chain)
_KNOWN_BRIDGE_ADDRESSES: set[str] = {
    "0x40ec5b33f54e0e8a33a975908c5ba1c14e5bbbdf",  # Polygon ERC20 Bridge
    "0xa0c68c638235ee32657e8f720a23cec1bfc6c9a8",  # Polygon Plasma Bridge
    "0x8484ef722627bf18ca5ae6bcf031c23e6e922b30",  # Hop Protocol
    "0x3666f603cc164936c1b87e207f36beba4ac5f18a",  # Hop USDC Bridge
    "0x99c9fc46f92e8a1c0dec1b1747d010903e884be1",  # Optimism Gateway
    "0x3154cf16ccdb4c6d922629664174b904d80f2c35",  # Base Bridge
    "0x49048044d57e1c92a77f79988d21fa8faf74e97e",  # Base Portal
}

# Combine all known sets for quick lookups
_ALL_KNOWN: dict[str, str] = {}
for _addr in _KNOWN_CEX_ADDRESSES:
    _ALL_KNOWN[_addr.lower()] = "cex"
for _addr in _KNOWN_MIXER_ADDRESSES:
    _ALL_KNOWN[_addr.lower()] = "mixer"
for _addr in _KNOWN_BRIDGE_ADDRESSES:
    _ALL_KNOWN[_addr.lower()] = "bridge"


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def classify_funding(funding_txns: list[dict]) -> str:
    """Classify the funding origin of a wallet from its inbound transactions.

    Parameters
    ----------
    funding_txns:
        A list of transaction dicts, each with at least a ``from``
        field (the sender address).  Optionally: ``value`` (float,
        USD), ``tx_count`` (int, sender's historical tx count).

    Returns
    -------
    str
        One of: ``'cex'``, ``'mixer'``, ``'bridge'``, ``'direct'``,
        ``'unknown'``.

    Notes
    -----
    When on-chain data is unavailable, this function relies on address
    matching against curated lists.  Behavioural heuristics supplement
    the check:
      - If the largest inbound tx comes from a known CEX, return ``'cex'``.
      - If any inbound tx originates from a mixer, return ``'mixer'``.
      - If funded via a bridge, return ``'bridge'``.
      - If there is a single large inflow from a low-tx-count address,
        return ``'direct'`` (likely a known entity or OTC desk).
    """
    if not funding_txns:
        return "unknown"

    # Sort by value descending so the largest transfer is checked first
    sorted_txns = sorted(
        funding_txns,
        key=lambda t: t.get("value", 0.0),
        reverse=True,
    )

    for txn in sorted_txns:
        sender = txn.get("from", "").lower()
        label = _ALL_KNOWN.get(sender)
        if label:
            return label

    # --- Behavioural heuristics (limited without chain data) ---

    # Heuristic: single large inflow from a wallet with very few txns
    # suggests a direct (OTC / known-entity) funding path.
    if len(sorted_txns) == 1:
        txn = sorted_txns[0]
        sender_tx_count = txn.get("tx_count", None)
        value = txn.get("value", 0.0)
        if sender_tx_count is not None and sender_tx_count < 10 and value > 5000.0:
            return "direct"

    return "unknown"


def is_suspicious(funding_type: str, wallet_age_days: int) -> bool:
    """Return True if the funding pattern warrants extra scrutiny.

    Suspicious patterns:
      - Any mixer-funded wallet.
      - Bridge-funded wallet less than 7 days old (possible sybil).
      - Unknown funding source on a very new wallet (< 3 days).
    """
    if funding_type == "mixer":
        return True
    if funding_type == "bridge" and wallet_age_days < 7:
        return True
    if funding_type == "unknown" and wallet_age_days < 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Async persistence / analysis
# ---------------------------------------------------------------------------

async def analyze_wallet_funding(address: str) -> dict:
    """Analyze and persist the funding classification for a single wallet.

    Looks up the wallet's earliest inbound trades and applies
    ``classify_funding``.  Falls back to behavioural heuristics when
    on-chain funding data is unavailable.

    Returns a dict with keys: address, funding_type, suspicious, details.
    """
    # Check if we already have a classification
    rows = await asyncio.to_thread(
        query,
        "SELECT funding_type, first_seen FROM wallets WHERE address = ?",
        [address],
    )

    if rows and rows[0][0]:
        funding_type = rows[0][0]
        first_seen = rows[0][1]
    else:
        # Attempt classification from trade behaviour
        funding_type = await _classify_from_behaviour(address)
        first_seen = None
        if rows:
            first_seen = rows[0][1]

    # Calculate wallet age
    if first_seen:
        if isinstance(first_seen, str):
            first_seen_dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        elif isinstance(first_seen, datetime):
            first_seen_dt = first_seen if first_seen.tzinfo else first_seen.replace(tzinfo=timezone.utc)
        else:
            first_seen_dt = datetime.now(timezone.utc)
        age_days = (datetime.now(timezone.utc) - first_seen_dt).days
    else:
        age_days = 0

    suspicious = is_suspicious(funding_type, age_days)

    # Persist
    try:
        await asyncio.to_thread(
            execute,
            "UPDATE wallets SET funding_type = ? WHERE address = ?",
            [funding_type, address],
        )
        if suspicious:
            await asyncio.to_thread(
                execute,
                "UPDATE wallets SET flagged = true, flag_reason = ? WHERE address = ?",
                [f"suspicious_funding:{funding_type}", address],
            )
    except Exception:
        log.exception("funding_persist_failed", address=address)

    result = {
        "address": address,
        "funding_type": funding_type,
        "suspicious": suspicious,
        "wallet_age_days": age_days,
        "details": f"Classified as '{funding_type}' via behavioural heuristics.",
    }

    log.info("funding_analyzed", **result)
    return result


async def batch_analyze(addresses: list[str]) -> dict[str, dict]:
    """Run funding analysis on a batch of wallet addresses.

    Parameters
    ----------
    addresses:
        List of wallet addresses.

    Returns
    -------
    dict[str, dict]
        Mapping of address to analysis result dict.
    """
    results: dict[str, dict] = {}
    for address in addresses:
        try:
            results[address] = await analyze_wallet_funding(address)
        except Exception:
            log.exception("batch_analyze_failed", address=address)
            results[address] = {
                "address": address,
                "funding_type": "unknown",
                "suspicious": False,
                "wallet_age_days": 0,
                "details": "Analysis failed.",
            }
    return results


# ---------------------------------------------------------------------------
# Behavioural heuristics (when on-chain data is unavailable)
# ---------------------------------------------------------------------------

async def _classify_from_behaviour(address: str) -> str:
    """Infer funding type from trade patterns when chain data is missing.

    Heuristics:
      - If the wallet's first trade is > $5,000 and it has very few
        total trades, it was likely funded by a large entity (``'direct'``).
      - If the wallet appeared recently and trades at very high frequency,
        it may be a bot funded through a bridge (``'bridge'``).
      - Otherwise, ``'unknown'``.
    """
    rows = await asyncio.to_thread(
        query,
        "SELECT price, size, usd_value, ts FROM trades "
        "WHERE wallet = ? ORDER BY ts ASC LIMIT 20",
        [address],
    )

    if not rows:
        return "unknown"

    first_trade_value = rows[0][2] if rows[0][2] else 0.0
    total_fetched = len(rows)

    # Large first trade with few subsequent trades -> likely direct / OTC
    if first_trade_value > 5000.0 and total_fetched < 5:
        return "direct"

    # Very high frequency trading in a short period -> possible bot via bridge
    if total_fetched >= 15:
        first_ts = _to_epoch(rows[0][3])
        last_ts = _to_epoch(rows[-1][3])
        span_hours = (last_ts - first_ts) / 3600.0 if last_ts > first_ts else 1.0
        trades_per_hour = total_fetched / span_hours
        if trades_per_hour > 20:
            return "bridge"

    return "unknown"


# _to_epoch imported from utils
