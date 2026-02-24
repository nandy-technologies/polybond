"""Async wrapper around py_clob_client.ClobClient for CLOB order execution.

All SDK calls are synchronous, so we dispatch via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import threading
import time as _time
from typing import Any

import config
from utils import log_id
from utils.logger import get_logger

log = get_logger("execution.clob_client")

_CLOB_TIMEOUT = config.CLOB_API_TIMEOUT


async def _to_thread_with_timeout(fn, *args, timeout=_CLOB_TIMEOUT):
    """Wrap sync SDK calls with asyncio timeout to prevent hanging."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)


_client: Any | None = None
_client_lock = threading.Lock()
_proxy_http_client: Any | None = None  # httpx.Client for proxy — closed on shutdown

# Heartbeat state
_heartbeat_task: asyncio.Task | None = None
_heartbeat_id: str = ""  # Discovered from exchange on first call


def _get_client():
    """Lazily create the ClobClient singleton (thread-safe)."""
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        # Double-check after acquiring lock
        if _client is not None:
            return _client

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not config.POLYMARKET_PRIVATE_KEY:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not configured")

        _client = ClobClient(
            host=config.CLOB_API_HOST,
            chain_id=config.POLYGON_CHAIN_ID,  # Polygon mainnet
            key=config.POLYMARKET_PRIVATE_KEY,
            signature_type=config.CLOB_SIGNATURE_TYPE,  # EOA wallet
            funder=config.POLYMARKET_WALLET_ADDRESS or None,
            creds=ApiCreds(
                api_key=config.POLYMARKET_API_KEY,
                api_secret=config.POLYMARKET_API_SECRET,
                api_passphrase=config.POLYMARKET_API_PASSPHRASE,
            ) if config.POLYMARKET_API_KEY else None,
        )
        if config.CLOB_PROXY_URL:
            global _proxy_http_client
            import httpx
            import py_clob_client.http_helpers.helpers as _clob_http
            _proxy_http_client = httpx.Client(http2=True, proxy=config.CLOB_PROXY_URL)
            _clob_http._http_client = _proxy_http_client
            log.info("clob_proxy_configured", proxy=config.CLOB_PROXY_URL.split("@")[-1])
        return _client


def _ensure_usdc_approval() -> None:
    """Approve USDC.e and CTF contracts for Polymarket exchange if needed.

    Checks allowance for exchange, neg-risk exchange, and neg-risk adapter,
    then approves with MAX_INT if insufficient. Also sets CTF approval.
    """
    from web3 import Web3

    USDC_E = config.POLYMARKET_USDC_E_ADDRESS
    CTF = config.POLYMARKET_CTF_ADDRESS
    EXCHANGE = config.POLYMARKET_EXCHANGE_ADDRESS
    NEG_RISK_EXCHANGE = config.POLYMARKET_NEG_RISK_EXCHANGE_ADDRESS
    NEG_RISK_ADAPTER = config.POLYMARKET_NEG_RISK_ADAPTER_ADDRESS
    MAX_INT = 2**256 - 1

    ERC20_ABI = [
        {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    ]
    CTF_ABI = [
        {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
        {"constant": False, "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}], "name": "setApprovalForAll", "outputs": [], "type": "function"},
    ]

    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
    account = w3.eth.account.from_key(config.POLYMARKET_PRIVATE_KEY)
    wallet = account.address

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)

    # Fetch nonce once with "pending" to avoid collisions in rapid succession
    nonce = w3.eth.get_transaction_count(wallet, "pending")

    spenders = [EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]
    _nonce_valid = True
    for spender in spenders:
        spender_cs = Web3.to_checksum_address(spender)
        allowance = usdc.functions.allowance(wallet, spender_cs).call()
        if allowance < MAX_INT // 2:
            log.info("approving_usdc", spender=spender[:10], wallet=wallet)
            tx = usdc.functions.approve(spender_cs, MAX_INT).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": config.APPROVAL_GAS_LIMIT,
                "gasPrice": w3.eth.gas_price,
                "chainId": config.POLYGON_CHAIN_ID,
            })
            signed = w3.eth.account.sign_transaction(tx, config.POLYMARKET_PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_RECEIPT_TIMEOUT)
            if receipt["status"] != 1:
                log.warning("usdc_approve_reverted", spender=spender[:10], tx=tx_hash.hex())
                _nonce_valid = False
                break  # Nonce is now invalid for subsequent txs
            nonce += 1
            log.info("usdc_approved", spender=spender[:10], tx=tx_hash.hex(), status=receipt["status"])

    if not _nonce_valid:
        log.warning("skipping_ctf_approvals_due_to_nonce_error")
        return

    # CTF setApprovalForAll for exchange and neg-risk exchange
    for operator in [EXCHANGE, NEG_RISK_EXCHANGE]:
        operator_cs = Web3.to_checksum_address(operator)
        approved = ctf.functions.isApprovedForAll(wallet, operator_cs).call()
        if not approved:
            log.info("approving_ctf", operator=operator[:10], wallet=wallet)
            tx = ctf.functions.setApprovalForAll(operator_cs, True).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": config.APPROVAL_GAS_LIMIT,
                "gasPrice": w3.eth.gas_price,
                "chainId": config.POLYGON_CHAIN_ID,
            })
            signed = w3.eth.account.sign_transaction(tx, config.POLYMARKET_PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_RECEIPT_TIMEOUT)
            if receipt["status"] != 1:
                log.warning("ctf_approve_reverted", operator=operator[:10], tx=tx_hash.hex())
                break
            nonce += 1
            log.info("ctf_approved", operator=operator[:10], tx=tx_hash.hex(), status=receipt["status"])


