"""Moss fork persistent-memory surface boundary (routing env is not admission)."""
import os
from utils import is_truthy_value

_HUMAN_MEMORY_SURFACES = frozenset({"telegram", "webui"})


def _should_skip_memory_for_runtime(*, platform=None, explicit_skip_memory=False,
                                    session_id=None, gateway_session_key=None):
    if explicit_skip_memory or any(is_truthy_value(os.environ.get(name)) for name in (
        "HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG",
    )):
        return True
    surface = str(platform or "").strip().lower().replace("-", "_")
    from agent.moss_memory_gate import moss_runtime, eligible
    if moss_runtime() and surface == "api_server":
        return not eligible({"platform": surface, "agent_context": "primary",
                             "session_id": session_id, "gateway_session_key": gateway_session_key})
    return surface not in _HUMAN_MEMORY_SURFACES
