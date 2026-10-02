def test_ci1_control_case_intentional_failure():
    """Deliberately broken control case for the CI-1 card.

    This test exists only to prove that the pytest gate actually fires on a
    real failure, instead of trivially passing on an empty test suite. It is
    expected to turn this CI run RED. A follow-up commit removes this file
    once the red run is captured as evidence; the gate is not considered
    proven by a green run alone (see CI-1 acceptance: "Zöld kapu önmagában
    nem bizonyíték").
    """
    assert 1 == 2, "intentional control-case failure for CI gate verification"