def _swap_native_usdc_to_usdc_e() -> float:
    """Swap all native USDC to USDC.e via ParaSwap aggregator on Polygon.

    Polymarket uses USDC.e as collateral. If the wallet holds native USDC,
    this converts it so the bot can trade. ParaSwap finds the cheapest route
    across all Polygon DEXes (typically QuickSwap V3, ~0% slippage).

    Returns the amount of USDC.e received, or 0.0 on failure/no balance.
    """
    import json
    import time
    import urllib.request
    from web3 import Web3

    USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    USDC_E = config.POLYMARKET_USDC_E_ADDRESS
    PARASWAP_PROXY = config.PARASWAP_PROXY_ADDRESS

    ERC20_ABI = [
        {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
        {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    ]

    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
    account = w3.eth.account.from_key(config.POLYMARKET_PRIVATE_KEY)
    wallet = account.address

    usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=ERC20_ABI)
    balance = usdc_n.functions.balanceOf(wallet).call()
    if balance < config.USDC_SWAP_MIN_AMOUNT:  # < $0.10 — not worth swapping
        return 0.0

    amount_usd = balance / 1e6
    log.info("native_usdc_detected", amount=f"${amount_usd:.2f}", action="swapping to USDC.e via ParaSwap")

    # Step 1: Approve ParaSwap's TokenTransferProxy BEFORE getting quote
    # (so quote stays fresh when we submit the swap immediately after)
    proxy_cs = Web3.to_checksum_address(PARASWAP_PROXY)
    allowance = usdc_n.functions.allowance(wallet, proxy_cs).call()
    if allowance < balance:
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        approve_tx = usdc_n.functions.approve(proxy_cs, 2**256 - 1).build_transaction({
            "from": wallet, "nonce": nonce, "gas": config.APPROVAL_GAS_LIMIT,
            "gasPrice": w3.eth.gas_price, "chainId": config.POLYGON_CHAIN_ID,
        })
        signed = w3.eth.account.sign_transaction(approve_tx, config.POLYMARKET_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_RECEIPT_TIMEOUT)
        if receipt["status"] != 1:
            log.warning("native_usdc_approve_failed", tx=tx_hash.hex())
            return 0.0
        log.info("native_usdc_approved_for_paraswap", tx=tx_hash.hex())

    # Capture pre-swap USDC.e balance for accurate delta reporting
    usdc_e_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    pre_swap_bal = usdc_e_contract.functions.balanceOf(wallet).call() / 1e6

    # Step 2: Get fresh quote + build swap tx + submit — all back-to-back
    max_attempts = config.USDC_SWAP_MAX_ATTEMPTS
    for attempt in range(max_attempts):
        # Get price quote
        price_url = (
            f"{config.PARASWAP_API_BASE}/prices?"
            f"srcToken={USDC_NATIVE}&destToken={USDC_E}"
            f"&amount={balance}&srcDecimals=6&destDecimals=6&side=SELL&network={config.POLYGON_CHAIN_ID}"
        )
        req = urllib.request.Request(price_url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=15)
        price_data = json.loads(resp.read())
        price_route = price_data["priceRoute"]

        dest_amount = int(price_route["destAmount"])
        quote_usd = dest_amount / 1e6
        loss_pct = (1.0 - dest_amount / balance) * 100
        log.info("usdc_swap_quote", quote=f"${quote_usd:.4f}", loss=f"{loss_pct:.2f}%", attempt=attempt + 1)

        if loss_pct > config.USDC_SWAP_MAX_LOSS_PCT:
            log.warning("usdc_swap_too_expensive", loss=f"{loss_pct:.2f}%")
            return 0.0

        # Build swap tx via ParaSwap API
        tx_body = json.dumps({
            "srcToken": USDC_NATIVE,
            "destToken": USDC_E,
            "srcAmount": str(balance),
            "priceRoute": price_route,
            "userAddress": wallet,
            "slippage": config.PARASWAP_MAX_SLIPPAGE_BPS,  # 1% max slippage (actual loss is ~0%)
            "ignoreChecks": True,
        }).encode()
        tx_req = urllib.request.Request(
            f"{config.PARASWAP_API_BASE}/transactions/{config.POLYGON_CHAIN_ID}?ignoreChecks=true",
            data=tx_body, headers={"Content-Type": "application/json"},
        )
        tx_resp = urllib.request.urlopen(tx_req, timeout=15)
        tx_data = json.loads(tx_resp.read())

        # Submit on-chain immediately
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        swap_tx = {
            "from": wallet,
            "to": Web3.to_checksum_address(tx_data["to"]),
            "data": tx_data["data"],
            "value": int(tx_data.get("value", "0")),
            "nonce": nonce,
            "gas": config.SWAP_GAS_LIMIT,
            "gasPrice": w3.eth.gas_price,
            "chainId": config.POLYGON_CHAIN_ID,
        }
        signed = w3.eth.account.sign_transaction(swap_tx, config.POLYMARKET_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_RECEIPT_TIMEOUT)

        if receipt["status"] == 1:
            post_swap_bal = usdc_e_contract.functions.balanceOf(wallet).call() / 1e6
            amount_received = post_swap_bal - pre_swap_bal
            if amount_received <= 0:
                amount_received = amount_usd  # RPC stale, use input as approx
            log.info("usdc_swap_success", amount_in=f"${amount_usd:.2f}",
                     amount_out=f"${amount_received:.2f}", tx=tx_hash.hex())
            return amount_received

        log.warning("usdc_swap_reverted", tx=tx_hash.hex(), attempt=attempt + 1)
        if attempt < max_attempts - 1:
            time.sleep(config.USDC_SWAP_RETRY_DELAY)

    return 0.0


async def initialize() -> dict:
    """Verify connectivity and return account info.

    Returns dict with 'balance' key on success.
    Raises RuntimeError if connection fails.
    """
    def _init():
        client = _get_client()
        # Create or derive API credentials if not already set
        if not config.POLYMARKET_API_KEY:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            log.info("api_creds_derived")
        # Ensure USDC.e approvals for exchange contracts
        try:
            _ensure_usdc_approval()
        except Exception as exc:
            log.warning("auto_approval_failed", error=str(exc),
                        hint="wallet may not be funded yet — approval will retry on next restart")
        # Auto-swap native USDC → USDC.e if needed
        swapped = 0.0
        try:
            swapped = _swap_native_usdc_to_usdc_e()
        except Exception as exc:
            log.warning("usdc_swap_error", error=str(exc))
        # Test connectivity
        balance = get_usdc_balance_sync()
        return {"balance": balance, "swapped": swapped}

    result = await _to_thread_with_timeout(_init, timeout=config.CLOB_INIT_TIMEOUT)
    log.info("clob_client_initialized", balance=result["balance"])
    if result.get("swapped", 0) > 0:
        try:
            from alerts.notifier import send_imsg
            await send_imsg(f"AUTO-SWAP: Converted native USDC → ${result['swapped']:.2f} USDC.e")
        except Exception:
            pass
    return result


_last_good_balance: float | None = None  # Last successfully fetched balance
_balance_haircutted: bool = False  # Prevent compounding haircuts


def get_usdc_balance_sync() -> float:
    """Get USDC (COLLATERAL) balance synchronously, in USD.

    On API failure, returns the last known good balance instead of 0.0
    to prevent false circuit breaker triggers. Returns 0.0 only if
    no good balance has ever been fetched.
    """
    global _last_good_balance, _balance_haircutted
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    client = _get_client()
    try:
        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if isinstance(bal, dict):
            raw = float(bal.get("balance", 0))
        else:
            raw = float(getattr(bal, "balance", 0))
        # CLOB API returns balance in micro-USDC (6 decimals)
        result = raw / 1e6
        # Fix #2: prevent geometric haircut decay - only reset flag when we acquire FRESH balance from API
        _last_good_balance = result
        _balance_haircutted = False  # Reset only on fresh API fetch, not cached returns
        return result
    except Exception as exc:
        log.warning("balance_fetch_error", error=str(exc))
        if _last_good_balance is not None:
            log.info("using_cached_balance", cached=_last_good_balance)
            # Don't reset _balance_haircutted here - this is a cached/stale value
            return _last_good_balance
        return 0.0


_balance_cache: tuple[float, float] | None = None  # (balance, monotonic_ts)
_BALANCE_CACHE_TTL = config.BALANCE_CACHE_TTL


def invalidate_balance_cache() -> None:
    """Force next get_usdc_balance() to fetch fresh data.

    Also applies a one-time haircut to _last_good_balance so stale cache
    doesn't allow overlapping orders if subsequent fetches keep failing.
    """
    global _balance_cache, _last_good_balance, _balance_haircutted
    _balance_cache = None
    # Apply haircut only once per invalidation cycle to avoid geometric decay
    if _last_good_balance is not None and _last_good_balance > 0 and not _balance_haircutted:
        _last_good_balance *= config.BALANCE_HAIRCUT_FACTOR  # Conservative 15% haircut
        _balance_haircutted = True


async def get_usdc_balance() -> float:
    """Get USDC balance asynchronously (cached, 30s TTL).
    
    Fix #2: Cache hits don't reset _balance_haircutted - only fresh API fetches do.
    """
    global _balance_cache
    now = _time.monotonic()
    if _balance_cache and (now - _balance_cache[1]) < _BALANCE_CACHE_TTL:
        # Return cached value without touching _balance_haircutted
        return _balance_cache[0]
    bal = await _to_thread_with_timeout(get_usdc_balance_sync)
    _balance_cache = (bal, now)
    return bal


def get_onchain_balances_sync() -> dict:
    """Get on-chain POL (native), USDC.e, and native USDC balances."""
    from web3 import Web3

    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    BALANCE_OF_ABI = [{"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]

    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
    wallet = Web3.to_checksum_address(config.POLYMARKET_WALLET_ADDRESS)

    pol = float(w3.from_wei(w3.eth.get_balance(wallet), "ether"))
    usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=BALANCE_OF_ABI)
    usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=BALANCE_OF_ABI)
    bal_e = usdc_e.functions.balanceOf(wallet).call() / 1e6
    bal_n = usdc_n.functions.balanceOf(wallet).call() / 1e6

    return {"pol": round(pol, 4), "usdc_onchain": round(bal_e + bal_n, 2)}


async def get_onchain_balances() -> dict:
    """Get on-chain balances asynchronously.
    
    Fix #17: Check for minimum POL (gas) balance and alert if insufficient.
    """
    result = await _to_thread_with_timeout(get_onchain_balances_sync)
    # Fix #17: alert on low POL for gas
    if result.get("pol", 0) < config.MIN_POL_GAS_BALANCE:
        log.error("insufficient_pol_for_gas", pol=result.get("pol", 0))
        try:
            from alerts.notifier import send_imsg
            await send_imsg(
                f"LOW POL: {result.get('pol', 0):.4f} POL remaining. "
                f"Fund wallet to continue trading."
            )
        except Exception:
            pass
    return result


async def place_limit_buy(
    token_id: str,
    price: float,
    size_usd: float,
    neg_risk: bool = False,
    equity: float | None = None,
    tick_size: str = "0.01",
    post_only: bool = True,
) -> dict:
    """Place a GTC limit buy order.

    Args:
        token_id: The CLOB token ID to buy.
        price: Limit price (e.g. 0.95).
        size_usd: Dollar amount to spend.
        neg_risk: Whether this is a neg-risk market.
        equity: Current portfolio equity for max-order guard.
        tick_size: Market tick size (e.g. "0.01" or "0.001").
        post_only: If True, reject if order would cross spread. False for taker fills.

    Returns:
        Dict with order info including 'id', 'status'.
    """
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType

    base = equity if equity is not None else config.BOND_SEED_CAPITAL
    max_order = base * config.BOND_MAX_ORDER_PCT
    if size_usd > max_order:
        raise ValueError(
            f"Order size ${size_usd:.2f} exceeds max ${max_order:.2f} "
            f"({config.BOND_MAX_ORDER_PCT:.0%} of equity ${base:.2f})"
        )

    def _place():
        client = _get_client()

        # Calculate shares from price and USD size
        shares = size_usd / price
        if shares < config.POLYMARKET_MIN_SHARES:
            raise ValueError(f"Order too small: {shares:.1f} shares (minimum {config.POLYMARKET_MIN_SHARES})")

        order_args = OrderArgs(
            price=price,
            size=shares,
            side=BUY,
            token_id=token_id,
            expiration=int(_time.time() + config.BOND_ORDER_TIMEOUT_HOURS * 3600),
        )

        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )

        signed_order = client.create_order(order_args, options=options)
        result = client.post_order(signed_order, orderType=OrderType.GTD, post_only=post_only)
        return result

    try:
        result = await _to_thread_with_timeout(_place)
        order_info = _normalize_order_result(result)
        log.info(
            "order_placed",
            token_id=log_id(token_id),
            price=price,
            size_usd=size_usd,
            post_only=post_only,
            order_id=order_info.get("id", "?"),
        )
        return order_info
    except Exception as exc:
        log.error("order_place_failed", token_id=log_id(token_id), price=price, error=str(exc))
        raise


