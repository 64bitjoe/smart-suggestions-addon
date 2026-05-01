from candidate import Candidate, MinerType


def test_candidate_signature_is_stable():
    c1 = Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id="light.kitchen",
        action="turn_on",
        details={"hour": 6, "minute": 45, "weekdays": [0, 1, 2, 3, 4]},
        occurrences=12,
        conditional_prob=0.85,
    )
    c2 = Candidate(
        miner_type=MinerType.TEMPORAL,
        entity_id="light.kitchen",
        action="turn_on",
        details={"weekdays": [0, 1, 2, 3, 4], "minute": 45, "hour": 6},  # different key order
        occurrences=99,  # different measurement, same pattern identity
        conditional_prob=0.99,
    )
    assert c1.signature() == c2.signature()


def test_candidate_signature_includes_miner_type_entity_action_and_key_details():
    c = Candidate(
        miner_type=MinerType.SEQUENCE,
        entity_id="light.lamp_a",
        action="turn_on",
        details={"target_entity": "light.lamp_b", "target_action": "turn_on", "delta_seconds": 30},
        occurrences=8,
        conditional_prob=0.9,
    )
    sig = c.signature()
    assert "sequence" in sig
    assert "light.lamp_a" in sig
    assert "light.lamp_b" in sig
