from const import _ACTION_DOMAINS, _SKIP_DOMAINS, _CONTEXT_ONLY_DOMAINS, _INACTIVE_STATES


def test_action_domains_contains_expected():
    assert "light" in _ACTION_DOMAINS
    assert "switch" in _ACTION_DOMAINS
    assert "scene" in _ACTION_DOMAINS
    assert "climate" in _ACTION_DOMAINS


def test_skip_domains_do_not_overlap_action_domains():
    assert _SKIP_DOMAINS.isdisjoint(_ACTION_DOMAINS)


def test_context_only_do_not_overlap_action_domains():
    assert _CONTEXT_ONLY_DOMAINS.isdisjoint(_ACTION_DOMAINS)


def test_inactive_states():
    assert "off" in _INACTIVE_STATES
    assert "locked" in _INACTIVE_STATES
