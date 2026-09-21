# Screen a declared set of conversion comparisons

Use this code unchanged. Before seeing outcomes, select up to three defensible
non-identifying dimensions and at most eight categories per dimension. Each
comparison is one category against the rest of the same eligible funnel cohort,
using one independent actor per row and the dimension on its first step. Missing
dimensions and ambiguous first-step values remain explicit; never use arbitrary
`any()` attribution. Do not compare overlapping actors as independent samples.

Use a two-sided Fisher exact test (probability ordering), then Holm correction
across **all** declared category comparisons, including unavailable comparisons
as p=1. Report at most the strongest surviving difference, its denominators,
absolute percentage-point difference, p-value, adjusted p-value and test name.
Require at least 20 actors in each arm before interpretation. This is descriptive
screening, not causality or a guarantee that a small sample has sufficient power.
If none passes, say "No supported breakdown". Never choose a new family after
seeing the results. Large exact-test support is deliberately bounded; report it
as unavailable instead of substituting an unreviewed approximation.

References: [SciPy's two-sided definition](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.fisher_exact.html)
and [Holm correction](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html).
No third-party package is required in the sandbox.

```python
import math


def fisher_exact_two_sided(a, b, c, d):
    """[[converted_A, remaining_A], [converted_B, remaining_B]]."""
    if any(type(x) is not int or x < 0 for x in (a, b, c, d)):
        raise ValueError("counts must be nonnegative integers")
    n, row, col = a + b + c + d, a + b, a + c
    if row == 0 or row == n or n > 1000000:
        return None
    low, high = max(0, row + col - n), min(row, col)
    if high - low > 10000:
        return None

    def choose_log(total, count):
        return math.lgamma(total + 1) - math.lgamma(count + 1) - math.lgamma(total - count + 1)

    def probability_log(x):
        return choose_log(col, x) + choose_log(n - col, row - x) - choose_log(n, row)

    observed = probability_log(a)
    return min(
        1.0,
        math.fsum(
            math.exp(p)
            for x in range(low, high + 1)
            if (p := probability_log(x)) <= observed + 1e-9
        ),
    )


def holm(p_values):
    if any(
        type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in p_values
    ):
        raise ValueError("finite p-values required")
    result = [None] * len(p_values)
    previous = 0.0
    for rank, index in enumerate(sorted(range(len(p_values)), key=p_values.__getitem__)):
        previous = max(previous, min(1.0, (len(p_values) - rank) * p_values[index]))
        result[index] = previous
    return result


def screen_comparisons(tables):
    if not 1 <= len(tables) <= 24:
        raise ValueError("declare 1-24 comparisons")
    values = [fisher_exact_two_sided(*table) for table in tables]
    adjusted = holm([p if p is not None else 1.0 for p in values])
    return [
        {
            "p": p,
            "adjusted_p": adj,
            "difference_pp": None
            if a + b == 0 or c + d == 0
            else 100.0 * (a / (a + b) - c / (c + d)),
            "supported": p is not None and a + b >= 20 and c + d >= 20 and adj <= 0.05,
        }
        for (a, b, c, d), p, adj in zip(tables, values, adjusted, strict=True)
    ]
```