async def cancel_order(order_id: str) -> bool:
    """Cancel an open order by ID."""
    def _cancel():
        client = _get_client()
        return client.cancel(order_id)

    try:
        result = await _to_thread_with_timeout(_cancel)
        # SDK may return a dict with error info
        if isinstance(result, dict) and result.get("not_canceled"):
            log.warning("order_cancel_rejected", order_id=order_id, result=str(result)[:200])
            return False
        log.info("order_cancelled", order_id=order_id)
        return True
    except Exception as exc:
        log.warning("order_cancel_failed", order_id=order_id, error=str(exc))
        return False


async def get_order_status(order_id: str) -> dict:
    """Get current status of an order."""
    def _get():
        client = _get_client()
        return client.get_order(order_id)

    try:
        result = await _to_thread_with_timeout(_get)
        return _normalize_order_result(result)
    except Exception as exc:
        log.warning("order_status_failed", order_id=order_id, error=str(exc))
        return {"id": order_id, "status": "unknown", "error": str(exc)}


async def get_tick_size(token_id: str) -> str:
    """Get tick size for a token via SDK (cached internally by the SDK)."""
    def _get():
        client = _get_client()
        return client.get_tick_size(token_id)

    try:
        return await _to_thread_with_timeout(_get)
    except Exception as exc:
        log.warning("tick_size_fallback", token_id=log_id(token_id), error=str(exc))
        return "0.01"


