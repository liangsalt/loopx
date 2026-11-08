"""Lossless source boundaries derived from declared structure, never text size.

These are navigation units, not inferred functional requirements. A title or an
object boundary needs semantic interpretation by the runtime just like its body.
"""
from __future__ import annotations

import re


PARTITIONING = "source_structure_v1"


def _part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def object_units(value):
    """Map every scalar/empty leaf to its complete declared object scope.

Objects in collections and explicitly labelled module/function/requirement
objects own work. Nested condition/expected-value records of an identified
requirement remain together. Unlabelled prose is never guessed into functions.
"""
    result = {}
    labels = {"module", "module_name", "block", "feature", "function", "function_name",
              "interface", "protocol", "requirement_id", "req_id"}

    def visit(node, pointer="", owner="", ancestors=(), force=False, atomic=False, requirement_member=False):
        if isinstance(node, dict) and node:
            identified = bool(labels.intersection(node))
            requirement = bool({"requirement_id", "req_id"}.intersection(node)) or requirement_member
            if not atomic and pointer != owner and (force or identified):
                ancestors, owner = (*ancestors, owner), pointer
            atomic = atomic or requirement
            collection = not any(not isinstance(child, (dict, list)) for child in node.values())
            for key, child in node.items():
                visit(child, pointer + "/" + _part(key), owner, ancestors,
                      force=collection and isinstance(child, dict), atomic=atomic,
                      requirement_member=pointer.rsplit("/", 1)[-1] == "requirements")
        elif isinstance(node, list) and node:
            for index, child in enumerate(node):
                visit(child, pointer + "/" + str(index), owner, ancestors,
                      force=isinstance(child, (dict, list)), atomic=atomic,
                      requirement_member=pointer.rsplit("/", 1)[-1] == "requirements")
        else:
            result[pointer] = {"pointer": owner, "parents": list(ancestors), "atomic": atomic}

    visit(value)
    return result


def sections(text):
    """Return complete Markdown/AsciiDoc sections, preserving exact offsets.

No section is split because of its length. Fenced/literal blocks and pipe tables
remain intact. Parent section bodies are retained as explicit dependencies by
the inventory builder, rather than copied or silently folded into child text.
"""
    lines = text.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    headings = []
    # Only an actual heading chooses the document syntax. Code samples can
    # contain AsciiDoc examples without changing a surrounding Markdown file.
    asciidoc_document = False
    format_fence = None
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if format_fence:
            if re.fullmatch(r" {0,3}" + re.escape(format_fence[0]) + "{" + str(format_fence[1]) + r",}\s*", line):
                format_fence = None
            continue
        if marker:
            format_fence = (marker[1][0], len(marker[1]))
            continue
        if re.match(r"^(={1,6})\s+", line):
            asciidoc_document = True
            break
        if re.match(r"^ {0,3}#{1,6}\s+", line):
            break
    fence = None
    table = False
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        markdown_fence = re.match(r"^ {0,3}(`{3,}|~{3,})", lines[index])
        if fence:
            if fence[0] in {"`", "~"}:
                if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", lines[index]):
                    fence = None
            elif stripped == fence:
                fence = None
            index += 1
            continue
        if markdown_fence:
            marker = markdown_fence[1]
            fence = (marker[0], len(marker))
            index += 1
            continue
        if asciidoc_document and stripped in {"----", "....", "++++", "====", "////", "|==="}:
            # A Setext underline is handled with its title below before this
            # line is reached. Standalone delimiters open an AsciiDoc block.
            fence = stripped
            index += 1
            continue
        if stripped.startswith("|") or (table and stripped):
            table = stripped.startswith("|")
            if table:
                index += 1
                continue
        if not stripped:
            table = False
        atx = None if asciidoc_document else re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", lines[index])
        asciidoc = re.match(r"^(={1,6})\s+(.+?)\s*$", lines[index]) if asciidoc_document else None
        if atx or asciidoc:
            match = atx or asciidoc
            headings.append((index, len(match[1]), match[2]))
        elif (not asciidoc_document and stripped and not lines[index].startswith(("    ", "\t")) and index + 1 < len(lines)
              and re.fullmatch(r" {0,3}(?:=+|-+)\s*", lines[index + 1])
              and not re.match(r"^\s*(?:[-*+]\s|\d+\.\s|[|>])", lines[index])):
            headings.append((index, 1 if lines[index + 1].lstrip().startswith("=") else 2, stripped))
            index += 1
        index += 1
    if not headings:
        return [{"start": 0, "end": len(text), "index": None, "titles": [], "parents": []}]
    result, stack = [], []
    if headings[0][0]:
        result.append({"start": 0, "end": offsets[headings[0][0]], "index": None,
                       "titles": [], "parents": []})
    for number, (line_index, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        result.append({"start": offsets[line_index],
                       "end": offsets[headings[number + 1][0]] if number + 1 < len(headings) else len(text),
                       "index": number, "titles": [entry[2] for entry in stack] + [title],
                       "parents": [entry[1] for entry in stack]})
        stack.append((level, number, title))
    return result
