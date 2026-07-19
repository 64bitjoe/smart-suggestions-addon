from lifecycle import (
    passes_emerging, confirm_gate, passes_confirmed, should_resurface,
)


def test_emerging_gate():
    assert passes_emerging(2, 0.4)
    assert not passes_emerging(1, 0.9)
    assert not passes_emerging(5, 0.39)


def test_confirm_gate_full_history():
    # 30 days of history → the mature bar: 5 occurrences, 0.6 prob
    assert confirm_gate(30.0, 0) == (5, 0.6)


def test_confirm_gate_young_install():
    # Day-5 install → floor of 3 occurrences, not 5
    min_occ, min_prob = confirm_gate(5.0, 0)
    assert min_occ == 3
    assert min_prob == 0.6


def test_confirm_gate_dismissal_bump_and_cap():
    assert confirm_gate(30.0, 3)[1] == 0.65
    assert confirm_gate(30.0, 5)[1] == 0.65      # bump per full group of 3
    assert confirm_gate(30.0, 100)[1] == 0.9     # capped


def test_passes_confirmed_day5_install_confirms_something():
    # The exact failure v3 had: young install, decent evidence → must confirm
    assert passes_confirmed(3, 0.7, history_days=5.0, dismissals_7d=0)
    assert not passes_confirmed(2, 0.7, history_days=5.0, dismissals_7d=0)


def test_should_resurface():
    assert should_resurface(0.75, 0.55, days_since_dismissal=1)   # prob rose ≥0.15
    assert not should_resurface(0.60, 0.55, days_since_dismissal=1)
    assert should_resurface(0.10, 0.55, days_since_dismissal=31)  # 30-day expiry