async def redeem_positions(condition_id: str, neg_risk: bool = False) -> str | None:
    """Redeem resolved CTF positions on-chain for USDC.e.

    Calls redeemPositions on the Gnosis CTF contract (or NegRiskAdapter for neg-risk markets).
    Returns tx hash on success, None on failure.
    """
    def _redeem():
        from web3 import Web3

        CTF = config.POLYMARKET_CTF_ADDRESS
        NEG_RISK_ADAPTER = config.POLYMARKET_NEG_RISK_ADAPTER_ADDRESS
        USDC_E = config.POLYMARKET_USDC_E_ADDRESS

        CTF_REDEEM_ABI = [
            {
                "inputs": [
                    {"name": "collateralToken", "type": "address"},
                    {"name": "parentCollectionId", "type": "bytes32"},
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"},
                ],
                "name": "redeemPositions",
                "outputs": [],
                "type": "function",
            },
            {
                "inputs": [{"name": "conditionId", "type": "bytes32"}],
                "name": "payoutNumerators",
                "outputs": [{"name": "", "type": "uint256[]"}],
                "type": "function",
                "constant": True,
            },
        ]

        # NegRiskAdapter has its own redeemPositions(conditionId, indexSets)
        NEG_RISK_REDEEM_ABI = [
            {
                "inputs": [
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"},
                ],
                "name": "redeemPositions",
                "outputs": [],
                "type": "function",
            },
        ]

        w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
        account = w3.eth.account.from_key(config.POLYMARKET_PRIVATE_KEY)
        wallet = account.address

        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        parent_collection = b"\x00" * 32

        # indexSets: [1, 2] covers both Yes (0b01) and No (0b10) outcome slots
        index_sets = [1, 2]

        nonce = w3.eth.get_transaction_count(wallet, "pending")
        gas_price = w3.eth.gas_price

        if neg_risk:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                abi=NEG_RISK_REDEEM_ABI,
            )
            tx = contract.functions.redeemPositions(
                condition_bytes,
                index_sets,
            ).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": config.REDEEM_GAS_LIMIT,
                "gasPrice": gas_price,
                "chainId": config.POLYGON_CHAIN_ID,
            })
        else:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(CTF),
                abi=CTF_REDEEM_ABI,
            )
            tx = contract.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E),
                parent_collection,
                condition_bytes,
                index_sets,
            ).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": config.REDEEM_GAS_LIMIT,
                "gasPrice": gas_price,
                "chainId": config.POLYGON_CHAIN_ID,
            })

        signed = w3.eth.account.sign_transaction(tx, config.POLYMARKET_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_RECEIPT_TIMEOUT)

        if receipt["status"] != 1:
            log.warning("redeem_reverted", condition_id=log_id(condition_id), tx=tx_hash.hex())
            return None

        return tx_hash.hex()

    try:
        result = await _to_thread_with_timeout(_redeem, timeout=30)
        if result:
            log.info("positions_redeemed", condition_id=log_id(condition_id), tx=result)
            invalidate_balance_cache()
        return result
    except Exception as exc:
        log.warning("redeem_failed", condition_id=log_id(condition_id), error=str(exc))
        return None


