"""Read the literal shell words in a Cronie command without evaluating it."""
from dataclasses import dataclass
import re


NAME = re.compile(rb'[a-zA-Z_][a-zA-Z_0-9]*=')
OPERATORS = b';|&()<>'
QUOTED = rb"""'[^']*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|\\."""
PART = re.compile(QUOTED + rb"""|(?:(?!\$[({])[^ \t;|&()<>'"`\\])+""")
QUOTED_PART = re.compile(QUOTED)
DOLLAR_EXPANSION = rb'\$(?=[a-zA-Z_0-9@*#?$!{(\-])'
BARE_EXPANSION = re.compile(DOLLAR_EXPANSION + rb'|[=:]~')
DOUBLE_ESCAPE = re.compile(rb'\\([$`"\\])|(`|' + DOLLAR_EXPANSION + rb')')
ENV_PATTERN = re.compile(rb'[*?\[{}]')


def cron_decode(raw):
    """Return the shell command, its byte offsets in raw, and whether stdin follows."""
    command = bytearray()
    offsets = []
    escaped = False
    for offset, char in enumerate(raw):
        if char == ord('%'):
            if not escaped:
                return bytes(command), offsets, True
            command.pop()
            offsets.pop()
        command.append(char)
        offsets.append(offset)
        escaped = char == ord('\\') and not escaped
    return bytes(command), offsets, False


def expansion_end(command, index):
    """Skip an opaque shell expansion while locating the executable."""
    opening = command[index + 1]
    closing = ord('}') if opening == ord('{') else ord(')')
    depth = 1
    index += 2
    while index < len(command):
        quoted = QUOTED_PART.match(command, index)
        if quoted:
            index = quoted.end()
        else:
            char = command[index]
            depth += (char == opening) - (char == closing)
            index += 1
            if not depth:
                return index
    return None


@dataclass(frozen=True)
class Word:
    value: bytes
    raw: bytes
    expands: bool = False
    env_pattern: bool = False
    operator: bool = False

    def assignment_name(self, *, env=False):
        match = NAME.match(self.value if env else self.raw)
        return match[0][:-1] if match else None


@dataclass(frozen=True)
class Command:
    words: list[Word]
    has_stdin: bool
    complete: bool

    @classmethod
    def parse(cls, raw):
        command, offsets, has_stdin = cron_decode(raw)
        words = []
        index = depth = 0
        while index < len(command):
            char = command[index:index + 1]
            if char in b' \t':
                index += 1
                continue
            if char == b'#':
                return cls(words, has_stdin, not depth)
            if char in OPERATORS:
                words.append(Word(char, char, operator=True))
                depth += (char == b'(') - (char == b')')
                index += 1
                continue
            start = index
            value = bytearray()
            expands = env_pattern = False
            while index < len(command):
                if command[index:index + 2] in (b'$(', b'${', b'<(', b'>('):
                    end = expansion_end(command, index)
                    if end is None:
                        return cls(words, has_stdin, False)
                    value.extend(command[index:end])
                    index = end
                    expands = True
                    continue
                if command[index] in b' \t' + OPERATORS:
                    break
                part = PART.match(command, index)
                if part is None:
                    return cls(words, has_stdin, False)
                chunk = part[0]
                index = part.end()
                if chunk.startswith(b"'"):
                    value.extend(chunk[1:-1])
                elif chunk.startswith(b'"'):
                    inner = chunk[1:-1]
                    expands |= any(match[2] for match in DOUBLE_ESCAPE.finditer(inner))
                    value.extend(DOUBLE_ESCAPE.sub(lambda match: match[1] or match[2], inner))
                elif chunk.startswith(b'\\'):
                    value.extend(chunk[1:])
                else:
                    expands |= (chunk.startswith(b'`') or bool(BARE_EXPANSION.search(chunk))
                                or (part.start() == start and chunk.startswith(b'~'))
                                or (chunk.endswith(b'$') and command[index:index + 1] in (b'(', b"'", b'"')))
                    env_pattern |= bool(ENV_PATTERN.search(chunk))
                    value.extend(chunk)
            words.append(Word(bytes(value), raw[offsets[start]:offsets[index - 1] + 1], expands, env_pattern))
        return cls(words, has_stdin, not depth)
