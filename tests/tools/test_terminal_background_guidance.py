"""Contract tests for choosing foreground versus background execution."""

from tools import terminal_tool


def test_terminal_guidance_keeps_decision_gates_in_foreground():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION
    background_help = terminal_tool.TERMINAL_SCHEMA["parameters"]["properties"][
        "background"
    ]["description"]

    for text in (description, background_help):
        assert "result is needed for the current turn's next decision" in text
        assert (
            "Expected duration alone does not justify background when bounded work "
            "fits within that limit"
        ) in text
        assert "semantically independent" in text
        assert "may exceed the 600-second foreground limit" in text
        assert 'Do not use background followed by process(action="wait")' in text


def test_terminal_guidance_preserves_notify_on_complete_for_bounded_background_work():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION
    background_help = terminal_tool.TERMINAL_SCHEMA["parameters"]["properties"][
        "background"
    ]["description"]

    for text in (description, background_help):
        assert "Bounded background work MUST set notify_on_complete=true" in text
