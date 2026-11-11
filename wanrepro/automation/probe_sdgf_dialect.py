#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-13 B4 方言合同锁 + AT-14 B1 画幅规约 · PY 侧探针（免费、纯离线：不发网络、不调渠道、不花钱）。

合同 = docs/nodes/AT-13.md v3 §2.9（I-06）+ docs/nodes/AT-14.md v2 §3.1/§五.1：
**同一份 JSON 夹具双端消费**——
  · 夹具：wanrepro/tests/fixtures/sdgf-dialect/cases.json（cases = 方言；sizeCases = 画幅）
  · TS 侧：web/scripts/probe-sdgf-payload.mjs（cases）+ web/scripts/probe-at13-b3.mjs（sizeCases）
  · PY 侧：本文件（两份都加载）
单改任一端实现（比例/声音生效位、代际时长、mode 映射、画幅规约）→ 至少一条共享 case 必红。

PY 侧断言两层（都在**真实现**上跑，不复制映射表）：
  ① 纯函数层：sdgf_full_model / sdgf_ratio_fields / sdgf_sound_fields / sdgf_clamp_duration / sdgf_mode_name
     逐条等于夹具期望（规范化 JSON 比对，不比键序）。
  ② 组装层：sdgf_plan（纯本地预演，无网络）产出的渠道请求体 body——
     顶层 aspect_ratio / metadata 合并结果 / 顶层 enableSound / duration / mode 与期望全等；
     期望 ok=false 的 case 断言 preflight 拒绝因（model|duration|ratio|reference-no-image）。
  ③ 画幅真入口（AT-14 B1）：`hub_api._ratio_from_size` + `_protocol_ratio` 两步 → `_normalize_config`
     落 gen_params/size_input → `do_approve` 落任务文件；rawSize 不进 v2 快照哈希。

跑法：wanrepro/ 下 `py -3 -m automation.probe_sdgf_dialect`（全对 exit 0；任一 FAIL exit 1）。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                      # wanrepro/
FIXTURE = ROOT / "tests" / "fixtures" / "sdgf-dialect" / "cases.json"
SIZE_PROBE_ID = "auto-9401"      # AT-14 B1 do_approve 落盘夹具（探针自建自清，真实任务只读）

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


