# -*- coding: utf-8 -*-
"""MCP 客户端：真实拉起 wanrepro.server（stdio），按顺序调用工具。

用法：
  py -3 -m wanrepro.client_demo list
  py -3 -m wanrepro.client_demo call <tool> '<json args>'
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self) -> None:
        self.p = subprocess.Popen(
            [sys.executable, "-m", "wanrepro.server"], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "wanrepro-cli", "version": "0.1"}})
        self._notify("notifications/initialized", {})

    def _send(self, obj: dict) -> None:
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed: " + self.p.stderr.read())
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list", {})["tools"]

    def call(self, name: str, **args) -> dict:
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        text = res["content"][0]["text"]
        try:
            return json.loads(text)
        except Exception:
            return {"ok": not res.get("isError"), "out": text}

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def main(argv: list[str]) -> int:
    c = Client()
    try:
        if len(argv) < 2 or argv[1] == "list":
            for t in c.list_tools():
                print(f"{t['name']:8s} {t['description']}")
            return 0
        name = argv[1]
        args = json.loads(argv[2]) if len(argv) > 2 else {}
        print(json.dumps(c.call(name, **args), ensure_ascii=False, indent=1))
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
