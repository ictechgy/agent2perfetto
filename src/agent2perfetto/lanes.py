"""Context-lane computation.

Two counter families per assistant call:

* ``ctx_*`` (occupancy) — the input / cache_read / cache_creation tokens
  that *that single call* reported. Their sum approximates the context the
  model carried on that turn.
* ``spend_*`` — cumulative sums of the same fields across the session,
  i.e. the billing trajectory. cache_read re-bills the same cached tokens
  every call, so spend grows monotonically and is NOT context size.

Both derive from client-reported usage; neither is a provider-side
context-window measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import KIND_MODEL_CALL

OCC_COUNTERS = ("ctx_total", "ctx_input", "ctx_cache_read", "ctx_cache_create")
SPEND_COUNTERS = ("spend_total", "spend_input", "spend_cache_read", "spend_cache_create")

APPROXIMATION_NOTE = (
    "ctx_* counters estimate per-call context occupancy from the usage "
    "fields that assistant call reported (input + cache_read + "
    "cache_creation tokens). spend_* counters are cumulative sums of the "
    "same fields over the session (billing trajectory, not context size). "
    "Both come from client-reported usage, not provider measurements; "
    "treat them as approximate."
)


@dataclass
class LaneSample:
    epoch_us: int
    session_id: str
    values: dict


def compute_context_lanes(turns) -> list:
    """Return one LaneSample per model-call turn with occupancy + spend values.

    Turns with an empty usage dict ({} — the adapter had no usage to report,
    e.g. a synthesized turn that only carries tool calls) produce no sample:
    "unknown" must not draw the counter to zero. A reported all-zeros usage
    still emits a zero sample (the call claimed those zeros).
    """
    cum = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    samples = []
    ordered = sorted(
        (
            t
            for t in turns
            if getattr(t, "kind", None) == KIND_MODEL_CALL and t.usage
        ),
        key=lambda t: (t.epoch_us, t.seq),
    )
    for t in ordered:
        call = {
            "input_tokens": t.usage.get("input_tokens", 0),
            "cache_creation_input_tokens": t.usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": t.usage.get("cache_read_input_tokens", 0),
        }
        values = {
            "ctx_input": call["input_tokens"],
            "ctx_cache_read": call["cache_read_input_tokens"],
            "ctx_cache_create": call["cache_creation_input_tokens"],
        }
        values["ctx_total"] = (
            values["ctx_input"] + values["ctx_cache_read"] + values["ctx_cache_create"]
        )
        for key in cum:
            cum[key] += call[key]
        values["spend_input"] = cum["input_tokens"]
        values["spend_cache_read"] = cum["cache_read_input_tokens"]
        values["spend_cache_create"] = cum["cache_creation_input_tokens"]
        values["spend_total"] = (
            values["spend_input"] + values["spend_cache_read"] + values["spend_cache_create"]
        )
        samples.append(LaneSample(epoch_us=t.epoch_us, session_id=t.session_id, values=values))
    return samples