async def get_neg_risk(token_id: str) -> bool:
    """Check if a market uses neg-risk model."""
    def _check():
        client = _get_client()
        try:
            return client.get_neg_risk(token_id)
        except Exception:
            return False

    try:
        return await _to_thread_with_timeout(_check)
    except Exception:
        return False


async def get_fee_rate(token_id: str) -> int:
    """Get fee rate in basis points for a token."""
    def _get():
        client = _get_client()
        return client.get_fee_rate_bps(token_id)

    try:
        return await _to_thread_with_timeout(_get)
    except Exception:
        return config.BOND_DEFAULT_FEE_BPS


def _normalize_order_result(result) -> dict:
    """Normalize SDK order result to a consistent dict format."""
    if isinstance(result, dict):
        return {
            "id": result.get("orderID", result.get("id", result.get("order_id", ""))),
            "status": result.get("status", result.get("orderStatus", "unknown")),
            "price": float(result.get("price", 0)),
            "size": float(result.get("size", result.get("original_size", 0))),
            "filled": float(result.get("size_matched", result.get("filled_size", 0))),
            "raw": result,
        }
    # Handle object responses
    return {
        "id": getattr(result, "orderID", getattr(result, "id", "")),
        "status": getattr(result, "status", "unknown"),
        "price": float(getattr(result, "price", 0)),
        "size": float(getattr(result, "size", 0)),
        "filled": float(getattr(result, "size_matched", 0)),
        "raw": str(result),
    }


