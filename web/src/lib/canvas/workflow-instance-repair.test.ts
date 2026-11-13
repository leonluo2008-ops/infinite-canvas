/**
 * P7b 自修闭环单测（node:test；经 `scripts/probe-p7-match-repair.mjs` 打包后真跑）。
 *
 * 覆盖（逐条对卡面 P7b 判据 + P0a §六）：
 *  A 计数口径：每实例「修改→重检」闭环轮次（不按 finding 计）、首检干净不自修、上限 2 轮（D11，传更大值夹回）
 *  B 修好路径：第 1 轮修好 → passed；改后正文**同一份字节**写画布 + 既有保存+重检；新自检绑定同一修订可见
 *  C 升级路径：2 轮仍 FAIL → U1；同 finding 同修法两轮 → U4；读不到全文/保存被拒/无可改范围 → U5；碰冻结项 → U2
 *  D 边界：自修只动正文（冻结项零变化、付费动作恒 0、不豁免判定）
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    createTermStrippingRepairer,
    failingFindings,
    failingSignature,
    findingTerms,
    frozenSnapshotOf,
    repairIdentityOf,
    repairNeeded,
    runInstanceSelfRepair,
    type FrozenSnapshot,
    type InstanceRepairRecord,
    type InstanceRepairer,
} from "@/lib/canvas/workflow-instance-repair";
import { stableDigest } from "@/lib/canvas/workflow-template";
import type { CanvasNodeData } from "@/types/canvas";

const BAD_WORD = "紫色圆柱";
const PRODUCT = "product-1";

/** 注入已知异常的正文（尾部违规词，TAILMARK 式定位：只有尾段带这个词）。 */
function textWithResidue(): string {
    return [
        "# 本片提示词",
        "场景：客户模特在雨夜街头转身，灯箱反光落在面料上。",
        "镜头：开场中景横移，推进环绕半周，收尾缓慢拉远留白。",
        "画面：冷蓝主光 + 暖色轮廓光，浅景深。",
        "本次参考图：@[node:img-1]=女性A",
        `尾段残留：${BAD_WORD}的香水瓶静置桌面，镜头缓慢推近。`,
    ].join("\n");
}

function cleanText(): string {
    return textWithResidue().replace(BAD_WORD, "");
}

/** 生产者 finding 形状（与 `automation/selfcheck.py` 的 v2 finding 同键）。 */
function residueFinding(): Record<string, unknown> {
    return {
        项: "绑图行残留",
        结论: "FAIL",
        证据: `已传图行的原物核心词「${BAD_WORD}」仍留在正文`,
        词: BAD_WORD,
        行号: 6,
        原文: `尾段残留：${BAD_WORD}的香水瓶静置桌面，镜头缓慢推近。`,
        说明: { 结论人话: `提示词里还留着 ${BAD_WORD}`, 发现: [{ 词: BAD_WORD, 行号: 6 }], 统计: { 词数: 1 } },
    };
}

const PASS_ITEMS = [
    { 项: "次序对账", 结论: "PASS" },
    { 项: "绑图行残留", 结论: "PASS" },
    { 项: "锁点对账", 结论: "PASS" },
    { 项: "组绑定一致性", 结论: "PASS" },
];

/** 冻结项来源节点：一条指令模板 + 一个组装 Config（自修不得改动它们）。 */
function frozenNodes(): CanvasNodeData[] {
    return [
        { id: "ins-1", type: "text", title: "转换指令", position: { x: 0, y: 0 }, width: 300, height: 200, metadata: { content: "指令正文", promptTemplate: "seedance-flow" } },
        { id: "cfg-1", type: "config", title: "提示词生成配置", position: { x: 0, y: 0 }, width: 300, height: 200, metadata: { generationMode: "text", composerContent: "拉片：@[node:lap-1]\n本次参考图：@[node:img-1]=女性A" } },
        { id: PRODUCT, type: "text", title: "终稿（本次）", position: { x: 0, y: 0 }, width: 300, height: 200, metadata: { content: textWithResidue() } },
    ];
}

/**
 * 假 hub：**只复刻自检语义的形状**（正文里还有违规词 → 对应项 FAIL；否则全 PASS），
 * 保存时真算审批快照（`stableDigest` 同源）并返回自检条目。真实 hub 的同一语义由 Python 探针验。
 */
