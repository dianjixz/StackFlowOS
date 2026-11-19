"""Artifact templates used by the deterministic (offline) brain.

These are real, runnable programs written into a project workspace by a coder
agent. Keeping them here makes the mock brain's output inspectable and
testable without going through the agent loop.
"""

from __future__ import annotations

MD_TOOL_TEMPLATE = r'''"""任务 __TID__：__TITLE__

__DESC__
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _CODE.sub(lambda m: "<code>" + m.group(1) + "</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(source: str) -> str:
    """Convert a small Markdown subset to HTML (headings/lists/bold/code/links)."""
    out: list[str] = []
    in_list = False
    for raw in source.splitlines():
        line = raw.rstrip()
        heading = _HEADING.match(line)
        item = _ITEM.match(line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
        elif not line:
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _selftest() -> int:
    sample = "# 标题\n\n这是 **粗体** 和 `代码`。\n\n- 一\n- 二\n"
    rendered = markdown_to_html(sample)
    assert "<h1>标题</h1>" in rendered
    assert "<strong>粗体</strong>" in rendered
    assert "<code>代码</code>" in rendered
    assert rendered.count("<li>") == 2
    print("selftest ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Markdown -> HTML")
    parser.add_argument("input", nargs="?")
    parser.add_argument("-o", "--output")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    source = Path(args.input).read_text("utf-8") if args.input else sys.stdin.read()
    rendered = markdown_to_html(source)
    if args.output:
        Path(args.output).write_text(rendered, "utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

GENERIC_TEMPLATE = r'''"""任务 __TID__：__TITLE__

__DESC__
"""

from __future__ import annotations


def run() -> str:
    """最小可运行入口。"""
    return "__TID__: ok"


def _selftest() -> int:
    assert run() == "__TID__: ok"
    print("selftest ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    print(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

README_TEMPLATE = r'''# __TITLE__

__GOAL__

## 用法

```bash
python3 src/__SRC__ README.md -o out.html
python3 src/__SRC__ --selftest
```

## 结构

- `src/`：实现与自检入口
- `design/`：设计文档与取舍
- `tests/`：测试报告
- `review/`：评审记录

## 说明

本项目由 StackFlowOS 的 agent 工人在项目工作区内生成；
每次写入、执行命令都经过能力审批并记录在事件流中。
'''

__all__ = ["MD_TOOL_TEMPLATE", "GENERIC_TEMPLATE", "README_TEMPLATE"]