# ── Heartbeat dead-man's switch ─────────────────────────────


async def start_heartbeat() -> None:
    """Start the heartbeat loop. Once started, if a heartbeat is missed
    within 10 seconds the exchange auto-cancels ALL open orders."""
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return  # already running

    _heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="clob_heartbeat")
    log.info("heartbeat_started")


async def stop_heartbeat() -> None:
    """Stop the heartbeat loop gracefully (before shutdown so orders
    aren't needlessly cancelled)."""
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
    _heartbeat_task = None
    log.info("heartbeat_stopped")


def close_proxy_client() -> None:
    """Close the proxy httpx.Client if one was created."""
    global _proxy_http_client
    if _proxy_http_client is not None:
        try:
            _proxy_http_client.close()
        except Exception:
            pass
        _proxy_http_client = None


_heartbeat_consecutive_failures: int = 0


async def _heartbeat_loop() -> None:
    """Send heartbeat every 5 seconds (exchange timeout is 10s).

    The exchange uses a rolling heartbeat ID: each successful POST returns
    the ID to use for the NEXT heartbeat.  On the very first call the
    exchange rejects with 400 but includes the initial heartbeat_id in the
    error response — we extract it and retry.
    """
    global _heartbeat_id, _heartbeat_consecutive_failures
    while True:
        try:
            new_id = await _to_thread_with_timeout(_post_heartbeat_sync, _heartbeat_id, timeout=config.HEARTBEAT_POST_TIMEOUT)
            if new_id:
                _heartbeat_id = new_id
            _heartbeat_consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err_str = str(exc)
            # On first call, extract the initial heartbeat_id from the error
            if "Invalid Heartbeat ID" in err_str:
                import re, json as _json
                _hb_found = False
                # Try JSON parse first
                try:
                    err_data = _json.loads(err_str)
                    if "heartbeat_id" in err_data:
                        _heartbeat_id = err_data["heartbeat_id"]
                        _hb_found = True
                except (_json.JSONDecodeError, TypeError):
                    pass
                # Fallback to regex
                if not _hb_found:
                    match = re.search(r"'heartbeat_id':\s*'([^']+)'", err_str)
                    if not match:
                        match = re.search(r'"heartbeat_id":\s*"([^"]+)"', err_str)
                    if match:
                        _heartbeat_id = match.group(1)
                        _hb_found = True
                if _hb_found:
                    log.info("heartbeat_id_discovered", hb_id=_heartbeat_id[:8])
                    _heartbeat_consecutive_failures = 0
                else:
                    log.warning("heartbeat_error", error=err_str[:200])
                    _heartbeat_consecutive_failures += 1
            else:
                log.warning("heartbeat_error", error=err_str[:200])
                _heartbeat_consecutive_failures += 1

            # Fix #34: Alert after 3 consecutive failures (15s+) to reduce false positives
            if _heartbeat_consecutive_failures == config.HEARTBEAT_ALERT_THRESHOLD:
                try:
                    from alerts.notifier import send_imsg
                    await send_imsg(
                        f"HEARTBEAT FAILURE: {_heartbeat_consecutive_failures} consecutive misses. "
                        f"Exchange may cancel all orders! Error: {err_str[:100]}"
                    )
                except Exception:
                    pass
        await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)


