# Kelly Prior: Empirical Fit from Polymarket Historical Data

*Generated: 2026-02-25 00:01*

## Methodology

- Pulled thousands of resolved binary markets from the Gamma API
- Fetched daily price history from CLOB API for each market
- Used the price **1 day before resolution** as the pre-resolution price
- Filtered to markets with prices in the 0.50-0.99 range
- Fit continuous functions: logistic, power law, beta CDF, cubic polynomial
- Selected best model by AIC (log-likelihood based)

## Sample Sizes by Price Range

| Price Range | N | Resolution Rate |
|---|---|---|
| 0.50-0.80 | 229 | 0.651 |
| 0.80-0.85 | 27 | 0.926 |
| 0.85-0.90 | 17 | 0.882 |
| 0.90-0.95 | 33 | 0.970 |
| 0.95-0.99 | 143 | 0.993 |

## Model Comparison

| Model | Parameters | AIC | Log-Likelihood |
|---|---|---|---|
| beta_cdf ✅ | a=1.4545, b=1.4895 | 334.8 | -165.4 |
| power | k=0.8800 | 336.5 | -167.2 |
| poly3 | a=3.2040, b=-8.0186, c=7.4941, d=-1.6540 | 337.7 | -164.9 |
| logistic | a=6.8895, b=0.5120 | 344.1 | -170.1 |

## Best Model: **beta_cdf**

```
P(YES) = BetaCDF(price; a=1.4545, b=1.4895)
```

## Implied q at Key Prices

| Price | Empirical q | Flat Prior (0.952) | Delta |
|---|---|---|---|
| 0.85 | 0.9077 | 0.9520 | -0.0443 |
| 0.88 | 0.9332 | 0.9520 | -0.0188 |
| 0.90 | 0.9488 | 0.9520 | -0.0032 |
| 0.92 | 0.9631 | 0.9520 | +0.0111 |
| 0.95 | 0.9815 | 0.9520 | +0.0295 |
| 0.97 | 0.9913 | 0.9520 | +0.0393 |
| 0.99 | 0.9983 | 0.9520 | +0.0463 |

## Recommendation

Replace the flat `q_mean = 0.952` prior with the **beta_cdf** function.
This gives a price-dependent prior that better reflects the empirical data:
- Lower prices (0.80-0.90) → lower resolution probability → less aggressive buying
- Higher prices (0.95+) → higher resolution probability → appropriate aggression

### Integration Code

```python
from scipy.stats import beta as beta_dist

def price_to_q(price: float) -> float:
    """Empirical prior: price → P(resolve YES)"""
    return float(beta_dist.cdf(price, 1.454489, 1.489500))
```

### Usage in Kelly calculation

```python
# Replace: q_mean = 0.952
# With:
q_mean = price_to_q(yes_price)
```