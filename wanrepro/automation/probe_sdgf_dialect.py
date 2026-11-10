#!/usr/bin/env python
"""AT-13 B4 方言合同锁 · PY 侧探针（免费、纯离线：不发网络、不调渠道、不花钱）。

合同 = docs/nodes/AT-13.md v3 §2.9（I-06）：**同一份 JSON 夹具双端消费**——
  · 夹具：wanrepro/tests/fixtures/sdgf-dialect/cases.json（输入 model/ratio/sound/duration/mode/imageCount + 期望输出）
  · TS 侧：web/scripts/probe-sdgf-payload.mjs（加载同一份夹具）
  · PY 侧：本文件（加载同一份夹具）
单改任一端实现（比例/声音生效位、代际时长、mode 映射）→ 至少一条共享 case 必红。

PY 侧断言两层（都在 `ui/server.py` **真实现**上跑，不复制映射表）：
  ① 纯函数层：sdgf_full_model / sdgf_ratio_fields / sdgf_sound_fields / sdgf_clamp_duration / sdgf_mode_name
     逐条等于夹具期望（规范化 JSON 比对，不比键序）。
  ② 组装层：sdgf_plan（纯本地预演，无网络）产出的渠道请求体 body——
     顶层 aspect_ratio / metadata 合并结果 / 顶层 enableSound / duration / mode 与期望全等；
     期望 ok=false 的 case 断言 preflight 拒绝因（model|duration|ratio|reference-no-image）。

跑法：wanrepro/ 下 `py -3 -m automation.probe_sdgf_dialect`（全对 exit 0；任一 FAIL exit 1）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                      # wanrepro/
FIXTURE = ROOT / "tests" / "fixtures" / "sdgf-dialect" / "cases.json"

# 拒绝因 → 预检理由里的可判别片段（**不靠错误文案猜分类**只在这里做「同一拒绝因」的锚定）
REJECT_ANCHORS = {
    "model": "channel.model 不能为空",
    "duration": "duration 只支持 4–30",
    "ratio": "ratio 只支持",
    "reference-no-image": "参考图",
}

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def load_server():
    # ui/server.py 顶部 `import hub_api` 是兄弟模块直连（直跑时 cwd=ui/）——按文件加载时必须先把 ui/ 放进 sys.path
    for path in (str(ROOT), str(ROOT / "ui")):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("at13_sdgf_dialect_server", ROOT / "ui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                              # 模块尾部有 __main__ 守卫，import 不起服务
    return module


def norm(value) -> str:
    """规范化 JSON 比对：键序不比、中文不转义（对象/数组/标量同一口径）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def payload_of(case: dict) -> dict:
    """夹具输入 → bridge 提交体（与 hub 链同一形态：ratio 已是最终画幅值）。"""
    src = case["input"]
    images = [f"https://fixture.invalid/ref-{i}.png" for i in range(1, int(src["imageCount"]) + 1)]
    return {
        "prompt": "夹具提示词（探针不提交）",
        "duration": src["duration"],
        "ratio": src["ratio"],
        "audio": src["sound"],
        "mode": src["mode"],
        "images": images,
        "channel": {"baseUrl": "https://fixture.invalid", "apiKey": "fixture-key", "model": src["model"]},
    }


