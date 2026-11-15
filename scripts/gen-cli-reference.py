"""Regenerate docs/cli-reference.md from the command parser.

    COLUMNS=80 python scripts/gen-cli-reference.py docs/cli-reference.md > new.md

Run it on Python 3.13 or later: argparse's help layout changed in 3.13, and the
page is kept in that layout. Each command's section is its --help, with a
section per subcommand for commands that have them (worker ...).
"""
import argparse, os, sys
os.environ["COLUMNS"] = "80"
from collab.cli import build_parser

def subs(p):
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)

parser = build_parser()
top = subs(parser)
head = open(sys.argv[1]).read().split("## Commands")[0]
sections = []
for name, p in top.choices.items():
    sections.append((name, p))
    if (inner := subs(p)) is not None:
        for sub_name, sp in inner.choices.items():
            sections.append((f"{name} {sub_name}", sp))
out = [head.rstrip("\n"), "", "## Commands", ""]
out += [f"- [{n}](#{n})" for n in top.choices]
for n, p in sections:
    out += ["", f"## {n}", "", "```text", p.format_help().rstrip("\n"), "```"]
print("\n".join(out))
