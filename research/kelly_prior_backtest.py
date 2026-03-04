#!/usr/bin/env python3
"""
Empirical Kelly Prior from Historical Polymarket Data — Continuous Function Fit

Pulls resolved binary markets from Gamma API, gets pre-resolution prices from CLOB,
and fits continuous functions mapping price → P(resolve YES).
"""

import json
import time
import warnings
import numpy as np
import requests
from scipy.optimize import curve_fit
from scipy.stats import beta as beta_dist
from scipy.special import expit
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

warnings.filterwarnings('ignore')

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICES_URL = "https://clob.polymarket.com/prices-history"
OUTPUT_DIR = Path(__file__).parent
PLOT_PATH = OUTPUT_DIR / "kelly_prior_fit.png"
RESULTS_PATH = OUTPUT_DIR / "kelly_prior_results.md"

# How many days before market close to sample the price
# We'll try multiple lookback windows and use whichever has data
LOOKBACK_DAYS = [7, 3, 14, 1]


def fetch_closed_binary_markets(max_markets=20000, start_offset=5000):
    """Fetch closed binary markets from Gamma API. Start at higher offsets for recent markets with CLOB data."""
    markets = []
    offset = start_offset
    limit = 100
    empty_count = 0
    
    print(f"Fetching closed binary markets starting at offset {start_offset}...")
    while offset < start_offset + max_markets:
        try:
            r = requests.get(GAMMA_URL, params={
                'closed': 'true', 'active': 'false',
                'limit': limit, 'offset': offset
            }, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            offset += limit
            empty_count += 1
            if empty_count > 5:
                break
            continue
        
        if not batch:
            empty_count += 1
            if empty_count > 3:
                print(f"  No more markets at offset {offset}")
                break
            offset += limit
            continue
        
        empty_count = 0
        for m in batch:
            try:
                outcomes = json.loads(m.get('outcomes', '[]'))
                prices = json.loads(m.get('outcomePrices', '[]'))
                tokens = json.loads(m.get('clobTokenIds', '[]'))
            except (json.JSONDecodeError, TypeError):
                continue
            
            # Only binary markets
            if len(outcomes) != 2 or len(prices) != 2 or len(tokens) < 1:
                continue
            
            # Determine resolution from final prices
            try:
                p0, p1 = float(prices[0]), float(prices[1])
            except (ValueError, TypeError):
                continue
            
            # Market must be clearly resolved (one outcome near 1)
            if p0 > 0.95:
                resolved_yes = True
            elif p1 > 0.95:
                resolved_yes = False
            else:
                continue  # ambiguous / not resolved
            
            markets.append({
                'question': m.get('question', ''),
                'token_id': tokens[0],  # YES token
                'resolved_yes': resolved_yes,
                'volume': float(m.get('volumeNum', 0) or m.get('volume', 0) or 0),
                'end_date': m.get('endDate', ''),
                'closed_time': m.get('closedTime', ''),
            })
        
        if len(markets) % 500 < 100:
            print(f"  offset={offset}, {len(markets)} binary resolved markets so far")
        offset += limit
    
    print(f"Total binary resolved markets: {len(markets)}")
    return markets


def fetch_pre_resolution_prices(markets, max_fetch=5000):
    """For each market, fetch price history and extract pre-resolution price in the bond zone."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    session = requests.Session()
    
    def fetch_one(m):
        try:
            r = session.get(CLOB_PRICES_URL, params={
                'market': m['token_id'],
                'interval': 'all',
                'fidelity': 1440
            }, timeout=10)
            r.raise_for_status()
            history = r.json().get('history', [])
        except Exception:
            return None, 'error'
        
        if len(history) < 3:
            return None, 'no_history'
        
        usable_history = history[:-1]
        if len(usable_history) < 2:
            return None, 'no_history'
        
        price = usable_history[-1].get('p', None)
        if price is None:
            return None, 'no_history'
        
        price = float(price)
        if 0.50 <= price <= 0.995:
            return (price, 1 if m['resolved_yes'] else 0), 'ok'
        return None, 'outside'
    
    to_fetch = markets[:max_fetch]
    print(f"\nFetching price histories for {len(to_fetch)} markets (concurrent)...")
    
    data_points = []
    errors = no_history = outside_zone = 0
    done = 0
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_one, m): m for m in to_fetch}
        for future in as_completed(futures):
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(to_fetch)} processed, {len(data_points)} data points")
            result, status = future.result()
            if status == 'error':
                errors += 1
            elif status == 'no_history':
                no_history += 1
            elif status == 'outside':
                outside_zone += 1
            elif result is not None:
                data_points.append(result)
    
    print(f"\nFetch complete: {done} fetched, {len(data_points)} usable data points, {errors} errors, {no_history} no-history, {outside_zone} outside-zone")
    return data_points


def compute_empirical_rates(data_points, n_bins=30, range_min=0.50, range_max=1.0):
    """Compute empirical resolution rates in bins for plotting."""
    prices = np.array([d[0] for d in data_points])
    outcomes = np.array([d[1] for d in data_points])
    
    bin_edges = np.linspace(range_min, range_max, n_bins + 1)
    bin_centers = []
    bin_rates = []
    bin_counts = []
    
    for i in range(n_bins):
        mask = (prices >= bin_edges[i]) & (prices < bin_edges[i + 1])
        count = mask.sum()
        if count >= 5:  # minimum sample
            bin_centers.append((bin_edges[i] + bin_edges[i + 1]) / 2)
            bin_rates.append(outcomes[mask].mean())
            bin_counts.append(count)
    
    return np.array(bin_centers), np.array(bin_rates), np.array(bin_counts)


# ---- Model definitions ----

def logistic_model(x, a, b):
    """P(YES) = 1 / (1 + exp(-a*(x - b)))"""
    return expit(a * (x - b))

def power_model(x, k):
    """P(YES) = x^k"""
    return np.clip(x, 0, 1) ** k

def beta_cdf_model(x, a, b):
    """P(YES) = BetaCDF(x; a, b)"""
    return beta_dist.cdf(x, a, b)

def poly3_model(x, a, b, c, d):
    """Cubic polynomial, clipped to [0,1]"""
    return np.clip(a * x**3 + b * x**2 + c * x + d, 0, 1)


def fit_models(prices, outcomes):
    """Fit all candidate models and return results."""
    results = {}
    n = len(prices)
    
    # 1. Logistic
    try:
        popt, pcov = curve_fit(logistic_model, prices, outcomes, p0=[10, 0.5], maxfev=10000)
        pred = logistic_model(prices, *popt)
        residuals = outcomes - pred
        sse = np.sum(residuals**2)
        k_params = 2
        aic = n * np.log(sse / n + 1e-10) + 2 * k_params
        bic = n * np.log(sse / n + 1e-10) + k_params * np.log(n)
        # Log-likelihood for binary outcomes
        ll = np.sum(outcomes * np.log(pred + 1e-10) + (1 - outcomes) * np.log(1 - pred + 1e-10))
        aic_ll = -2 * ll + 2 * k_params
        results['logistic'] = {'params': popt, 'names': ['a', 'b'], 'aic': aic_ll, 'func': logistic_model, 'll': ll}
        print(f"  Logistic: a={popt[0]:.4f}, b={popt[1]:.4f}, AIC={aic_ll:.1f}")
    except Exception as e:
        print(f"  Logistic fit failed: {e}")
    
    # 2. Power law
    try:
        popt, pcov = curve_fit(power_model, prices, outcomes, p0=[1.0], maxfev=10000)
        pred = power_model(prices, *popt)
        ll = np.sum(outcomes * np.log(pred + 1e-10) + (1 - outcomes) * np.log(1 - pred + 1e-10))
        k_params = 1
        aic_ll = -2 * ll + 2 * k_params
        results['power'] = {'params': popt, 'names': ['k'], 'aic': aic_ll, 'func': power_model, 'll': ll}
        print(f"  Power: k={popt[0]:.4f}, AIC={aic_ll:.1f}")
    except Exception as e:
        print(f"  Power fit failed: {e}")
    
    # 3. Beta CDF
    try:
        popt, pcov = curve_fit(beta_cdf_model, prices, outcomes, p0=[2, 0.5], maxfev=10000,
                               bounds=([0.01, 0.01], [100, 100]))
        pred = beta_cdf_model(prices, *popt)
        ll = np.sum(outcomes * np.log(pred + 1e-10) + (1 - outcomes) * np.log(1 - pred + 1e-10))
        k_params = 2
        aic_ll = -2 * ll + 2 * k_params
        results['beta_cdf'] = {'params': popt, 'names': ['a', 'b'], 'aic': aic_ll, 'func': beta_cdf_model, 'll': ll}
        print(f"  Beta CDF: a={popt[0]:.4f}, b={popt[1]:.4f}, AIC={aic_ll:.1f}")
    except Exception as e:
        print(f"  Beta CDF fit failed: {e}")
    
    # 4. Polynomial (cubic)
    try:
        popt, pcov = curve_fit(poly3_model, prices, outcomes, p0=[1, -1, 1, 0], maxfev=10000)
        pred = poly3_model(prices, *popt)
        ll = np.sum(outcomes * np.log(pred + 1e-10) + (1 - outcomes) * np.log(1 - pred + 1e-10))
        k_params = 4
        aic_ll = -2 * ll + 2 * k_params
        results['poly3'] = {'params': popt, 'names': ['a', 'b', 'c', 'd'], 'aic': aic_ll, 'func': poly3_model, 'll': ll}
        print(f"  Poly3: {', '.join(f'{n}={v:.4f}' for n, v in zip(['a','b','c','d'], popt))}, AIC={aic_ll:.1f}")
    except Exception as e:
        print(f"  Poly3 fit failed: {e}")
    
    return results


def make_plot(bin_centers, bin_rates, bin_counts, models, best_name):
    """Generate the comparison plot."""
    fig, ax = plt.subplots(1, 1, figsize=(12, 7))
    
    # Scatter of empirical rates (size proportional to sample count)
    sizes = np.clip(bin_counts / 2, 10, 200)
    ax.scatter(bin_centers, bin_rates, s=sizes, alpha=0.7, c='steelblue', 
               edgecolors='navy', label='Empirical resolution rate', zorder=5)
    
    # Fitted curves
    x_smooth = np.linspace(0.50, 0.995, 200)
    colors = {'logistic': 'red', 'power': 'green', 'beta_cdf': 'purple', 'poly3': 'orange'}
    
    for name, model in models.items():
        pred = model['func'](x_smooth, *model['params'])
        style = '-' if name == best_name else '--'
        lw = 2.5 if name == best_name else 1.5
        ax.plot(x_smooth, pred, style, color=colors.get(name, 'gray'), 
                label=f'{name} (AIC={model["aic"]:.0f})', linewidth=lw)
    
    # Current flat prior
    ax.axhline(y=0.952, color='gray', linestyle=':', linewidth=2, label='Current flat prior (q=0.952)')
    
    # Perfect calibration line
    ax.plot([0.5, 1.0], [0.5, 1.0], 'k--', alpha=0.3, label='Perfect calibration (q=price)')
    
    # Bond zone shading
    ax.axvspan(0.80, 0.99, alpha=0.08, color='green', label='Bond zone (0.80-0.99)')
    
    ax.set_xlabel('YES Token Price (pre-resolution)', fontsize=13)
    ax.set_ylabel('P(Resolves YES)', fontsize=13)
    ax.set_title('Polymarket Empirical Resolution Rate vs Price\nContinuous Function Fits', fontsize=14)
    ax.legend(loc='lower right', fontsize=10)
    ax.set_xlim(0.49, 1.01)
    ax.set_ylim(0.3, 1.05)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(PLOT_PATH, dpi=150)
    print(f"\nPlot saved to {PLOT_PATH}")


def write_results(models, best_name, key_prices, bin_stats):
    """Write findings to markdown."""
    best = models[best_name]
    
    lines = [
        "# Kelly Prior: Empirical Fit from Polymarket Historical Data",
        "",
        f"*Generated: {time.strftime('%Y-%m-%d %H:%M')}*",
        "",
        "## Methodology",
        "",
        "- Pulled thousands of resolved binary markets from the Gamma API",
        "- Fetched daily price history from CLOB API for each market",
        "- Used the price **1 day before resolution** as the pre-resolution price",
        "- Filtered to markets with prices in the 0.50-0.99 range",
        "- Fit continuous functions: logistic, power law, beta CDF, cubic polynomial",
        "- Selected best model by AIC (log-likelihood based)",
        "",
        "## Sample Sizes by Price Range",
        "",
        "| Price Range | N | Resolution Rate |",
        "|---|---|---|",
    ]
    for label, n, rate in bin_stats:
        lines.append(f"| {label} | {n} | {rate:.3f} |")
    
    lines += [
        "",
        "## Model Comparison",
        "",
        "| Model | Parameters | AIC | Log-Likelihood |",
        "|---|---|---|---|",
    ]
    for name, m in sorted(models.items(), key=lambda x: x[1]['aic']):
        param_str = ", ".join(f"{n}={v:.4f}" for n, v in zip(m['names'], m['params']))
        marker = " ✅" if name == best_name else ""
        lines.append(f"| {name}{marker} | {param_str} | {m['aic']:.1f} | {m['ll']:.1f} |")
    
    lines += [
        "",
        f"## Best Model: **{best_name}**",
        "",
    ]
    
    # Model-specific formula
    if best_name == 'logistic':
        a, b = best['params']
        lines.append(f"```\nP(YES) = 1 / (1 + exp(-{a:.4f} * (price - {b:.4f})))\n```")
    elif best_name == 'power':
        k = best['params'][0]
        lines.append(f"```\nP(YES) = price ^ {k:.4f}\n```")
    elif best_name == 'beta_cdf':
        a, b = best['params']
        lines.append(f"```\nP(YES) = BetaCDF(price; a={a:.4f}, b={b:.4f})\n```")
    elif best_name == 'poly3':
        a, b, c, d = best['params']
        lines.append(f"```\nP(YES) = {a:.4f}*x³ + {b:.4f}*x² + {c:.4f}*x + {d:.4f}\n```")
    
    lines += [
        "",
        "## Implied q at Key Prices",
        "",
        "| Price | Empirical q | Flat Prior (0.952) | Delta |",
        "|---|---|---|---|",
    ]
    for price, q in key_prices:
        delta = q - 0.952
        lines.append(f"| {price:.2f} | {q:.4f} | 0.9520 | {delta:+.4f} |")
    
    lines += [
        "",
        "## Recommendation",
        "",
        f"Replace the flat `q_mean = 0.952` prior with the **{best_name}** function.",
        "This gives a price-dependent prior that better reflects the empirical data:",
        "- Lower prices (0.80-0.90) → lower resolution probability → less aggressive buying",
        "- Higher prices (0.95+) → higher resolution probability → appropriate aggression",
        "",
        "### Integration Code",
        "",
        "```python",
    ]
    
    if best_name == 'logistic':
        a, b = best['params']
        lines += [
            "from scipy.special import expit",
            "",
            f"def price_to_q(price: float) -> float:",
            f'    """Empirical prior: price → P(resolve YES)"""',
            f"    return float(expit({a:.6f} * (price - {b:.6f})))",
        ]
    elif best_name == 'power':
        k = best['params'][0]
        lines += [
            f"def price_to_q(price: float) -> float:",
            f'    """Empirical prior: price → P(resolve YES)"""',
            f"    return price ** {k:.6f}",
        ]
    elif best_name == 'beta_cdf':
        a, b = best['params']
        lines += [
            "from scipy.stats import beta as beta_dist",
            "",
            f"def price_to_q(price: float) -> float:",
            f'    """Empirical prior: price → P(resolve YES)"""',
            f"    return float(beta_dist.cdf(price, {a:.6f}, {b:.6f}))",
        ]
    elif best_name == 'poly3':
        a, b, c, d = best['params']
        lines += [
            f"def price_to_q(price: float) -> float:",
            f'    """Empirical prior: price → P(resolve YES)"""',
            f"    return max(0, min(1, {a:.6f}*price**3 + {b:.6f}*price**2 + {c:.6f}*price + {d:.6f}))",
        ]
    
    lines += [
        "```",
        "",
        "### Usage in Kelly calculation",
        "",
        "```python",
        "# Replace: q_mean = 0.952",
        "# With:",
        "q_mean = price_to_q(yes_price)",
        "```",
    ]
    
    with open(RESULTS_PATH, 'w') as f:
        f.write('\n'.join(lines))
    
    print(f"Results written to {RESULTS_PATH}")


def main():
    print("=" * 70)
    print("EMPIRICAL KELLY PRIOR — POLYMARKET HISTORICAL DATA")
    print("=" * 70)
    
    # Step 1: Fetch markets
    markets = fetch_closed_binary_markets(max_markets=8000, start_offset=10000)
    
    if not markets:
        print("ERROR: No markets fetched!")
        return
    
    # Step 2: Fetch pre-resolution prices
    data_points = fetch_pre_resolution_prices(markets, max_fetch=3000)
    
    if len(data_points) < 50:
        print(f"ERROR: Only {len(data_points)} data points, need more!")
        return
    
    prices = np.array([d[0] for d in data_points])
    outcomes = np.array([d[1] for d in data_points])
    
    # Step 3: Print sample sizes
    print("\n" + "=" * 50)
    print("SAMPLE SIZES BY PRICE RANGE")
    print("=" * 50)
    
    bin_stats = []
    for lo, hi, label in [(0.50, 0.80, "0.50-0.80"), (0.80, 0.85, "0.80-0.85"), 
                           (0.85, 0.90, "0.85-0.90"), (0.90, 0.95, "0.90-0.95"), 
                           (0.95, 0.995, "0.95-0.99")]:
        mask = (prices >= lo) & (prices < hi)
        n = mask.sum()
        rate = outcomes[mask].mean() if n > 0 else 0
        print(f"  {label}: N={n:>5}, resolution rate={rate:.3f}")
        bin_stats.append((label, n, rate))
    
    print(f"\n  Total data points: {len(data_points)}")
    print(f"  Bond zone (0.80-0.99): {((prices >= 0.80) & (prices < 0.995)).sum()}")
    
    # Step 4: Fit models
    print("\n" + "=" * 50)
    print("FITTING CONTINUOUS MODELS")
    print("=" * 50)
    
    models = fit_models(prices, outcomes)
    
    if not models:
        print("ERROR: No models fit successfully!")
        return
    
    # Best model by AIC
    best_name = min(models, key=lambda k: models[k]['aic'])
    best = models[best_name]
    
    print(f"\n  BEST MODEL: {best_name} (AIC={best['aic']:.1f})")
    
    # Step 5: Print implied q at key prices
    print("\n" + "=" * 50)
    print("IMPLIED q AT KEY PRICES")
    print("=" * 50)
    
    key_prices_list = [0.85, 0.88, 0.90, 0.92, 0.95, 0.97, 0.99]
    key_price_results = []
    
    print(f"  {'Price':>6} | {'Empirical q':>12} | {'Flat Prior':>10} | {'Delta':>8}")
    print(f"  {'-'*6}-+-{'-'*12}-+-{'-'*10}-+-{'-'*8}")
    
    for p in key_prices_list:
        q = float(best['func'](np.array([p]), *best['params'])[0]) if hasattr(best['func'](np.array([p]), *best['params']), '__len__') else float(best['func'](p, *best['params']))
        delta = q - 0.952
        print(f"  {p:>6.2f} | {q:>12.4f} | {'0.9520':>10} | {delta:>+8.4f}")
        key_price_results.append((p, q))
    
    # Step 6: Compute empirical rates for plotting  
    bin_centers, bin_rates, bin_counts = compute_empirical_rates(data_points, n_bins=25)
    
    # Step 7: Plot
    make_plot(bin_centers, bin_rates, bin_counts, models, best_name)
    
    # Step 8: Write results
    write_results(models, best_name, key_price_results, bin_stats)
    
    print("\n" + "=" * 50)
    print("DONE!")
    print("=" * 50)


if __name__ == '__main__':
    main()
