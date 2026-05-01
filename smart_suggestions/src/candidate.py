from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MinerType(str, Enum):
    TEMPORAL = "temporal"
    SEQUENCE = "sequence"
    CROSS_AREA = "cross_area"
    WASTE = "waste"


# Keys that participate in pattern identity per miner type. occurrences/probability
# are *measurements* of the pattern, not part of its identity.
_SIG_KEYS: dict[MinerType, tuple[str, ...]] = {
    MinerType.TEMPORAL: ("hour", "minute", "weekdays"),
    MinerType.SEQUENCE: ("target_entity", "target_action", "delta_seconds"),
    MinerType.CROSS_AREA: ("trigger_entity", "latency_bucket"),
    MinerType.WASTE: ("condition",),
}


@dataclass
class Candidate:
    miner_type: MinerType
    entity_id: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)
    occurrences: int = 0
    # Set by miners. P(action | trigger) — the raw conditional probability the miner measured.
    conditional_prob: float = 0.0
    # Set by CandidateFilter after multi-criteria gating. Reserved for future use.
    confidence: float = 0.0

    def signature(self) -> str:
        """Stable identity hash used for LLM cache keys and dismissal matching.

        Format: {miner}:{entity}:{action}:{detail_val...}:{digest}

        The detail values for the miner's identity keys are included in the
        human-readable prefix so callers can inspect the pattern from the key
        alone (e.g. for logging or dismissal matching). Lists are sorted and
        joined with '-' for stability. The SHA1 digest at the tail is computed
        over the full canonical-JSON identity payload, so dict-key order
        differences in `details` produce the same signature.

        CONTRACT: Treat the signature as an opaque key. Do not split or parse
        it — entity IDs and detail values may contain ':' or '-' separators in
        the future. Equality comparison and substring search are safe; structural
        parsing is not.
        """
        keys = _SIG_KEYS.get(self.miner_type, ())
        identity = {k: self.details.get(k) for k in keys}
        for k, v in identity.items():
            if isinstance(v, list):
                identity[k] = sorted(v)
        payload = {
            "miner": self.miner_type.value,
            "entity": self.entity_id,
            "action": self.action,
            "id": identity,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha1(blob.encode()).hexdigest()[:16]
        detail_parts_list = []
        for k in keys:
            v = identity[k]
            if isinstance(v, list):
                detail_parts_list.append("-".join(str(x) for x in v))
            else:
                detail_parts_list.append(str(v))
        detail_parts = ":".join(detail_parts_list)
        prefix = f"{self.miner_type.value}:{self.entity_id}:{self.action}"
        if detail_parts:
            prefix = f"{prefix}:{detail_parts}"
        return f"{prefix}:{digest}"
