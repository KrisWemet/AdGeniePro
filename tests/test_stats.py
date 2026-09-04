"""The statistics have to be right; every optimizer decision rests on them."""

from __future__ import annotations

import math
import random

import pytest

from adgenie.core.stats import (
    beta_cdf,
    beta_interval,
    beta_ppf,
    expected_loss_choosing,
    prob_b_beats_a,
    prob_rate_above,
    thompson_sample_beta,
    wilson_interval,
)


def test_beta_cdf_uniform_case():
    # Beta(1, 1) is the uniform distribution, so its CDF is the identity.
    for x in (0.0, 0.1, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert beta_cdf(x, 1, 1) == pytest.approx(x, abs=1e-9)


def test_beta_cdf_is_monotone_and_bounded():
    previous = 0.0
    for i in range(1, 100):
        value = beta_cdf(i / 100, 3, 7)
        assert 0.0 <= value <= 1.0
        assert value >= previous
        previous = value


def test_beta_cdf_matches_known_symmetry():
    # I_x(a, b) == 1 - I_(1-x)(b, a)
    for a, b, x in ((2, 5, 0.3), (7, 3, 0.62), (1, 9, 0.05)):
        assert beta_cdf(x, a, b) == pytest.approx(1 - beta_cdf(1 - x, b, a), abs=1e-9)


def test_beta_ppf_inverts_cdf():
    for a, b in ((2, 8), (15, 400), (1, 1)):
        for p in (0.05, 0.25, 0.5, 0.9, 0.975):
            x = beta_ppf(p, a, b)
            assert beta_cdf(x, a, b) == pytest.approx(p, abs=1e-6)


def test_credible_interval_narrows_with_more_data():
    small = beta_interval(2, 100, 0.90)
    large = beta_interval(20, 1000, 0.90)
    assert small.mean == pytest.approx(large.mean, abs=0.02)
    assert (small.upper - small.lower) > (large.upper - large.lower) * 2


def test_interval_brackets_the_mean():
    interval = beta_interval(37, 900, 0.9)
    assert interval.lower < interval.mean < interval.upper


def test_prob_rate_above_falls_as_evidence_accumulates():
    # Zero conversions: the more clicks, the less plausible a 2% true rate is.
    probabilities = [prob_rate_above(0.02, 0, n) for n in (10, 50, 200, 800)]
    assert probabilities == sorted(probabilities, reverse=True)
    assert probabilities[0] > 0.5
    assert probabilities[-1] < 0.01


def test_prob_rate_above_edge_cases():
    assert prob_rate_above(0.0, 0, 100) == 1.0
    assert prob_rate_above(1.0, 5, 100) == 0.0


def test_informative_prior_shrinks_a_small_sample():
    """A flat prior badly over-rates one conversion in ten clicks."""
    flat = beta_interval(1, 10, 0.9)
    informed = beta_interval(1, 10, 0.9, prior_a=0.5, prior_b=24.5)
    assert flat.mean > 0.10
    assert informed.mean < 0.06


def test_prob_b_beats_a_is_symmetric():
    forward = prob_b_beats_a(5, 200, 12, 200)
    backward = prob_b_beats_a(12, 200, 5, 200)
    assert forward + backward == pytest.approx(1.0, abs=1e-6)


def test_prob_b_beats_a_identical_arms_is_half():
    assert prob_b_beats_a(10, 300, 10, 300) == pytest.approx(0.5, abs=1e-6)


def test_prob_b_beats_a_exact_matches_monte_carlo():
    rng = random.Random(7)
    exact = prob_b_beats_a(8, 250, 20, 250)
    sampled = prob_b_beats_a(8.5, 250.5, 20.5, 250.5, samples=40000, rng=rng)
    assert abs(exact - sampled) < 0.03


def test_thompson_samples_are_valid_probabilities():
    rng = random.Random(1)
    samples = [thompson_sample_beta(20, 400, rng=rng) for _ in range(400)]
    assert all(0.0 <= s <= 1.0 for s in samples)
    assert sum(samples) / len(samples) == pytest.approx(0.05, abs=0.01)


def test_flat_prior_lets_a_tiny_sample_outrank_a_proven_arm():
    """Why the platform never uses Beta(1, 1) for allocation.

    One conversion in twenty clicks reads as a 9% rate under a flat prior,
    which beats a genuine 5% arm measured over five hundred clicks. Left
    uncorrected this hands the budget to whichever ad got lucky first.
    """
    rng = random.Random(3)
    unproven = sum(
        thompson_sample_beta(1, 20, rng=rng) > thompson_sample_beta(25, 500, rng=rng)
        for _ in range(2000)
    )
    assert unproven > 1000


def test_informative_prior_ranks_the_proven_arm_higher_but_still_explores():
    """With the pooled prior the ordering is right and exploration survives."""
    rng = random.Random(3)
    prior_a, prior_b = 1.25, 23.75
    unproven = sum(
        thompson_sample_beta(1, 20, prior_a, prior_b, rng=rng)
        > thompson_sample_beta(25, 500, prior_a, prior_b, rng=rng)
        for _ in range(2000)
    )
    assert unproven < 1000, "the proven arm should usually win"
    assert unproven > 100, "but the unproven arm must still get explored"


def test_expected_loss_is_small_when_choosing_the_better_arm():
    rng = random.Random(5)
    loss = expected_loss_choosing(60, 1000, 20, 1000, samples=8000, rng=rng)
    assert loss < 0.005


def test_wilson_interval_handles_zero_and_full():
    assert wilson_interval(0, 100).lower == 0.0
    assert wilson_interval(100, 100).upper == pytest.approx(1.0, abs=1e-6)
    assert wilson_interval(0, 0).mean == 0.0


def test_beta_cdf_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        beta_cdf(0.5, 0, 1)
    with pytest.raises(ValueError):
        beta_ppf(1.5, 1, 1)