function makeHub(seed = textWithResidue()) {
    const calls: { kind: string; text?: string; expectText?: string | null }[] = [];
    let text = seed;
    let snapshotSeq = 0;
    return {
        calls,
        text: () => text,
        client: {
            async saveRevision(input: { localId: string; text: string; expectText?: string | null }) {
                calls.push({ kind: "save", text: input.text, expectText: input.expectText });
                if (input.expectText !== undefined && input.expectText !== null && input.expectText !== text) {
                    throw new Error("正文刚被别处改过（409）：按最新正文再改");
                }
                text = input.text;
                snapshotSeq += 1;
                const failing = text.includes(BAD_WORD);
                return {
                    state: "awaiting_approval",
                    approvalSnapshot: `snap-${stableDigest(text).slice(0, 8)}`,
                    selfcheck: failing ? [...PASS_ITEMS.slice(0, 1), residueFinding(), ...PASS_ITEMS.slice(2)] : PASS_ITEMS,
                };
            },
        },
    };
}

/** 画布侧：写正文 = update_node（生产同口径）；返回最后一次写入的内容便于逐字节对账。 */
function makeCanvas(seed = textWithResidue()) {
    const nodes = frozenNodes().map((node) => (node.id === PRODUCT ? { ...node, metadata: { ...node.metadata, content: seed } } : node));
    const writes: { nodeId: string; text: string }[] = [];
    return {
        nodes,
        writes,
        readText: (nodeId: string) => {
            const node = nodes.find((item) => item.id === nodeId);
            return node ? String(node.metadata?.content ?? "") : null;
        },
        writeCanvasText: (input: { nodeId: string; text: string }) => {
            const node = nodes.find((item) => item.id === input.nodeId);
            if (node) node.metadata = { ...node.metadata, content: input.text, prompt: input.text };
            writes.push({ nodeId: input.nodeId, text: input.text });
        },
    };
}

function runRepair(input: { hub: ReturnType<typeof makeHub>; canvas: ReturnType<typeof makeCanvas>; repairer?: InstanceRepairer; maxRounds?: number; frozen?: () => FrozenSnapshot; nodesAfter?: () => CanvasNodeData[]; findings?: unknown[] }) {
    const frozen = input.frozen || (() => frozenSnapshotOf(input.nodesAfter ? input.nodesAfter() : input.canvas.nodes, "topo-digest"));
    return runInstanceSelfRepair(
        {
            plan: { instanceId: "p7demo#1", templateId: "p7demo", instanceIndex: 1 },
            productNodeId: PRODUCT,
            localId: "auto-9001",
            findings: input.findings || [PASS_ITEMS[0], residueFinding(), PASS_ITEMS[2], PASS_ITEMS[3]],
            maxRounds: input.maxRounds,
        },
        {
            readText: input.canvas.readText,
            writeCanvasText: input.canvas.writeCanvasText,
            saveRevision: input.hub.client.saveRevision,
            repairer: input.repairer || createTermStrippingRepairer(),
            frozen,
            now: () => "2026-11-13T00:00:00.000Z",
        },
    );
}

/** 无效修法执行体：每轮都「改」但改的不是违规词（正文里加一句安慰话），用于验 2 轮升级。 */
function makeIneffectiveRepairer(tag: string): InstanceRepairer {
    return (input) => ({ text: `${input.text}\n补充说明 ${tag}${input.round}：镜头保持平稳。`, reason: `第 ${input.round} 轮补充说明 ${tag}`, removedTerms: [`补充说明${tag}${input.round}`], changedLines: [] });
}

// ─────────────────────────────────────────────── A 计数口径

