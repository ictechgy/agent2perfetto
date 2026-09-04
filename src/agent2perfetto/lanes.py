"""Context-lane computation.

The context lane approximates how much conversation the model carried at each
assistant turn by summing the token-usage fields the log reports per call.
These are cumulative sums, not provider-side context-window measurements.
"""

from __future__ import annotations

from dataclasses import dataclass

LANE_COUNTERS = ("ctx_total", "ctx_input", "ctx_cache_read", "ctx_cache_create")

APPROXIMATION_NOTE = (
    "ctx_* counters are cumulative sums of the usage fields reported per "
    "assistant call (input, cache_read, cache_creation tokens). They "
    "approximate context-window occupancy and are not provider measurements."
)


@dataclass
class LaneSample:
    epoch_us: int
    session_id: str
    values: dict


def compute_context_lanes(records) -> list:
    """Return one LaneSample per assistant record, with cumulative token sums."""
    cum = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    samples = []
    ordered = sorted(
        (r for r in records if r.type == "assistant"),
        key=lambda r: (r.epoch_us, r.seq),
    )
    for r in ordered:
        for key in cum:
            cum[key] += r.usage.get(key, 0)
        values = {
            "ctx_input": cum["input_tokens"],
            "ctx_cache_read": cum["cache_read_input_tokens"],
            "ctx_cache_create": cum["cache_creation_input_tokens"],
        }
        values["ctx_total"] = (
            values["ctx_input"] + values["ctx_cache_read"] + values["ctx_cache_create"]
        )
        samples.append(LaneSample(epoch_us=r.epoch_us, session_id=r.session_id, values=values))
    return samples