def _post_heartbeat_sync(hb_id: str) -> str | None:
    """Post heartbeat and return the next heartbeat_id."""
    client = _get_client()
    result = client.post_heartbeat(hb_id)
    if isinstance(result, dict):
        return result.get("heartbeat_id")
    return None


def get_heartbeat_status() -> dict:
    """Return heartbeat status for dashboard display."""
    return {
        "active": _heartbeat_task is not None and not _heartbeat_task.done(),
        "heartbeat_id": _heartbeat_id[:8] + "..." if _heartbeat_id else "",
    }


# ── Sell orders ──────────────────────────────────────────────


async def place_limit_sell(
    token_id: str,
    price: float,
    shares: float,
    neg_risk: bool = False,
    tick_size: str = "0.01",
    post_only: bool = True,
) -> dict:
    """Place a GTC limit sell order.

    Args:
        token_id: The CLOB token ID to sell.
        price: Limit price (e.g. 0.95).
        shares: Number of shares to sell.
        neg_risk: Whether this is a neg-risk market.
        tick_size: Market tick size.
        post_only: If True, order is rejected if it would cross the spread.
                   Pass False for auto-exits that need to fill at best bid.

    Returns:
        Dict with order info including 'id', 'status'.
    """
    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions, OrderType

    if shares < config.POLYMARKET_MIN_SHARES:
        raise ValueError(f"Order too small: {shares:.1f} shares (minimum {config.POLYMARKET_MIN_SHARES})")

    def _place():
        client = _get_client()
        order_args = OrderArgs(
            price=price,
            size=shares,
            side=SELL,
            token_id=token_id,
            expiration=int(_time.time() + config.BOND_SELL_ORDER_TIMEOUT_SECS),  # 1hr for exits
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        signed_order = client.create_order(order_args, options=options)
        return client.post_order(signed_order, orderType=OrderType.GTD, post_only=post_only)

    try:
        result = await _to_thread_with_timeout(_place)
        order_info = _normalize_order_result(result)
        log.info(
            "sell_order_placed",
            token_id=log_id(token_id),
            price=price,
            shares=shares,
            order_id=order_info.get("id", "?"),
        )
        return order_info
    except Exception as exc:
        log.error("sell_order_failed", token_id=log_id(token_id), price=price, error=str(exc))
        raise


async def place_market_sell(
    token_id: str,
    shares: float,
    neg_risk: bool = False,
    tick_size: str = "0.01",
) -> dict:
    """Place a FOK (fill-or-kill) market sell for emergency exits.

    Args:
        token_id: The CLOB token ID to sell.
        shares: Number of shares to sell.
        neg_risk: Whether this is a neg-risk market.
        tick_size: Market tick size.

    Returns:
        Dict with order info.
    """
    from py_clob_client.clob_types import MarketOrderArgs, PartialCreateOrderOptions, OrderType

    def _place():
        client = _get_client()
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=shares,
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        signed_order = client.create_market_order(order_args, options=options)
        return client.post_order(signed_order, orderType=OrderType.FOK)

    try:
        result = await _to_thread_with_timeout(_place)
        order_info = _normalize_order_result(result)
        log.info(
            "market_sell_placed",
            token_id=log_id(token_id),
            shares=shares,
            order_id=order_info.get("id", "?"),
        )
        return order_info
    except Exception as exc:
        log.error("market_sell_failed", token_id=log_id(token_id), shares=shares, error=str(exc))
        raise


# ── Order management ─────────────────────────────────────────


async def get_open_orders(market: str | None = None, asset_id: str | None = None) -> list[dict]:
    """Get all open orders from the exchange.

    Returns list of normalized order dicts.
    """
    from py_clob_client.clob_types import OpenOrderParams

    def _get():
        client = _get_client()
        params = OpenOrderParams()
        if market:
            params.market = market
        if asset_id:
            params.asset_id = asset_id
        return client.get_orders(params=params)

    try:
        results = await _to_thread_with_timeout(_get)
        if not isinstance(results, list):
            return []
        return [_normalize_order_result(r) for r in results]
    except Exception as exc:
        log.warning("get_open_orders_failed", error=str(exc))
        return []


async def cancel_all_orders() -> bool:
    """Cancel ALL open orders on the exchange (server-side, one call)."""
    def _cancel():
        client = _get_client()
        return client.cancel_all()

    try:
        await _to_thread_with_timeout(_cancel)
        log.info("all_orders_cancelled_server_side")
        return True
    except Exception as exc:
        log.error("cancel_all_failed", error=str(exc))
        return False


async def cancel_orders_batch(order_ids: list[str]) -> bool:
    """Cancel multiple orders by ID in one request."""
    def _cancel():
        client = _get_client()
        return client.cancel_orders(order_ids)

    try:
        await _to_thread_with_timeout(_cancel)
        log.info("batch_cancel_complete", count=len(order_ids))
        return True
    except Exception as exc:
        log.warning("batch_cancel_failed", count=len(order_ids), error=str(exc))
        return False


async def cancel_market_orders(market: str = "", asset_id: str = "") -> bool:
    """Cancel all orders for a specific market or asset."""
    def _cancel():
        client = _get_client()
        return client.cancel_market_orders(market=market, asset_id=asset_id)

    try:
        await _to_thread_with_timeout(_cancel)
        log.info("market_orders_cancelled", market=log_id(market), asset_id=log_id(asset_id))
        return True
    except Exception as exc:
        log.warning("market_orders_cancel_failed", error=str(exc))
        return False


# ── Maker rebate scoring ─────────────────────────────────────


async def check_orders_scoring(order_ids: list[str]) -> dict:
    """Check if orders are earning maker rebates. Returns {order_id: bool}."""
    from py_clob_client.clob_types import OrdersScoringParams

    def _check():
        client = _get_client()
        return client.are_orders_scoring(OrdersScoringParams(orderIds=order_ids))

    try:
        result = await _to_thread_with_timeout(_check)
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        log.warning("scoring_check_failed", error=str(exc))
        return {}


# ── Batch order submission ────────────────────────────────────


async def place_limit_buys_batch(orders: list[dict]) -> list[dict]:
    """Place multiple GTD limit buy orders in one API call.

    Each entry: {"token_id", "price", "size_usd", "neg_risk", "tick_size", "equity"}
    Returns list of order results (same order as input).
    """
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import (
        OrderArgs, PartialCreateOrderOptions, OrderType, PostOrdersArgs,
    )

    def _place():
        client = _get_client()
        signed_orders = []
        for o in orders:
            shares = o["size_usd"] / o["price"]
            args = OrderArgs(
                price=o["price"],
                size=shares,
                side=BUY,
                token_id=o["token_id"],
                expiration=int(_time.time() + config.BOND_ORDER_TIMEOUT_HOURS * 3600),
            )
            opts = PartialCreateOrderOptions(
                tick_size=o["tick_size"],
                neg_risk=o.get("neg_risk", False),
            )
            signed = client.create_order(args, options=opts)
            signed_orders.append(
                PostOrdersArgs(order=signed, orderType=OrderType.GTD, postOnly=not o.get("is_taker", False))
            )
        return client.post_orders(signed_orders)

    return await _to_thread_with_timeout(_place, timeout=config.CLOB_BATCH_TIMEOUT)


# ── REST orderbook ───────────────────────────────────────────


async def get_orderbook_rest(token_id: str) -> dict | None:
    """Fetch orderbook via REST API (for bootstrap before WS data arrives)."""
    def _get():
        client = _get_client()
        return client.get_order_book(token_id)

    try:
        result = await _to_thread_with_timeout(_get)
        # Convert OrderBookSummary to our standard dict format
        bids = []
        asks = []
        if hasattr(result, "bids") and result.bids:
            bids = [{"price": float(b.price), "size": float(b.size)} for b in result.bids]
        if hasattr(result, "asks") and result.asks:
            asks = [{"price": float(a.price), "size": float(a.size)} for a in result.asks]

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0

        return {
            "market_id": getattr(result, "market", "") or "",
            "asset_id": getattr(result, "asset_id", token_id) or token_id,
            "bids": sorted(bids, key=lambda x: x["price"], reverse=True)[:10],
            "asks": sorted(asks, key=lambda x: x["price"])[:10],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "mid_price": round(mid_price, 4),
            "ask_depth": round(sum(a["price"] * a["size"] for a in sorted(asks, key=lambda x: x["price"])[:10]), 2),
            "bid_depth": round(sum(b["price"] * b["size"] for b in sorted(bids, key=lambda x: x["price"], reverse=True)[:10]), 2),
            "ts": __import__("time").time(),
        }
    except Exception as exc:
        log.debug("orderbook_rest_failed", token_id=log_id(token_id), error=str(exc))
        return None