describe("A 计数口径（每实例闭环轮次，不按 finding 计）", () => {
    it("首检干净 → 不自修（not_needed，0 轮，零保存）", async () => {
        const hub = makeHub(cleanText());
        const canvas = makeCanvas(cleanText());
        const record = await runRepair({ hub, canvas, findings: PASS_ITEMS });
        assert.equal(record.outcome, "not_needed");
        assert.equal(record.rounds.length, 0);
        assert.equal(hub.calls.length, 0);
        assert.equal(repairNeeded(PASS_ITEMS), false);
        assert.equal(repairNeeded([residueFinding()]), true);
    });

    it("一轮里修多条 finding 只消耗 1 轮（不按 finding 计）", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const multi: InstanceRepairer = (input) => {
            const terms = [...new Set(input.findings.flatMap((item) => findingTerms(item)))];
            return { text: input.text.split(BAD_WORD).join(""), reason: `一次删掉 ${terms.join("/")}`, removedTerms: terms, changedLines: [6] };
        };
        const record = await runRepair({ hub, canvas, repairer: multi });
        assert.equal(record.outcome, "passed");
        assert.equal(record.rounds.length, 1, "多条 finding 也只算 1 轮");
    });

    it("上限=2（D11）：传 5 也夹回 2，且第 2 轮后如实升级", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runRepair({ hub, canvas, repairer: makeIneffectiveRepairer("A"), maxRounds: 5 });
        assert.equal(record.maxRounds, 2, "D11 是上限不是旋钮");
        assert.equal(record.rounds.length, 2);
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U1");
        assert.ok(record.finalFindings > 0, "2 轮后仍 FAIL 的项数要如实记");
    });

    it("生产者三态：WARN 不算未通过（告警不是可自修对象），FAIL 才算", async () => {
        // 读路径汇总的「拆解门禁」合成条目是 PASS/WARN/FAIL 三态；把 WARN 当 FAIL 会让自修
        // 对着一个没有词的告警空跑一轮并以 U5 假升级（真任务上表现为莫名「需人工」）。
        const WARN_ITEM = { 项: "拆解门禁", 结论: "WARN", 证据: "告警：镜头 3 缺运镜词", 说明: { 结论人话: "有告警" } };
        assert.equal(repairNeeded([...PASS_ITEMS, WARN_ITEM]), false, "只有 WARN 时不该触发自修");
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runRepair({ hub, canvas, findings: [...PASS_ITEMS, WARN_ITEM] });
        assert.equal(record.outcome, "not_needed");
        assert.equal(record.rounds.length, 0);
        assert.equal(hub.calls.length, 0, "WARN 不该触发任何保存");
        // 同一批里既有 WARN 又有 FAIL：只修 FAIL，且 WARN 不占轮次、不被当成「修好了」
        assert.equal(repairNeeded([...PASS_ITEMS, WARN_ITEM, residueFinding()]), true);
        assert.equal(failingFindings([WARN_ITEM, residueFinding()]).length, 1);
        assert.equal(failingFindings([WARN_ITEM]).length, 0);
        // 结论缺失/认不出：保守按未通过（不把不知道当通过）
        assert.equal(repairNeeded([{ 项: "次序对账" }]), true);
        assert.equal(repairNeeded([{ 项: "次序对账", 结论: "pass" }]), false, "大小写归一");
    });
});

// ─────────────────────────────────────────────── B 修好路径

describe("B 修好路径（第 1 轮成功 + 同一修订可见）", () => {
    it("第 1 轮修好：删掉违规词 → 既有保存+重检 → PASS 绑定同一修订", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runRepair({ hub, canvas });
        assert.equal(record.outcome, "passed");
        assert.equal(record.rounds.length, 1);
        assert.equal(record.finalFindings, 0);
        const round = record.rounds[0];
        assert.equal(round.findingsBefore, 1);
        assert.equal(round.findingsAfter, 0);
        assert.equal(round.edited, true);
        assert.deepEqual(round.removedTerms, [BAD_WORD]);
        assert.ok(round.reason.includes(BAD_WORD), round.reason);
        assert.equal(round.outcome, "pass");
        // 同一修订：保存的正文 == 画布写回正文 == 重检针对的那一份
        assert.equal(hub.calls.length, 1);
        assert.equal(hub.calls[0].text, canvas.writes[0].text);
        assert.equal(round.digestAfter, stableDigest(canvas.writes[0].text));
        assert.equal(round.digestBefore, stableDigest(textWithResidue()));
        assert.ok(round.approvalSnapshot.startsWith("snap-"), round.approvalSnapshot);
        assert.deepEqual(record.finalSelfcheck, PASS_ITEMS);
        // 保存走的是乐观锁（expect_text=改前正文），不是盲写
        assert.equal(hub.calls[0].expectText, textWithResidue());
    });

    it("只改含违规词的那一行，标题/清单行/其它行逐字不动", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runRepair({ hub, canvas });
        const after = canvas.writes[0].text.split("\n");
        const before = textWithResidue().split("\n");
        assert.equal(after.length, before.length);
        assert.deepEqual(after.slice(0, 5), before.slice(0, 5));
        assert.ok(!after[5].includes(BAD_WORD));
        assert.deepEqual(record.rounds[0].changedLines, [6]);
    });

    it("自修痕迹可查：每轮改了什么/为什么/结果都进记录（P8 数据基础）", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runRepair({ hub, canvas });
        const identity = repairIdentityOf(record);
        assert.ok(identity);
        assert.equal(identity?.outcome, "passed");
        assert.equal(identity?.rounds.length, 1);
        assert.deepEqual(identity?.rounds[0].removedTerms, [BAD_WORD]);
        assert.ok(String(identity?.rounds[0].reason || "").length > 0);
        assert.equal(identity?.rounds[0].findingsAfter, 0);
        assert.ok(String(identity?.rounds[0].digestAfter || "").length > 0);
        assert.ok(String(identity?.rounds[0].approvalSnapshot || "").length > 0);
        assert.deepEqual(identity?.rounds[0].scope, ["第 6 行"]);
    });
});

