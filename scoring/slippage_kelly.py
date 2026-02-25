"""Slippage-Adjusted Kelly — Newton's method solver accounting for market impact."""

from __future__ import annotations


def kelly_with_slippage(
    q: float, p: float, bankroll: float, book_depth: float, f_init: float | None = None
) -> float:
    """Kelly fraction adjusted for linear slippage/market impact.

    The effective price after slippage: p_eff(f) = p + (f * bankroll) / (2 * book_depth)
    We solve for f that maximizes E[log(wealth)] via Newton's method.

    Args:
        q: model probability of winning
        p: market price
        bankroll: current bankroll
        book_depth: available ask-side depth in dollars
        f_init: initial guess for fraction (default: standard Kelly)

    Returns:
        Optimal fraction in [0, 1].
    """
    if q <= p or q <= 0 or p <= 0 or p >= 1:
        return 0.0

    # Fall back to standard Kelly if no book depth
    b = (1.0 - p) / p
    kelly_std = max(0.0, (b * q - (1.0 - q)) / b)

    if book_depth <= 0:
        return kelly_std

    f = f_init if f_init is not None else kelly_std
    if f <= 0:
        return 0.0

    slip_k = bankroll / (2.0 * book_depth)  # linear impact: avg fill = midpoint

    for _ in range(20):
        p_eff = p + slip_k * f
        if p_eff >= 1.0 or p_eff <= 0.0:
            f *= 0.5
            continue

        b_eff = (1.0 - p_eff) / p_eff
        denom_win = 1.0 + f * b_eff
        if denom_win <= 0 or f >= 1.0:
            f *= 0.5
            continue

        # Correct derivative via chain rule: p_eff = p + slip_k*f
        # N(f) = p_eff * (1 + f*b_eff), N'(f), N''(f)
        n_val = p_eff * denom_win
        n_prime = (1.0 - p) + slip_k * (1.0 - 2.0 * f)
        n_pp = -2.0 * slip_k

        g_prime = q * n_prime / n_val - q * slip_k / p_eff - (1.0 - q) / (1.0 - f)
        g_pp = (q * n_pp / n_val
                - q * (n_prime / n_val) ** 2
                + q * slip_k ** 2 / p_eff ** 2
                - (1.0 - q) / ((1.0 - f) ** 2))

        if abs(g_pp) < 1e-12:
            break

        f_new = f - g_prime / g_pp
        f = max(0.0, min(1.0, f_new))

        if abs(g_prime) < 1e-8:
            break

    return max(0.0, f)
