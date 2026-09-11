from __future__ import annotations

import codecs
import re
from pathlib import Path


_DESTINATIONS = {
    "colortbl", "datastore", "filetbl", "fonttbl", "footer", "footerf",
    "footerl", "footerr", "generator", "header", "headerf", "headerl",
    "headerr", "info", "listtable", "listoverridetable", "nonshppict",
    "object", "objdata", "pict", "private", "revtbl", "stylesheet",
    "themedata", "xmlnstbl",
}
_CONTROL_TEXT = {
    "bullet": "•", "cell": " | ", "emdash": "—", "endash": "–",
    "line": "\n", "page": "\n", "par": "\n", "row": "\n", "tab": "\t",
    "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”",
}


def _codec(codepage: int, fallback: str) -> str:
    candidate = f"cp{codepage}"
    try:
        codecs.lookup(candidate)
    except LookupError:
        return fallback
    return candidate


def _skip_fallback(source: str, index: int, count: int) -> int:
    while count and index < len(source):
        if source[index] == "\\":
            index += 4 if source[index:index + 2] == "\\'" else 2
        elif source[index] in "{}":
            break
        else:
            index += 1
        count -= 1
    return min(index, len(source))


def _plain_text(data: bytes) -> str:
    source = data.decode("latin1")
    output: list[str] = []
    stack: list[tuple[bool, int, str]] = []
    skip, uc_skip, encoding = False, 1, "cp1252"
    index = 0
    while index < len(source):
        char = source[index]
        if char == "{":
            stack.append((skip, uc_skip, encoding))
            index += 1
            continue
        if char == "}":
            if stack:
                skip, uc_skip, encoding = stack.pop()
            index += 1
            continue
        if char != "\\":
            if not skip and char not in "\r\n":
                output.append(
                    bytes((ord(char),)).decode(encoding, errors="replace")
                    if ord(char) >= 128
                    else char
                )
            index += 1
            continue

        index += 1
        if index >= len(source):
            break
        symbol = source[index]
        if symbol in "\\{}":
            if not skip:
                output.append(symbol)
            index += 1
            continue
        if symbol == "'":
            raw_hex = source[index + 1:index + 3]
            if re.fullmatch(r"[0-9a-fA-F]{2}", raw_hex):
                if not skip:
                    output.append(
                        bytes((int(raw_hex, 16),)).decode(encoding, errors="replace")
                    )
                index += 3
                continue
        if symbol == "*":
            skip = True
            index += 1
            continue
        if not symbol.isalpha():
            if not skip:
                output.append({"~": " ", "_": "-"}.get(symbol, ""))
            index += 1
            continue

        start = index
        while index < len(source) and source[index].isalpha():
            index += 1
        word = source[start:index].lower()
        sign = -1 if index < len(source) and source[index] == "-" else 1
        index += sign < 0
        start = index
        while index < len(source) and source[index].isdigit():
            index += 1
        number = sign * int(source[start:index]) if start < index else None
        index += index < len(source) and source[index] == " "

        if word in _DESTINATIONS:
            skip = True
        elif word == "ansicpg" and number is not None:
            encoding = _codec(number, encoding)
        elif word == "uc" and number is not None:
            uc_skip = max(0, number)
        elif word == "bin" and number is not None:
            index = min(len(source), index + max(0, number))
        elif word == "u" and number is not None:
            if not skip:
                output.append(chr(number if number >= 0 else number + 65536))
            index = _skip_fallback(source, index, uc_skip)
        elif not skip and word in _CONTROL_TEXT:
            output.append(_CONTROL_TEXT[word])

    lines = []
    for raw_line in "".join(output).replace("\x00", "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_rtf_text(path: Path) -> tuple[str, str, list[str]]:
    data = path.read_bytes()
    if not data.lstrip().startswith(b"{\\rtf"):
        return "", "rtf_invalid", ["Файл не содержит сигнатуру RTF."]
    text = _plain_text(data)
    if len(text) < 80 or not any(char.isalnum() for char in text):
        return "", "rtf_quality_failed", [f"RTF не дал полезный текст: длина {len(text)}."]
    return text, "ok", []