// ─────────────────────────────────────────────── C 升级路径

describe("C 升级路径（U1/U2/U4/U5：交人，不豁免）", () => {
    it("U4：同一 finding 用相同修法两轮仍复现 → 不再空耗，第 2 轮即停", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        // 每轮「改」同一处但都不删违规词，并且理由字符串相同 → 同修法
        const sameMethod: InstanceRepairer = (input) => ({ text: `${input.text}\n（第 ${input.round} 轮复核过）`, reason: "复核正文后保持原样", removedTerms: [], changedLines: [] });
        const record = await runRepair({ hub, canvas, repairer: sameMethod });
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U4", JSON.stringify(record.escalation));
        assert.equal(record.rounds.length, 2);
    });

    it("U5：读不到全文 → 立即交人（不猜正文）", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runInstanceSelfRepair(
            { plan: { instanceId: "p7demo#1", templateId: "p7demo", instanceIndex: 1 }, productNodeId: PRODUCT, localId: "auto-9001", findings: [residueFinding()] },
            { readText: () => null, writeCanvasText: canvas.writeCanvasText, saveRevision: hub.client.saveRevision, repairer: createTermStrippingRepairer() },
        );
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U5");
        assert.equal(record.rounds.length, 0);
        assert.equal(hub.calls.length, 0, "读不到全文就不该去保存");
    });

    it("U5：既有保存被拒（409 乐观锁）→ 交人，且不重试盲写", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record = await runInstanceSelfRepair(
            { plan: { instanceId: "p7demo#1", templateId: "p7demo", instanceIndex: 1 }, productNodeId: PRODUCT, localId: "auto-9001", findings: [residueFinding()] },
            {
                readText: canvas.readText,
                writeCanvasText: canvas.writeCanvasText,
                // 端点口径：别处改过 → 409（不是本卡重试的对象）
                saveRevision: async () => {
                    throw new Error("正文刚被别处改过（409）");
                },
                repairer: createTermStrippingRepairer(),
            },
        );
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U5");
        assert.ok(String(record.escalation?.detail || "").includes("409"), String(record.escalation?.detail));
    });

    it("U5：finding 没有可定位的违规词 → 交人（不自造修法）", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const noLocator = { 项: "组绑定一致性", 结论: "FAIL", 证据: "同号不同 URL" };
        const record = await runInstanceSelfRepair(
            { plan: { instanceId: "p7demo#1", templateId: "p7demo", instanceIndex: 1 }, productNodeId: PRODUCT, localId: "auto-9001", findings: [noLocator] },
            { readText: canvas.readText, writeCanvasText: canvas.writeCanvasText, saveRevision: hub.client.saveRevision, repairer: createTermStrippingRepairer() },
        );
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U5");
        assert.equal(hub.calls.length, 0);
    });

    it("U2：修法把冻结项（composerContent 骨架）也改了 → 立刻交人 + 冻结项问题留痕", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        // 恶意的「修法」：顺手把组装 Config 的 composerContent 也改了（生产 Agent 不该这么干）
        const frozen = () => frozenSnapshotOf(canvas.nodes, "topo-digest");
        const record = await runInstanceSelfRepair(
            { plan: { instanceId: "p7demo#1", templateId: "p7demo", instanceIndex: 1 }, productNodeId: PRODUCT, localId: "auto-9001", findings: [residueFinding()] },
            {
                readText: canvas.readText,
                writeCanvasText: (input) => {
                    canvas.writeCanvasText(input);
                    const config = canvas.nodes.find((node) => node.id === "cfg-1");
                    if (config) config.metadata = { ...config.metadata, composerContent: `拉片：@[node:lap-1]\n${input.text.slice(0, 40)}\n本次参考图：@[node:img-1]=女性A` };
                },
                saveRevision: hub.client.saveRevision,
                repairer: createTermStrippingRepairer(),
                frozen,
            },
        );
        assert.equal(record.outcome, "escalated");
        assert.equal(record.escalation?.code, "U2", JSON.stringify(record.escalation));
        assert.equal(record.frozen.unchanged, false);
        assert.ok(record.frozen.problems.some((line) => line.includes("composerContent 骨架")), record.frozen.problems.join(" | "));
    });
});

