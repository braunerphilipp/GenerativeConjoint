"""
Conjoint design generation using coordinate exchange to maximise D-efficiency.

Algorithm
---------
1. Initialise a level-balanced random design (each level of each attribute appears
   equally often across all profiles).
2. Apply coordinate exchange: for every cell (profile × attribute), try all possible
   levels and keep the one that maximises det(X'X), the information-matrix determinant.
3. Repeat from a new random start `n_starts` times; keep the best design found.

Encoding
--------
Effect coding is used: for an attribute with m levels, levels 0..m-2 get a +1 in
their own column; level m-1 gets -1 in all m-1 columns.  This is centred and produces
an orthogonal baseline for the intercept, which is required for the D-efficiency metric
to be meaningful across designs with different attribute structures.

Sample-size estimation
----------------------
Based on Orme (2010) heuristic: n * T * J >= 500 * L
where T = tasks, J = alternatives per task, L = max levels in any single attribute.
Rearranged: n >= ceil(500 * L / (T * J)).
A recommended target of 1.5 × that minimum is returned as well.
"""

import numpy as np
import random
from typing import List, Tuple


def generate_design(
    attributes_levels: List[int],
    num_tasks: int,
    num_alternatives: int,
    n_starts: int = 20,
) -> Tuple[np.ndarray, float]:
    """
    Return a D-efficient design and its D-efficiency score.

    Parameters
    ----------
    attributes_levels : list of int
        Number of levels for each attribute (in attribute order).
    num_tasks : int
    num_alternatives : int
    n_starts : int
        Number of independent random starts for the coordinate exchange.

    Returns
    -------
    design : np.ndarray, shape (num_tasks, num_alternatives, num_attributes)
        Level indices (0-based) for each cell.
    d_efficiency : float
        D-efficiency of the returned design (0–1 scale relative to orthogonal ideal).
    """
    n_attrs = len(attributes_levels)
    n_profiles = num_tasks * num_alternatives

    if n_profiles == 0 or n_attrs == 0:
        raise ValueError("Need at least one task, one alternative, and one attribute.")

    best_design: np.ndarray | None = None
    best_d_eff = -1.0

    for _ in range(n_starts):
        D = _init_balanced(attributes_levels, n_profiles)
        D = _coordinate_exchange(D, attributes_levels)
        d_eff = _d_efficiency(D, attributes_levels)
        if d_eff > best_d_eff:
            best_d_eff = d_eff
            best_design = D.copy()

    result = best_design.reshape(num_tasks, num_alternatives, n_attrs)
    return result, best_d_eff


def estimate_sample_size(
    num_tasks: int,
    num_alternatives: int,
    max_levels: int,
) -> Tuple[int, int]:
    """
    Return (minimum_n, recommended_n) for a conjoint study.

    Uses the Orme (2010) heuristic: n >= 500 * L / (T * J).
    The recommended value applies a 1.5× safety factor.
    Raw formula values are returned; callers should note the practical minimum
    of ~200 respondents for stable estimates.
    """
    if num_tasks <= 0 or num_alternatives <= 0:
        return 0, 0
    raw = 500 * max_levels / (num_tasks * num_alternatives)
    minimum = int(np.ceil(raw))
    recommended = int(np.ceil(raw * 1.5))
    return minimum, recommended


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _init_balanced(attributes_levels: List[int], n_profiles: int) -> np.ndarray:
    """Level-balanced random initialisation."""
    n_attrs = len(attributes_levels)
    D = np.zeros((n_profiles, n_attrs), dtype=int)
    for j, nl in enumerate(attributes_levels):
        repeats, remainder = divmod(n_profiles, nl)
        seq = list(range(nl)) * repeats + list(range(remainder))
        random.shuffle(seq)
        D[:, j] = seq
    return D


def _effects_encode(D: np.ndarray, attributes_levels: List[int]) -> np.ndarray:
    """Construct the effects-coded X matrix from a raw design matrix."""
    n_profiles, _ = D.shape
    n_params = 1 + sum(nl - 1 for nl in attributes_levels)
    X = np.zeros((n_profiles, n_params))
    X[:, 0] = 1.0  # intercept

    col = 1
    for j, nl in enumerate(attributes_levels):
        for lv in range(nl - 1):
            mask_pos = D[:, j] == lv
            mask_neg = D[:, j] == nl - 1
            X[mask_pos, col] = 1.0
            X[mask_neg, col] = -1.0
            col += 1

    return X


def _log_det_XtX(D: np.ndarray, attributes_levels: List[int]) -> float:
    """Compute log|X'X| for the design (returns -inf if singular)."""
    X = _effects_encode(D, attributes_levels)
    XtX = X.T @ X
    sign, logdet = np.linalg.slogdet(XtX)
    return logdet if sign > 0 else -np.inf


def _d_efficiency(D: np.ndarray, attributes_levels: List[int]) -> float:
    """
    D-efficiency in [0, 1] relative to a hypothetical orthogonal design.
    Computed as exp(logdet / p) / n, normalised so an orthogonal design = 1.
    """
    X = _effects_encode(D, attributes_levels)
    n, p = X.shape
    XtX = X.T @ X
    sign, logdet = np.linalg.slogdet(XtX)
    if sign <= 0:
        return 0.0
    return float(np.exp(logdet / p) / n)


def _coordinate_exchange(
    D: np.ndarray,
    attributes_levels: List[int],
    max_iter: int = 100,
) -> np.ndarray:
    """
    Coordinate exchange: iterate over every (profile, attribute) cell and
    choose the level that maximises det(X'X).  Stop when no cell improves.
    """
    n_profiles, n_attrs = D.shape

    for _ in range(max_iter):
        improved = False
        # Randomise iteration order to avoid systematic bias
        order = list(range(n_profiles))
        random.shuffle(order)

        for i in order:
            for j, nl in enumerate(attributes_levels):
                if nl == 1:
                    continue
                current = D[i, j]
                best_lv = current
                best_ld = _log_det_XtX(D, attributes_levels)

                for lv in range(nl):
                    if lv == current:
                        continue
                    D[i, j] = lv
                    ld = _log_det_XtX(D, attributes_levels)
                    if ld > best_ld:
                        best_ld = ld
                        best_lv = lv

                D[i, j] = best_lv
                if best_lv != current:
                    improved = True

        if not improved:
            break

    return D
