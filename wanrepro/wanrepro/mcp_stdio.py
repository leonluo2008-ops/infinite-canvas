# -*- coding: utf-8 -*-
"""最小 MCP stdio 传输层（JSON-RPC 2.0 over stdin/stdout）

只实现我们需要的四个方法，避免为了一个薄壳引入整套框架：
  initialize / notifications/initialized / tools/list / tools/call / ping

用官方 SDK 的客户端可以直接连（见 client_demo.py）。
"""
from __future__ import annotations

import inspect
import json
import sys
import traceback

PROTOCOL = "2024-11-05"

_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema(fn) -> dict:
    sig = inspect.signature(fn)
    props, required = {}, []
    for name, p in sig.parameters.items():
        ann = p.annotation
        t = "string"
        if ann in _TYPE:
            t = _TYPE[ann]
        else:
            s = str(ann)
            for py_t, js in _TYPE.items():
                if py_t.__name__ in s:
                    t = js
                    break
        props[name] = {"type": t, "description": ""}
        if p.default is inspect.Parameter.empty:
            required.append(name)
        else:
            props[name]["default"] = p.default
    return {
        "name": fn.__name__,
        "description": (inspect.getdoc(fn) or "").strip().split("\n")[0],
        "inputSchema": {"type": "object", "properties": props, "required": required},
    }


def serve(name: str, title: str, tools: dict) -> int:
    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": name, "version": "0.1.0", "title": title},
            }})
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [_schema(f) for f in tools.values()]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tname = params.get("name")
            args = params.get("arguments") or {}
            fn = tools.get(tname)
            if not fn:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"unknown tool: {tname}"}})
                continue
            try:
                result = fn(**args)
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": not bool(result.get("ok", True)),
                }})
            except Exception:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": traceback.format_exc()}],
                    "isError": True,
                }})
        else:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"unknown method: {method}"}})
    return 0
