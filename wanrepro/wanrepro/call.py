# -*- coding: utf-8 -*-
"""按 JSON 脚本依次调用 MCP 工具，避免在 shell 里转义中文与引号。

用法：py -3 -m wanrepro.call steps.json [--chars 4000]

steps.json:
{"calls":[{"tool":"guide","args":{"task":"render","assets":3}}, ...]}
"""
from __future__ import annotations

import json
import sys

from wanrepro.client_demo import Client


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "steps.json"
    limit = 4000
    if "--chars" in argv:
        limit = int(argv[argv.index("--chars") + 1])
    steps = json.loads(open(path, encoding="utf-8").read())["calls"]

    c = Client()
    try:
        for i, s in enumerate(steps, 1):
            tool, args = s["tool"], s.get("args", {})
            print(f"\n===== [{i}] {tool} {json.dumps(args, ensure_ascii=False)}")
            res = c.call(tool, **args)
            if "out" in res and res["out"]:
                out = res["out"]
                print(out if len(out) <= limit else out[:limit] + f"\n…（截断，共 {len(out)} 字符）")
            if "err" in res and res["err"]:
                print("STDERR:", res["err"][-1500:])
            print(f"→ ok={res.get('ok')} code={res.get('code')} data={json.dumps(res.get('data'), ensure_ascii=False)}")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
