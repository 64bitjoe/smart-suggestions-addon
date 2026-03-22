"""Shared domain constants for Smart Suggestions add-on."""

# Domains that are usually uninteresting for suggestions
_SKIP_DOMAINS = {
    "sun", "zone", "updater", "persistent_notification", "person",
    "device_tracker",
}

# Domains that appear in entity_states (context) but NOT in available_actions
_CONTEXT_ONLY_DOMAINS = {"sensor", "weather", "binary_sensor"}

# Domains that ARE interesting as potential actions
_ACTION_DOMAINS = {
    "light", "switch", "climate", "media_player", "cover",
    "fan", "lock", "vacuum", "input_boolean", "automation", "script", "scene",
}

# States that count as "inactive" for dormancy filtering
_INACTIVE_STATES = {"off", "idle", "paused", "standby", "closed", "locked"}

# Feedback scoring constants
FEEDBACK_UPVOTE_MULTIPLIER = 8   # score boost per net upvote
FEEDBACK_DOWNVOTE_MULTIPLIER = 10  # score penalty per net downvote
FEEDBACK_HARD_EXCLUDE_THRESHOLD = -3  # net votes at or below this → hard exclude