def probe_size_cases(server, fixture: dict) -> None:
    """AT-14 B1 画幅：**真入口两步**（_ratio_from_size → _protocol_ratio）+ _normalize_config 留痕。

    夹具 = cases.json 的 sizeCases（与 TS 侧 probe-at13-b3.mjs 同一份）。
    断言四层：①规约结果 ②渠道接收域拒绝 ③_normalize_config 的 gen_params.ratio + size_input
    ④rawSize 不进 v2 快照哈希（同比例不同原串 → gen_params 全等）；外加一条 do_approve 落盘。
    """
    hub = server.hub_api
    block = fixture.get("sizeCases") or {}
    cases = block.get("cases") or []
    print(f"\n画幅夹具 sizeCases：{len(cases)} 例（合同 {block.get('contract', '?')}）")

    ids = [c.get("id") for c in cases]
    check("sizeCases id 唯一且非空", len(ids) == len(set(ids)) and all(ids),
          f"{len(ids)} 条 / {len(set(ids))} 唯一")

    # §五.1 规定输入集覆盖守卫（少一个输入=合同被削弱，直接红）
    required = {"240x427", "480x854", "240X427", "240×427", "9:16", "21:9", "auto", "0x0", "abc", "2000x100"}
    present = {c["input"]["size"] for c in cases}
    check("§五.1 十个规定输入全部在夹具内", required <= present, f"缺 {sorted(required - present)}")
    check("夹具含拒绝侧（零维 + 渠道不接）与接受侧",
          any(c["expect"]["reject"] == "size" for c in cases)
          and any(c["expect"]["reject"] == "channel" for c in cases)
          and any(c["expect"]["reject"] is None for c in cases))
    check("夹具覆盖 wan 渠道（apiFormat 分流不只测 sdgf）",
          any(c["input"].get("apiFormat") == "wan" for c in cases))

    seen_by_ratio: dict[str, list[tuple[str, dict]]] = {}
    for case in cases:
        cid, src, want = case["id"], case["input"], case["expect"]
        size = src["size"]
        api_format = src.get("apiFormat", "sdgf")

        # ① 第一步：画布 inferVideoRatio 同口径规约
        normalized, reject = None, None
        try:
            normalized = hub._ratio_from_size(size)
        except hub.HubInputError as e:
            reject, detail = "size", str(e)
            check(f"{cid} · 输入拒绝文案含「必须为正」", "必须为正" in detail, detail[:120])
        else:
            check(f"{cid} · _ratio_from_size 规约结果", normalized == want["ratio"],
                  f"{normalized!r} ≠ {want['ratio']!r}")
            # ② 第二步：渠道接收域（不支持=显式拒绝，不静默换值）
            try:
                wire = hub._protocol_ratio(api_format, normalized)
            except hub.HubInputError as e:
                reject, wire = "channel", None
                check(f"{cid} · 渠道拒绝文案保留", bool(str(e).strip()), str(e)[:120])
            else:
                check(f"{cid} · _protocol_ratio 提交体画幅", wire == want["wire"],
                      f"{wire!r} ≠ {want['wire']!r}")

        # ③ 真入口：_normalize_config（前端 approve config 的同一路径）
        cfg = {"渠道身份": {"baseUrl": "https://fixture.invalid", "apiFormat": api_format,
                            "渠道名": "夹具渠道"},
               "model": "" if api_format == "wan" else "SD2.5-满血-GF-720P",
               "mode": "" if api_format == "wan" else "frames",
               "params": {"duration": 8, "size": size, "rawSize": size,
                          "vquality": "720", "audio": True}}
        try:
            params, channel, size_input = hub._normalize_config(cfg, {})
        except hub.HubInputError:
            check(f"{cid} · _normalize_config 同步拒绝（{reject}）", reject is not None, "两步口径不一致")
            check(f"{cid} · 前端可提交标记", bool(want["accepted"]) is False, "拒绝项却被标可提交")
        else:
            check(f"{cid} · _normalize_config 通过（前端同样可提交）",
                  reject is None and want["accepted"] is True, f"reject={reject}")
            check(f"{cid} · gen_params.ratio = 渠道提交体画幅", params["ratio"] == want["wire"],
                  f"{params['ratio']!r} ≠ {want['wire']!r}")
            check(f"{cid} · size_input 留痕（原始输入 + 规约结果）",
                  size_input == {"raw": size, "ratio": want["wire"]},
                  f"{size_input!r}")
            check(f"{cid} · gen_params 词表不变（rawSize 不并进参数）",
                  sorted(params) == ["audio", "duration", "ratio", "resolution"], str(sorted(params)))
            seen_by_ratio.setdefault(want["ratio"], []).append((size, params))

    # ④ rawSize 不进 v2 快照哈希：同规约结果、不同原始串 → gen_params 全等
    for ratio, group in seen_by_ratio.items():
        if len(group) < 2:
            continue
        first_size, first_params = group[0]
        same = all(params == first_params for _size, params in group[1:])
        check(f"ratio={ratio} 的 {len(group)} 个原始输入 → gen_params 全等（rawSize 不进哈希输入）",
              same, f"{[(s, p) for s, p in group]}")

    # ⑤ do_approve 真落盘：size_input 写进任务文件，审批快照仍是 v2 哈希（rawSize 不参与）
    tdir = ROOT / "automation" / "projects" / SIZE_PROBE_ID
    try:
        if tdir.is_dir():
            shutil.rmtree(tdir)
        tdir.mkdir(parents=True, exist_ok=True)
        prompt = "镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 8
        (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
        hub.state.save(tdir, {"local_id": SIZE_PROBE_ID, "bridge_id": f"probe-at14b1-{SIZE_PROBE_ID}",
                              "created_at": "2026-01-01T00:00:00+08:00",
                              "state": "awaiting_approval",
                              "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None,
                                           "to": "awaiting_approval", "note": "probe-at14-b1 画幅夹具"}],
                              "intent": "AT-14 B1 画幅探针夹具", "assets": []})
        cfg = {"渠道身份": {"baseUrl": "https://fixture.invalid", "apiFormat": "sdgf", "渠道名": "夹具渠道"},
               "model": "SD2.5-满血-GF-720P", "mode": "frames",
               "params": {"duration": 8, "size": "240x427", "rawSize": "240×427",
                          "vquality": "720", "audio": True}}
        out = hub.do_approve(SIZE_PROBE_ID, config=cfg)
        task = hub.state.load(tdir)
        check("do_approve 落 size_input（原始输入 240×427 → 规约 9:16）",
              task.get("size_input") == {"raw": "240×427", "ratio": "9:16"},
              json.dumps(task.get("size_input"), ensure_ascii=False))
        check("do_approve 后 gen_params.ratio 已是渠道枚举（不再有 240:427）",
              task["gen_params"]["ratio"] == "9:16", str(task["gen_params"]))
        check("approve 响应回显 size_input",
              out.get("size_input") == {"raw": "240×427", "ratio": "9:16"}, str(out.get("size_input")))
        expect_snap = hub.state.snapshot_hash_v2(prompt, task["gen_params"], task["gen_channel"])
        check("审批快照 = v2 哈希（prompt+params+渠道身份+model+mode，rawSize 不参与）",
              task.get("approval_snapshot") == expect_snap,
              f"{task.get('approval_snapshot')} ≠ {expect_snap}")
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


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

    # ---------------------------------------------------------------- 画幅（AT-14 B1）：真入口两步 + 留痕
    probe_size_cases(server, fixture)

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
