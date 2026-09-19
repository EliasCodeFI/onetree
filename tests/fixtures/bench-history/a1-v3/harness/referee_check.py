"""onetree-bench referee: corpus files keep header and token-line shape."""
import pathlib
import re
import sys

HEADER = "onetree-bench corpus file"
TOKEN = re.compile(r"^tok:w[0-9]+:t[0-9]+:[0-9a-f]{12}$")
problems = []
root = pathlib.Path.cwd() / "corpus"
files = sorted(root.glob("*.txt")) if root.is_dir() else []
if not files:
    problems.append("corpus/ is missing or empty")
for path in files:
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0] != HEADER:
        problems.append(path.name + ": header")
    for number, line in enumerate(lines[1:], 2):
        if not TOKEN.match(line):
            problems.append(path.name + ":" + str(number) + ": token line")
sys.stderr.write("".join(problem + "\n" for problem in problems))
raise SystemExit(1 if problems else 0)