def main() -> int:
    server = load_server()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    cases = fixture.get("cases") or []
    print(f"夹具：{FIXTURE.relative_to(ROOT)}")
    print(f"fixture 案例数: {len(cases)}（合同 {fixture.get('contract', '?')}）")

    # ---------------------------------------------------------------- 夹具自身的覆盖守卫
    ids = [c.get("id") for c in cases]
    check("夹具 case id 唯一且非空", len(ids) == len(set(ids)) and all(ids), f"{len(ids)} 条 / {len(set(ids))} 唯一")

    def pick(pred) -> list[dict]:
        return [c for c in cases if pred(c["input"], c["expect"])]

    coverage = {
        "满血 GF 名命中": bool(pick(lambda i, e: e["fullModel"] is True)),
        "别名/未知模型不命中": bool(pick(lambda i, e: e["fullModel"] is False)),
        "音频开": bool(pick(lambda i, e: i["sound"] is True)),
        "音频关": bool(pick(lambda i, e: i["sound"] is False)),
        "代际截断（clamp<请求）": bool(pick(lambda i, e: e["clampDuration"] < i["duration"])),
        "不截断（clamp==请求）": bool(pick(lambda i, e: e["clampDuration"] == i["duration"])),
        "metadata 双写（ratio）": bool(pick(lambda i, e: "metadata" in (e["ratioFields"] or {}))),
        "metadata 不写（枚举外画幅）": bool(pick(lambda i, e: "metadata" not in (e["ratioFields"] or {}))),
        "metadata 合并（ratio+sound 同对象）": bool(pick(lambda i, e: e.get("metadata")
                                                    and set(e["metadata"]) >= {"ratio", "enableSound"})),
        "自适应画幅（ratio=adaptive）": bool(pick(lambda i, e: i["ratio"] == "adaptive")),
        "mode=文生视频": bool(pick(lambda i, e: e["modeName"] == "文生视频")),
        "mode=首帧生视频": bool(pick(lambda i, e: e["modeName"] == "首帧生视频")),
        "mode=首尾帧": bool(pick(lambda i, e: e["modeName"] == "首尾帧")),
        "mode=参考生视频": bool(pick(lambda i, e: e["modeName"] == "参考生视频")),
        "拒绝因=model": bool(pick(lambda i, e: (e.get("assembly") or {}).get("reject") == "model")),
        "拒绝因=duration": bool(pick(lambda i, e: (e.get("assembly") or {}).get("reject") == "duration")),
        "拒绝因=ratio": bool(pick(lambda i, e: (e.get("assembly") or {}).get("reject") == "ratio")),
        "拒绝因=reference-no-image": bool(pick(lambda i, e: (e.get("assembly") or {}).get("reject") == "reference-no-image")),
    }
    for name, ok in coverage.items():
        check(f"夹具覆盖：{name}", ok)

    # ---------------------------------------------------------------- 逐 case：纯函数层 + 组装层
    asserts = 0
    for case in cases:
        cid, src, want = case["id"], case["input"], case["expect"]
        model, ratio = src["model"], src["ratio"]
        image_count = int(src["imageCount"])
        mode = src["mode"]

        asserts += 1
        check(f"{cid} · 纯函数 sdgf_full_model", server.sdgf_full_model(model) is want["fullModel"],
              f"{model!r} → {server.sdgf_full_model(model)}")
        asserts += 1
        got_ratio = server.sdgf_ratio_fields(model, ratio)
        check(f"{cid} · 纯函数 sdgf_ratio_fields", norm(got_ratio) == norm(want["ratioFields"]),
              f"{norm(got_ratio)} ≠ {norm(want['ratioFields'])}")
        asserts += 1
        got_sound = server.sdgf_sound_fields(model, bool(src["sound"]))
        check(f"{cid} · 纯函数 sdgf_sound_fields", norm(got_sound) == norm(want["soundFields"]),
              f"{norm(got_sound)} ≠ {norm(want['soundFields'])}")
        asserts += 1
        got_clamp = server.sdgf_clamp_duration(model, int(src["duration"]))
        check(f"{cid} · 纯函数 sdgf_clamp_duration", got_clamp == want["clampDuration"],
              f"{got_clamp} ≠ {want['clampDuration']}")
        if want["modeName"] is not None:
            asserts += 1
            got_mode = server.sdgf_mode_name(mode, image_count)
            check(f"{cid} · 纯函数 sdgf_mode_name", got_mode == want["modeName"],
                  f"{got_mode!r} ≠ {want['modeName']!r}")

        # 组装层（sdgf_plan 纯本地预演，无网络）
        plan = server.sdgf_plan(payload_of(case))
        body, pre = plan["body"], plan["preflight"]
        assembly = want["assembly"]
        asserts += 1
        if assembly["ok"]:
            check(f"{cid} · 组装 preflight 通过", pre["ok"] is True, "; ".join(pre["reasons"])[:200])
            asserts += 1
            check(f"{cid} · 请求体 aspect_ratio", body.get("aspect_ratio") == want["ratioFields"]["aspect_ratio"],
                  f"{body.get('aspect_ratio')!r} ≠ {want['ratioFields']['aspect_ratio']!r}")
            asserts += 1
            check(f"{cid} · 请求体 metadata 合并结果", norm(body.get("metadata")) == norm(want["metadata"]),
                  f"{norm(body.get('metadata'))} ≠ {norm(want['metadata'])}")
            asserts += 1
            check(f"{cid} · 请求体顶层 enableSound", body.get("enableSound") == want["topEnableSound"],
                  f"{body.get('enableSound')!r} ≠ {want['topEnableSound']!r}")
            asserts += 1
            check(f"{cid} · 请求体 duration（代际截断后）", body.get("duration") == want["clampDuration"],
                  f"{body.get('duration')!r} ≠ {want['clampDuration']!r}")
            asserts += 1
            check(f"{cid} · 请求体 mode（站方中文枚举）", body.get("mode") == want["modeName"],
                  f"{body.get('mode')!r} ≠ {want['modeName']!r}")
            if model.strip():
                asserts += 1
                check(f"{cid} · 请求体 model 原样透传", body.get("model") == model.strip(),
                      f"{body.get('model')!r} ≠ {model.strip()!r}")
        else:
            anchor = REJECT_ANCHORS.get(assembly["reject"])
            reasons = "；".join(pre["reasons"])
            check(f"{cid} · 组装被拒（{assembly['reject']}）", pre["ok"] is False and anchor is not None and anchor in reasons,
                  f"ok={pre['ok']} reasons={reasons[:200]}")

    # ---------------------------------------------------------------- 总账
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nprobe_sdgf_dialect: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS"
          f"（fixture 案例数 {len(cases)}，逐 case 断言 {asserts} 条）"
          + (f"，FAIL {len(failed)} 项" if failed else ""))
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name} —— {detail[:200]}")
    if failed:
        return 1
    print("PASS：PY 侧 5 个方言函数 + sdgf_plan 组装体与共享夹具逐条全等（单改任一端必红）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
