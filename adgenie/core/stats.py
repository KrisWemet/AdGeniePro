"""Small, dependency-free Bayesian statistics used by the optimizer.

Ad optimization fails in two directions. Kill a good ad on three unlucky days
and you burn the winner; keep a bad one because "it might turn around" and you
burn the budget. Both are the same mistake: acting on a point estimate that has
no error bar attached. Everything here exists to put an error bar on a rate
computed from a handful of clicks.

Conversion rate is modelled as Beta-Binomial. The prior is weak
(Beta(1, 1) by default) but can be seeded from an offer's historical EPC so a
brand-new ad is not judged from a blank slate.

No numpy or scipy: this runs anywhere the API runs.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

__all__ = [
    "Interval",
    "beta_cdf",
    "beta_ppf",
    "beta_interval",
    "prob_rate_above",
    "prob_b_beats_a",
    "thompson_sample_beta",
    "wilson_interval",
    "expected_loss_choosing",
]

_EPS = 1e-12
_MAX_ITER = 300


@dataclass(frozen=True)
class Interval:
    """A credible interval plus the point estimate it surrounds."""

    lower: float
    mean: float
    upper: float
    level: float

    def as_dict(self) -> dict:
        return {
            "lower": self.lower,
            "mean": self.mean,
            "upper": self.upper,
            "level": self.level,
        }


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _EPS:
        d = _EPS
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _EPS:
            d = _EPS
        c = 1.0 + aa / c
        if abs(c) < _EPS:
            c = _EPS
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _EPS:
            d = _EPS
        c = 1.0 + aa / c
        if abs(c) < _EPS:
            c = _EPS
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a, b) == P(X <= x) for X ~ Beta(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if a <= 0 or b <= 0:
        raise ValueError("beta parameters must be positive")
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        b * math.log1p(-x) + a * math.log(x) - _log_beta(b, a)
    ) * _betacf(b, a, 1.0 - x) / b


def beta_ppf(p: float, a: float, b: float, tol: float = 1e-10) -> float:
    """Inverse CDF by bisection. Monotone, so bisection is fast and safe."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be in [0, 1]")
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if beta_cdf(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def beta_interval(
    successes: float,
    trials: float,
    level: float = 0.90,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
) -> Interval:
    """Equal-tailed credible interval for a rate.

    `successes` may be fractional (platform conversion counts often are).
    """
    successes = max(0.0, float(successes))
    trials = max(successes, float(trials))
    a = prior_a + successes
    b = prior_b + (trials - successes)
    tail = (1.0 - level) / 2.0
    return Interval(
        lower=beta_ppf(tail, a, b),
        mean=a / (a + b),
        upper=beta_ppf(1.0 - tail, a, b),
        level=level,
    )


def prob_rate_above(
    threshold: float,
    successes: float,
    trials: float,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
) -> float:
    """P(true rate > threshold) given the observed data.

    This is the question a kill rule actually asks: not "is the measured
    conversion rate below breakeven" but "how sure am I that the *true* rate
    is below breakeven".
    """
    if threshold <= 0.0:
        return 1.0
    if threshold >= 1.0:
        return 0.0
    successes = max(0.0, float(successes))
    trials = max(successes, float(trials))
    a = prior_a + successes
    b = prior_b + (trials - successes)
    return 1.0 - beta_cdf(threshold, a, b)


def thompson_sample_beta(
    successes: float,
    trials: float,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
    rng: random.Random | None = None,
) -> float:
    """Draw one sample from the posterior. Used for budget allocation.

    Sampling instead of ranking by mean is what keeps a promising-but-unproven
    creative funded: its posterior is wide, so it occasionally samples high and
    earns another slice of budget.
    """
    rng = rng or random
    a = prior_a + max(0.0, float(successes))
    b = prior_b + max(0.0, float(trials) - float(successes))
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    total = x + y
    return x / total if total > 0 else 0.0


def prob_b_beats_a(
    a_successes: float,
    a_trials: float,
    b_successes: float,
    b_trials: float,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
    samples: int = 20000,
    rng: random.Random | None = None,
) -> float:
    """P(variant B has a higher true rate than variant A).

    Uses the exact closed form when the posterior counts are small integers,
    and Monte Carlo otherwise (the exact sum is O(alpha_b) terms).
    """
    aa = prior_a + a_successes
    ab = prior_b + (a_trials - a_successes)
    ba = prior_a + b_successes
    bb = prior_b + (b_trials - b_successes)

    integral_ok = all(float(v).is_integer() for v in (aa, ab, ba, bb))
    if integral_ok and ba <= 300 and aa <= 300 and ab <= 3000 and bb <= 3000:
        total = 0.0
        for i in range(int(ba)):
            total += math.exp(
                _log_beta(aa + i, ab + bb)
                - math.log(bb + i)
                - _log_beta(1 + i, bb)
                - _log_beta(aa, ab)
            )
        return min(1.0, max(0.0, total))

    rng = rng or random
    wins = 0
    for _ in range(samples):
        if thompson_sample_beta(b_successes, b_trials, prior_a, prior_b, rng) > (
            thompson_sample_beta(a_successes, a_trials, prior_a, prior_b, rng)
        ):
            wins += 1
    return wins / samples


def expected_loss_choosing(
    chosen_successes: float,
    chosen_trials: float,
    rival_successes: float,
    rival_trials: float,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
    samples: int = 20000,
    rng: random.Random | None = None,
) -> float:
    """Expected regret (in rate points) from picking `chosen` over `rival`.

    A test can stop when this drops below a threshold you are willing to lose,
    which is a far better stopping rule than "p < 0.05".
    """
    rng = rng or random
    loss = 0.0
    for _ in range(samples):
        c = thompson_sample_beta(chosen_successes, chosen_trials, prior_a, prior_b, rng)
        r = thompson_sample_beta(rival_successes, rival_trials, prior_a, prior_b, rng)
        loss += max(0.0, r - c)
    return loss / samples


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> Interval:
    """Frequentist interval for CTR. Cheap and stable at small n."""
    if trials <= 0:
        return Interval(0.0, 0.0, 1.0, 0.95)
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    margin = (
        z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    ) / denom
    return Interval(
        lower=max(0.0, center - margin),
        mean=p,
        upper=min(1.0, center + margin),
        level=0.95,
    )
