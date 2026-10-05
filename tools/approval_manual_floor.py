"""Port the existing fork manual floor from b81e879305 to modular approval."""
import os
import re
from typing import Optional
from tools.approval_detection import (
    _HERMES_CONFIG_PATH, _HERMES_ENV_PATH, _PROJECT_SENSITIVE_WRITE_TARGET,
    _iter_top_level_shell_segments, _shell_segment_tokens,
    _iter_shell_command_word_spans, _deobfuscate_shell_word_for_detection,
    _command_detection_variants,
)

_SMART_FLOOR_CONFIG_TARGET_RE = re.compile(
    rf"(?:{_HERMES_CONFIG_PATH}|{_HERMES_ENV_PATH}|"
    rf"{_PROJECT_SENSITIVE_WRITE_TARGET}|(?:.*/)?compose\.ya?ml)",
    re.IGNORECASE,
)


def _is_smart_floor_config_target(token: str) -> bool:
    return bool(_SMART_FLOOR_CONFIG_TARGET_RE.fullmatch(token))


def _short_or_long_option_present(args: list[str], short: str, long: str) -> bool:
    for token in args:
        if token == "--":
            break
        if token == long or token.startswith(f"{long}="):
            return True
        if token.startswith("-") and not token.startswith("--") and short in token[1:]:
            return True
    return False


def _command_operands(args: list[str], *, short_options_with_values: frozenset[str] = frozenset(), long_options_with_values: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    operands = []
    target_directories = []
    options = True
    index = 0
    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and token.startswith("--"):
            option, separator, attached = token.partition("=")
            if option in long_options_with_values:
                if separator:
                    value = attached
                elif index + 1 < len(args):
                    index += 1
                    value = args[index]
                else:
                    value = ""
                if option == "--target-directory" and value:
                    target_directories.append(value)
            index += 1
            continue
        if options and token.startswith("-") and token != "-":
            chars = token[1:]
            value_option_at = next((at for at, char in enumerate(chars) if char in short_options_with_values), None)
            if value_option_at is not None:
                option = chars[value_option_at]
                attached = chars[value_option_at + 1:]
                if attached:
                    value = attached
                elif index + 1 < len(args):
                    index += 1
                    value = args[index]
                else:
                    value = ""
                if option == "t" and value:
                    target_directories.append(value)
            index += 1
            continue
        operands.append(token)
        index += 1
    return operands, target_directories


def _inplace_edit_targets(executable: str, args: list[str]) -> list[str]:
    short_program_options = frozenset({"e", "f"}) if executable == "sed" else frozenset({"e", "E"})
    long_program_options = frozenset({"--expression", "--file"}) if executable == "sed" else frozenset()
    operands, _ = _command_operands(args, short_options_with_values=short_program_options, long_options_with_values=long_program_options)
    has_explicit_program = any(_short_or_long_option_present(args, short, long) for short, long in ((("e", "--expression"), ("f", "--file")) if executable == "sed" else (("e", "--unused"), ("E", "--unused"))))
    return operands if has_explicit_program else operands[1:]


def _smart_floor_config_write(command: str) -> bool:
    for segment in _iter_top_level_shell_segments(command):
        segment_tokens = _shell_segment_tokens(segment, 0)
        if segment_tokens is not None:
            for index, token in enumerate(segment_tokens[:-1]):
                if token in {">", ">>"} and _is_smart_floor_config_target(segment_tokens[index + 1]):
                    return True
        for start, _, word in _iter_shell_command_word_spans(segment):
            executable = os.path.basename(_deobfuscate_shell_word_for_detection(word)).lower()
            if executable not in {"tee", "sed", "perl", "ruby", "cp", "mv", "install", "truncate"}:
                continue
            tokens = _shell_segment_tokens(segment, start)
            if not tokens:
                continue
            args = tokens[1:]
            if executable == "tee":
                operands, _ = _command_operands(args)
                if any(_is_smart_floor_config_target(path) for path in operands):
                    return True
                continue
            if executable in {"sed", "perl", "ruby"}:
                if _short_or_long_option_present(args, "i", "--in-place") and any(_is_smart_floor_config_target(path) for path in _inplace_edit_targets(executable, args)):
                    return True
                continue
            if executable in {"cp", "mv"}:
                operands, targets = _command_operands(args, short_options_with_values=frozenset({"S", "t"}), long_options_with_values=frozenset({"--suffix", "--target-directory"}))
                if any(_is_smart_floor_config_target(path) for path in (targets or operands[-1:])):
                    return True
                continue
            if executable == "install":
                operands, targets = _command_operands(args, short_options_with_values=frozenset({"g", "m", "o", "S", "t"}), long_options_with_values=frozenset({"--group", "--mode", "--owner", "--suffix", "--target-directory"}))
                directory_mode = _short_or_long_option_present(args, "d", "--directory")
                destinations = targets or (operands if directory_mode else operands[-1:])
                if any(_is_smart_floor_config_target(path) for path in destinations):
                    return True
                continue
            operands, _ = _command_operands(args, short_options_with_values=frozenset({"r", "s"}), long_options_with_values=frozenset({"--reference", "--size"}))
            if any(_is_smart_floor_config_target(path) for path in operands):
                return True
    return False


def _smart_manual_floor_reason(command: str) -> Optional[str]:
    """Escalate the three existing fork classes to ordinary human approval."""
    patterns = (
        (r"\b(?:curl|wget|git|gh|ssh|docker|hermes|kanban|echo|printf|cat|base64|base32|base16|xxd)\b[^\n|;&]*\|(?:[^\n|;&]*\|)?\s*(?:[/\w.-]*/)?(?:python[23]?|perl|ruby|node|(?:ba)?sh)\b", "manual_floor:pipe_to_interpreter"),
        (r"\bgit\s+push\b[^\n;&]*(?:--force(?:-with-lease)?|-f\b|--delete\b|:\S+)", "manual_floor:git_history_remote"),
    )
    for variant in _command_detection_variants(command):
        for pattern, reason in patterns:
            if re.search(pattern, variant, re.IGNORECASE | re.DOTALL):
                return reason
        if _smart_floor_config_write(variant):
            return "manual_floor:env_config_write"
    return None
