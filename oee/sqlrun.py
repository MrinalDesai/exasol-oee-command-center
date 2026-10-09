"""Run .sql files against Exasol, including UDF/Lua script definitions.

Plain statements end with ';'. CREATE ... SCRIPT statements end with a line
containing only '/', as in Exasol's own clients, so semicolons inside script
bodies are left alone.
"""
from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_START = re.compile(
    r"^\s*CREATE\s+(OR\s+REPLACE\s+)?(\w+\s+)?(SCALAR\s+|SET\s+)?SCRIPT\b", re.IGNORECASE
)


def split_statements(sql: str) -> list[str]:
    stmts: list[str] = []
    buf: list[str] = []
    in_script = False
    in_quote = False

    for line in sql.splitlines():
        if in_script:
            if line.strip() == "/":
                stmts.append("\n".join(buf).rstrip())
                buf, in_script = [], False
            else:
                buf.append(line)
            continue

        if not buf and not in_quote and _SCRIPT_START.match(line):
            in_script = True
            buf.append(line)
            continue

        out = []
        i = 0
        while i < len(line):
            ch = line[i]
            if in_quote:
                out.append(ch)
                if ch == "'":
                    if i + 1 < len(line) and line[i + 1] == "'":
                        out.append("'")
                        i += 1
                    else:
                        in_quote = False
            elif ch == "'":
                in_quote = True
                out.append(ch)
            elif line.startswith("--", i):
                break
            elif ch == ";":
                buf.append("".join(out))
                stmt = "\n".join(buf).strip()
                if stmt:
                    stmts.append(stmt)
                buf, out = [], []
            else:
                out.append(ch)
            i += 1
        if "".join(out).strip() or (buf and in_quote):
            buf.append("".join(out))

    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def run_file(conn, path: str | Path, params: dict[str, str] | None = None, verbose: bool = True) -> int:
    text = Path(path).read_text()
    for key, value in (params or {}).items():
        text = text.replace("{" + key + "}", value)
    stmts = split_statements(text)
    for s in stmts:
        if verbose:
            print(f"  {Path(path).name}: {s.splitlines()[0][:90]}")
        conn.execute(s)
    return len(stmts)
