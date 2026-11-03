import itertools

import numpy as np
import pytest

from bili_asr.asr.constrained_alignment import decode_timestamp_path


def test_path_matches_exhaustive_maximum_and_preserves_scores():
    scores = np.random.default_rng(71).normal(size=(4, 5))
    original = scores.copy()
    path, evidence = decode_timestamp_path(scores, duration_seconds=.4, step_seconds=.1)
    valid = [p for p in itertools.product(range(5), repeat=4)
             if p[0] < p[1] <= p[2] < p[3]]
    best = max(sum(scores[i, p[i]] for i in range(4)) for p in valid)
    assert sum(scores[i, path[i]] for i in range(4)) == pytest.approx(best)
    assert evidence['algorithm'] == 'monotone-max-score/v1'
    np.testing.assert_array_equal(scores, original)


def test_selects_real_classes_instead_of_interpolating_equal_argmaxes():
    scores = np.array([[0, 4, 1, 0], [0, 5, 3, 0]], dtype=float)
    path, evidence = decode_timestamp_path(scores, duration_seconds=.3, step_seconds=.1)
    assert path == [1, 2]
    assert evidence['changed_argmax_positions'] == 1


@pytest.mark.parametrize('scores,duration,step', [
    (np.zeros((3, 5)), 1, .1), (np.zeros((4, 2)), .1, .1),
    (np.array([[float('nan'), 1], [2, 3]]), 1, .1),
    (np.zeros((2, 5)), -1, .1), (np.zeros((2, 5)), 1, 0),
])
def test_invalid_or_infeasible_lattice_is_rejected(scores, duration, step):
    with pytest.raises(ValueError):
        decode_timestamp_path(scores, duration_seconds=duration, step_seconds=step)