// ─────────────────────────────────────────────── D 边界

describe("D 边界（只动正文 / 冻结项零变化 / 零付费）", () => {
    it("正常自修：冻结项前后零变化、付费动作恒 0、判定口径不自造", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const before = JSON.stringify(frozenNodes().map((node) => ({ id: node.id, composer: node.metadata?.composerContent, tpl: node.metadata?.promptTemplate })));
        const record = await runRepair({ hub, canvas });
        const after = JSON.stringify(canvas.nodes.map((node) => ({ id: node.id, composer: node.metadata?.composerContent, tpl: node.metadata?.promptTemplate })));
        assert.equal(after, before, "冻结项（指令模板 id / composerContent）被改了");
        assert.equal(record.frozen.unchanged, true, record.frozen.problems.join(" | "));
        assert.equal(record.paidActions, 0);
        // 判定口径：自修只消费 hub 返回的 结论 字段，不产生自己的 PASS/FAIL
        assert.equal(record.finalSelfcheck.length, 4);
        assert.ok(record.rounds.every((round) => round.findingsAfter === failingFindings(record.finalSelfcheck).length || round.outcome === "pass"));
    });

    it("failingSignature 对同一条 finding 稳定，换词就变（U4 判据可用）", () => {
        const first = failingSignature([residueFinding()]);
        assert.equal(failingSignature([residueFinding()]), first);
        assert.notEqual(failingSignature([{ ...residueFinding(), 词: "另一个词" }]), first);
        assert.equal(failingSignature(PASS_ITEMS), failingSignature([]), "全 PASS 与空集同签名");
    });

    it("确定性执行体只删 finding 给的词，不碰标题/清单行；找不到词就 U5 不自造", () => {
        const repairer = createTermStrippingRepairer();
        const edit = repairer({ round: 1, text: `# 标题 ${BAD_WORD}\n本次参考图：@[node:img-1]=${BAD_WORD}\n正文含 ${BAD_WORD} 一行`, findings: [residueFinding()], history: [] });
        assert.ok(!("stop" in edit));
        const text = (edit as { text: string }).text;
        assert.ok(text.startsWith(`# 标题 ${BAD_WORD}`), "标题行不该被改");
        assert.ok(text.includes(`本次参考图：@[node:img-1]=${BAD_WORD}`), "清单行不该被改（图号真源）");
        const missing = repairer({ round: 1, text: "正文里没有那个词", findings: [residueFinding()], history: [] });
        assert.ok("stop" in missing && missing.stop.code === "U5");
    });
});

/** 记录形状冒烟：必填字段齐（P8 侧栏直接消费）。 */
describe("E 记录形状", () => {
    it("InstanceRepairRecord 字段齐：轮次/digest/快照/冻结/付费/末次自检", async () => {
        const hub = makeHub();
        const canvas = makeCanvas();
        const record: InstanceRepairRecord = await runRepair({ hub, canvas });
        for (const key of ["schemaVersion", "instanceId", "templateId", "instanceIndex", "maxRounds", "rounds", "outcome", "finalFindings", "frozen", "paidActions", "finalSelfcheck"] as const) {
            assert.ok(key in record, `缺字段 ${key}`);
        }
        assert.equal(record.schemaVersion, 1);
        assert.equal(record.maxRounds, 2);
        assert.equal(record.instanceId, "p7demo#1");
    });
});
