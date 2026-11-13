/**
 * P7b · Agent 自修闭环（B0-D4 落地，2 轮上限 D11）。
 *
 * 依据（条款冻结）：
 * - B0 D4：「质检异常=Agent 自修优先（自动改→重跑→仍不过升级给人）」；D11：「Agent 自动自修最多 2 轮，
 *   超限升级给人」；C8：全文读取通道（`read_node_text`）已就位，自修必须读到**全文**才动手。
 * - 审计 G16：自检修后**必须走 hub 既有保存/重检入口并绑定同一修订**，禁另写正则评分器 —— 所以本文件
 *   **不产出任何 PASS/FAIL 判定**：finding 一律来自 hub 自检生产者（`selfcheck.run_selfcheck`，
 *   `hub_api.save_prompt_text` 透出），重检也只调既有保存端点（写正文 → 重算快照 → 重跑自检）。
 * - 审计红线表「自检/正文修改」行：消费生产者 finding，调用既有保存与重检入口；**不把「自动修好」与
 *   「自动豁免失败」混为一谈** —— 本文件既不豁免 finding、也不改判定口径，修不好就升级给人。
 * - P0a §六（自修边界细化）：§6.1 计数口径=**每实例「修改→重检」完整闭环轮次**（与 finding 数量无关，
 *   不按 finding 计）；§6.2 升级条件 U1-U5（本文件逐条落地，见 `RepairStopCode`）。
 *
 * 边界：
 * - 自修**只动正文**（文本链，非付费）：不生成任何 `run_generation`、不碰拓扑/指令模板/composerContent 骨架/
 *   五槽结构（每轮前后各取一次冻结快照，变了立刻停 U2）。付费重跑必须另过 P0b 确认（不在本文件）。
 * - 不落「现在怎么样」：`InstanceRepairRecord` 是**事件痕迹**（第几轮、改了什么、为什么、结果如何），
 *   实例状态仍由 `workflow-batch-status.ts` 现查派生（C5 / §5.3）。
 */

import { composerSkeletonDigest } from "@/lib/canvas/workflow-instance";
import { stableDigest } from "@/lib/canvas/workflow-template";
import type { CanvasNodeData } from "@/types/canvas";

/** D11 上限：2 轮。这是上限，不是可调参数——调用方传更大值也夹回 2。 */
export const REPAIR_MAX_ROUNDS = 2;

/** 升级给人的原因代号（P0a §6.2 一一对应）。 */
export type RepairStopCode = "U1" | "U2" | "U3" | "U4" | "U5";

export const REPAIR_STOP_TEXT: Record<RepairStopCode, string> = {
    U1: "2 轮自修耗尽仍有未通过项（D11 字面）：实例交人，不自动豁免、不自动通过",
    U2: "修复需触碰冻结项（指令模板 id / generationMode / composerContent 骨架 / 骨架拓扑）：自修不改冻结项，交人",
    U3: "修复动作属付费类（视频生成/付费重跑）：自修只做非付费自动段，付费一律人在场",
    U4: "同一 finding 连续两轮用相同修法仍复现：防等价修法死循环空耗轮次，交人",
    U5: "自修/重检通路本身没走通（读不到全文 / 保存被拒 / finding 无可定位范围）：基础设施异常不是可自修对象，交人排查",
};

/** hub 自检条目（生产者 finding；字段口径见 `automation/selfcheck.py` 的 v2 finding）。 */
export type InstanceRepairFinding = {
    项?: unknown;
    结论?: unknown;
    文字?: unknown;
    原文?: unknown;
    词?: unknown;
    原词?: unknown;
    片段?: unknown;
    行号?: unknown;
    图号?: unknown;
    序号?: unknown;
    说明?: unknown;
    [key: string]: unknown;
};

function textOf(value: unknown): string {
    return typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
}

/**
 * 一条 finding 是否未通过（口径就是生产者的 `结论`，本文件不重判）。
 *
 * 生产者是**三态**（`automation/selfcheck.py`：自检四项只有 PASS/FAIL，读路径汇总的「拆解门禁」
 * 合成条目是 PASS/WARN/FAIL）：`WARN` 是告警——面板展示给人看，**不是可自修对象**，把它当未通过
 * 会让自修对着一个没有词的告警空跑一轮并以 U5 假升级。所以只有明确 FAIL 才触发自修；
 * 结论缺失/认不出的条目保守按未通过处理（不把不知道当通过）。
 */
export function isFailingFinding(item: unknown): boolean {
    const record = (item || {}) as InstanceRepairFinding;
    const verdict = textOf(record.结论).trim().toUpperCase();
    if (verdict === "PASS" || verdict === "WARN") return false;
    return true;
}

/** 未通过项集合（消费生产者 finding，按原顺序）。 */
export function failingFindings(items: unknown[]): InstanceRepairFinding[] {
    return (items || []).map((item) => (item || {}) as InstanceRepairFinding).filter((item) => isFailingFinding(item));
}

/**
 * finding 里可定位的违规词（自修「限定修改范围」的入口）：
 * v2 finding 的 `词`/`原词`/`片段`，以及折叠区 `说明.发现[].词`；都不带就返回空。
 */
export function findingTerms(item: InstanceRepairFinding): string[] {
    const terms = new Set<string>();
    for (const field of ["词", "原词", "片段"] as const) {
        const value = textOf(item[field]).trim();
        if (value) terms.add(value);
    }
    const say = item.说明 as Record<string, unknown> | undefined;
    const findings = say && Array.isArray(say.发现) ? (say.发现 as Record<string, unknown>[]) : [];
    for (const finding of findings) {
        for (const field of ["词", "原词", "片段"]) {
            const value = textOf(finding?.[field]).trim();
            if (value) terms.add(value);
        }
    }
    return [...terms];
}

/** finding 的位置描述（人话；P8 侧栏展示「改的是哪一行」）。 */
export function findingLocators(item: InstanceRepairFinding): string[] {
    const out: string[] = [];
    for (const field of ["行号", "图号", "序号", "句序"] as const) {
        const value = textOf(item[field]).trim();
        if (value) out.push(`${field}=${value}`);
    }
    return out;
}

/** 未通过项的稳定签名（升级条件 U4 的「同一 finding」判据）。 */
export function failingSignature(items: unknown[]): string {
    return stableDigest(
        failingFindings(items)
            .map((item) => [textOf(item.项), findingTerms(item).sort().join("/"), findingLocators(item).join(",")].join(":"))
            .sort()
            .join("|"),
    );
}

/** 冻结项快照（自修前后各取一次；零变化才算「没碰冻结项」）。 */
export type FrozenSnapshot = {
    topologyDigest: string;
    instructionIds: string[];
    composerSkeletonDigest: string;
};

/** 从画布节点 + 实例拓扑 digest 取冻结快照（复用 P3 的骨架 digest 同一实现）。 */
export function frozenSnapshotOf(nodes: CanvasNodeData[], topologyDigest: string): FrozenSnapshot {
    const instructions = nodes
        .filter((node) => node.metadata?.promptTemplate !== undefined)
        .map((node) => `${node.id}:${textOf(node.metadata?.promptTemplate)}`)
        .sort();
    const composers = nodes
        .filter((node) => String(node.metadata?.composerContent || "").trim())
        .map((node) => `${node.id}:${composerSkeletonDigest(String(node.metadata?.composerContent || ""))}`)
        .sort();
    return { topologyDigest: String(topologyDigest || ""), instructionIds: instructions, composerSkeletonDigest: stableDigest(composers.join("|")) };
}

export function frozenChanged(before: FrozenSnapshot | undefined, after: FrozenSnapshot | undefined): string[] {
    if (!before || !after) return [];
    const problems: string[] = [];
    if (before.topologyDigest !== after.topologyDigest) problems.push(`骨架拓扑变了：${before.topologyDigest} → ${after.topologyDigest}`);
    if (JSON.stringify(before.instructionIds) !== JSON.stringify(after.instructionIds)) problems.push(`指令模板 id 变了：${before.instructionIds.join(",")} → ${after.instructionIds.join(",")}`);
    if (before.composerSkeletonDigest !== after.composerSkeletonDigest) problems.push(`composerContent 骨架变了：${before.composerSkeletonDigest} → ${after.composerSkeletonDigest}`);
    return problems;
}

// ─────────────────────────────────────────────── 自修执行体（Agent 侧）

export type RepairEdit = {
    /** 修改后的正文（全文）。 */
    text: string;
    /** 为什么这么改（人话，进痕迹）。 */
    reason: string;
    /** 改了哪几行（1-based，展示用）。 */
    changedLines?: number[];
    /** 从正文里删掉的违规词（finding 驱动；展示与 U4 同修法判定用）。 */
    removedTerms?: string[];
    /** 限定修改的范围说明（哪几处/哪一节）。 */
    scope?: string[];
};

export type RepairStop = { stop: { code: RepairStopCode; detail: string } };

export type InstanceRepairerInput = {
    round: number;
    /** read_node_text 原文（逐字全文，未截断）。 */
    text: string;
    /** 本轮未通过项（生产者 finding，原样传入，不自造）。 */
    findings: InstanceRepairFinding[];
    history: RepairRoundTrace[];
};

/** 自修执行体：生产 = 画布对话 Agent（读 read_node_text → 限定范围改正文）；探针 = 下面的确定性执行体。 */
export type InstanceRepairer = (input: InstanceRepairerInput) => RepairEdit | RepairStop | Promise<RepairEdit | RepairStop>;

export function isRepairStop(value: RepairEdit | RepairStop): value is RepairStop {
    return Boolean((value as RepairStop)?.stop);
}

/** 正文里的清单行前缀（与 lapian-assets 同源默认值）：自修不动清单行（图号真源）。 */
const MANIFEST_PREFIXES = ["本次参考图：", "本次参考图:"];

function isProtectedLine(line: string): boolean {
    const head = line.trim();
    if (!head) return true;
    if (head.startsWith("#")) return true;                                        // 标题（结构）
    if (MANIFEST_PREFIXES.some((prefix) => head.startsWith(prefix))) return true;   // 清单行（图号真源）
    return false;
}

/** 删词后收拾标点/空白：不制造重复标点、不留空行壳。 */
function tidyAfterRemoval(line: string): string {
    return line
        .replace(/[ \t]{2,}/g, " ")
        .replace(/[，、]{2,}/g, "，")
        .replace(/^[，、。；;,\s]+/, "")
        .replace(/[，、]{2,}/g, "，")
        .trimEnd();
}

/**
 * 确定性自修执行体（**修复动作**，不是判定器）：按 finding 给的违规词，从正文里删掉这些词。
 *
 * 为什么可以做成确定性：卡面判据要求「用 TAILMARK 式定位注入的违规词」→ 删掉即可 PASS，且
 * 生产侧 Agent 干的是同一件事（读全文 → 按 finding 限定范围改正文）。判定仍全部由 hub 自检出，
 * 本执行体从不说「现在 PASS 了」。
 */
export function createTermStrippingRepairer(options: { maxTermsPerRound?: number } = {}): InstanceRepairer {
    const maxTerms = Math.max(1, Number(options.maxTermsPerRound ?? 8));
    return (input) => {
        const terms = [...new Set(input.findings.flatMap((item) => findingTerms(item)))].slice(0, maxTerms);
        if (!terms.length) {
            return { stop: { code: "U5", detail: "未通过项没有可定位的违规词（finding 不带 词/原词/片段）：自修执行体读不到可改范围" } };
        }
        const lines = String(input.text || "").split("\n");
        const changedLines: number[] = [];
        const removed: string[] = [];
        const next = lines.map((line, index) => {
            if (isProtectedLine(line)) return line;
            let working = line;
            for (const term of terms) {
                if (!working.includes(term)) continue;
                working = working.split(term).join("");
                if (!removed.includes(term)) removed.push(term);
            }
            if (working === line) return line;
            const tidied = tidyAfterRemoval(working);
            changedLines.push(index + 1);
            return tidied.trim() ? tidied : "";
        });
        if (!removed.length) {
            return { stop: { code: "U5", detail: `finding 指向的词（${terms.join("/")}）在正文里找不到：自修执行体无法定位，交人排查` } };
        }
        const text = next.join("\n").replace(/\n{3,}/g, "\n\n").trimEnd();
        return {
            text,
            reason: `按 finding 删掉违规词「${removed.join("/")}」（只动含该词的行，标题/清单行不动）`,
            changedLines,
            removedTerms: removed,
            scope: changedLines.map((line) => `第 ${line} 行`),
        };
    };
}

// ─────────────────────────────────────────────── 轮次痕迹（落 BatchRecord 身份结构）

export type RepairRoundTrace = {
    round: number;
    at: string;
    /** 本轮开始时未通过项数（生产者结论口径）。 */
    findingsBefore: number;
    failingItemsBefore: string[];
    /** 本轮是否真的改了正文（没改 = 执行体没找到可改范围，按 stop 处理）。 */
    edited: boolean;
    changedLines: number[];
    removedTerms: string[];
    reason: string;
    scope: string[];
    /** 修订 digest（改前/改后）：同一修订可见的判据。 */
    digestBefore: string;
    digestAfter: string;
    /** hub 保存回执的审批快照（新自检结果绑定这一版修订）。 */
    approvalSnapshot: string;
    /** 保存后重检的未通过项数。 */
    findingsAfter: number;
    outcome: "pass" | "still_failing";
    /** 本轮命中的升级原因（有值即停）。 */
    stop?: { code: RepairStopCode; detail: string };
};

/** 实例自修身份记录（事件痕迹；不含「现在怎么样」）。 */
export type InstanceRepairRecord = {
    schemaVersion: 1;
    instanceId: string;
    templateId: string;
    instanceIndex: number;
    /** 上限（D11 固定 2；写进记录便于审计「为什么停在第 2 轮」）。 */
    maxRounds: number;
    rounds: RepairRoundTrace[];
    outcome: "passed" | "escalated" | "not_needed";
    escalation?: { code: RepairStopCode; detail: string; round: number };
    /** 末次自检未通过项数（0 = 修好；>0 = 交人）。 */
    finalFindings: number;
    /** 冻结项快照（自修前后零变化才算合规）。 */
    frozen: { before?: FrozenSnapshot; after?: FrozenSnapshot; unchanged: boolean; problems: string[] };
    /** 自修期间的付费动作数（恒 0：自修只走文本链）。 */
    paidActions: number;
    /** 末次自检原始条目（P8 侧栏直接渲染，不需要再查 hub）。 */
    finalSelfcheck: unknown[];
};

export type InstanceRepairDeps = {
    /** 读正文全文（生产 = `readNodeFullText`/read_node_text 通道；探针 = 直接读画布产物节点）。 */
    readText: (nodeId: string) => string | null;
    /** 把改好的正文写回画布产物节点（画布是唯一操作面：画布与 hub 必须同一份字节）。 */
    writeCanvasText: (input: { nodeId: string; text: string }) => void | Promise<void>;
    /** **既有**保存+重检入口（`hub_api.save_prompt_text` 语义：写正文 → 重算快照 → 重跑自检）。 */
    saveRevision: (input: { localId: string; text: string; expectText?: string | null }) => Promise<{ selfcheck?: unknown[]; approvalSnapshot?: string; state?: string; unchanged?: boolean }>;
    repairer: InstanceRepairer;
    /** 冻结项快照（每轮前后各取一次）；缺省 = 不校验（探针里会显式传）。 */
    frozen?: () => FrozenSnapshot;
    now?: () => string;
    log?: (message: string) => void;
};

/** 首轮自检就有未通过项时才需要自修；否则 `not_needed`。 */
export function repairNeeded(findings: unknown[]): boolean {
    return failingFindings(findings).length > 0;
}

/**
 * 跑自修闭环：≤2 轮「读全文 → 限定修改 → 既有保存+重检」。
 *
 * 每轮顺序（不可换）：read_node_text 全文 → repairer 改 → 写回画布 → **既有保存端点**（顺带重检）
 * → 冻结项比对 → 判停。停止条件：
 * - 重检 0 项未通过 → `passed`（自动**修好**）；
 * - 命中 U1-U5 → `escalated`（交人，**不豁免** finding、不改判定口径、不进下一步）。
 */
export async function runInstanceSelfRepair(
    input: {
        plan: { instanceId: string; templateId: string; instanceIndex: number };
        /** 画布侧的正文产物节点（read_node_text 的 nodeId）。 */
        productNodeId: string;
        /** hub 任务 id（既有保存端点的 localId）。 */
        localId: string;
        /** 首轮未通过项（hub 自检生产者原样透出）。 */
        findings: unknown[];
        /** 上限（传 >2 夹回 2：D11 是上限不是旋钮）。 */
        maxRounds?: number;
    },
    deps: InstanceRepairDeps,
): Promise<InstanceRepairRecord> {
    const now = deps.now || (() => new Date().toISOString());
    const log = deps.log || (() => {});
    const maxRounds = Math.min(REPAIR_MAX_ROUNDS, Math.max(1, Number(input.maxRounds ?? REPAIR_MAX_ROUNDS)));
    const record: InstanceRepairRecord = {
        schemaVersion: 1,
        instanceId: input.plan.instanceId,
        templateId: input.plan.templateId,
        instanceIndex: input.plan.instanceIndex,
        maxRounds,
        rounds: [],
        outcome: "not_needed",
        finalFindings: failingFindings(input.findings).length,
        frozen: { unchanged: true, problems: [] },
        paidActions: 0,
        finalSelfcheck: Array.isArray(input.findings) ? input.findings : [],
    };
    if (!repairNeeded(input.findings)) return record;

    const frozenBefore = deps.frozen?.();
    record.frozen.before = frozenBefore;
    let currentFindings: unknown[] = input.findings || [];

    for (let round = 1; round <= maxRounds; round += 1) {
        const before = currentFindings;
        const failingBefore = failingFindings(before);
        const text = deps.readText(input.productNodeId);
        if (text === null || text === undefined) {
            record.outcome = "escalated";
            record.escalation = { code: "U5", detail: `读不到产物节点 ${input.productNodeId} 的正文全文（read_node_text 通道没给回文本）`, round };
            break;
        }
        const workingText = String(text).trim();
        const digestBefore = stableDigest(workingText);

        let edit: RepairEdit | RepairStop;
        try {
            edit = await deps.repairer({ round, text: workingText, findings: failingBefore, history: record.rounds });
        } catch (error) {
            record.outcome = "escalated";
            record.escalation = { code: "U5", detail: `自修执行体异常：${(error as Error).message}`, round };
            break;
        }
        if (isRepairStop(edit)) {
            record.outcome = "escalated";
            record.escalation = { code: edit.stop.code, detail: edit.stop.detail, round };
            break;
        }
        const nextText = String(edit.text || "").trim();
        if (!nextText) {
            record.outcome = "escalated";
            record.escalation = { code: "U5", detail: "自修执行体给出空正文（保存端点会拒）：交人排查", round };
            break;
        }

        // 写回画布（画布=唯一操作面）+ 既有保存端点（同一字节 → 同一修订）
        await deps.writeCanvasText({ nodeId: input.productNodeId, text: nextText });
        let saved: Awaited<ReturnType<InstanceRepairDeps["saveRevision"]>>;
        try {
            saved = await deps.saveRevision({ localId: input.localId, text: nextText, expectText: workingText });
        } catch (error) {
            record.outcome = "escalated";
            record.escalation = { code: "U5", detail: `既有保存+重检被拒：${(error as Error).message}`, round };
            break;
        }
        const after = Array.isArray(saved.selfcheck) ? saved.selfcheck : [];
        const trace: RepairRoundTrace = {
            round,
            at: now(),
            findingsBefore: failingBefore.length,
            failingItemsBefore: failingBefore.map((item) => textOf(item.项) || "（未命名项）"),
            edited: nextText !== workingText,
            changedLines: edit.changedLines || [],
            removedTerms: edit.removedTerms || [],
            reason: edit.reason || "",
            scope: edit.scope || [],
            digestBefore,
            digestAfter: stableDigest(nextText),
            approvalSnapshot: String(saved.approvalSnapshot || ""),
            findingsAfter: failingFindings(after).length,
            outcome: failingFindings(after).length ? "still_failing" : "pass",
        };
        record.rounds.push(trace);
        record.finalSelfcheck = after;
        record.finalFindings = trace.findingsAfter;
        log(`[repair] 实例 ${input.plan.instanceId} 第 ${round} 轮：${trace.findingsBefore} → ${trace.findingsAfter} 未通过（${edit.reason}）`);

        // 冻结项零变化（自修只动正文；一旦变了立刻交人，U2）
        const frozenAfter = deps.frozen?.();
        const frozenProblems = frozenChanged(frozenBefore, frozenAfter);
        if (frozenProblems.length) {
            record.frozen = { before: frozenBefore, after: frozenAfter, unchanged: false, problems: frozenProblems };
            trace.outcome = "still_failing";
            trace.stop = { code: "U2", detail: `自修触碰冻结项：${frozenProblems.join("；")}` };
            record.outcome = "escalated";
            record.escalation = { code: "U2", detail: trace.stop.detail, round };
            break;
        }

        if (!trace.findingsAfter) {
            record.outcome = "passed";
            record.frozen = { before: frozenBefore, after: frozenAfter, unchanged: true, problems: [] };
            break;
        }

        // U4：同一 finding 连续两轮采用相同修法仍复现
        const previous = record.rounds[record.rounds.length - 2];
        if (previous) {
            const sameFindings = failingSignature(after) === failingSignature(before);
            const sameMethod = stableDigest([previous.reason, previous.removedTerms.join("/")].join("|")) === stableDigest([trace.reason, trace.removedTerms.join("/")].join("|"));
            if (sameFindings && sameMethod) {
                trace.stop = { code: "U4", detail: `同一 finding（${trace.failingItemsBefore.join("/")}）用相同修法（${trace.reason}）两轮仍复现` };
                record.outcome = "escalated";
                record.escalation = { code: "U4", detail: trace.stop.detail, round };
                record.frozen = { before: frozenBefore, after: frozenAfter, unchanged: true, problems: [] };
                break;
            }
        }

        currentFindings = after;
        record.frozen = { before: frozenBefore, after: frozenAfter, unchanged: true, problems: [] };
        if (round === maxRounds) {
            trace.stop = { code: "U1", detail: `${maxRounds} 轮自修后仍有 ${trace.findingsAfter} 项未通过` };
            record.outcome = "escalated";
            record.escalation = { code: "U1", detail: trace.stop.detail, round };
            break;
        }
    }

    if (record.outcome === "escalated" && !record.escalation) {
        record.escalation = { code: "U5", detail: "自修闭环未收敛且未记录原因（不应发生，按通路异常交人）", round: record.rounds.length };
    }
    return record;
}

/** 自修记录 → 批次成员身份侧的紧凑投影（轮次痕迹保留：P8 侧栏要能查「改了什么/为什么/结果」）。 */
export type InstanceRepairIdentity = {
    outcome: InstanceRepairRecord["outcome"];
    maxRounds: number;
    rounds: { round: number; findingsBefore: number; findingsAfter: number; removedTerms: string[]; reason: string; changedLines: number[]; digestAfter: string; approvalSnapshot: string; scope: string[] }[];
    escalation?: { code: RepairStopCode; detail: string; round: number };
    finalFindings: number;
};

export function repairIdentityOf(record: InstanceRepairRecord | null | undefined): InstanceRepairIdentity | undefined {
    if (!record) return undefined;
    return {
        outcome: record.outcome,
        maxRounds: record.maxRounds,
        rounds: record.rounds.map((round) => ({
            round: round.round,
            findingsBefore: round.findingsBefore,
            findingsAfter: round.findingsAfter,
            removedTerms: round.removedTerms,
            reason: round.reason,
            changedLines: round.changedLines,
            digestAfter: round.digestAfter,
            approvalSnapshot: round.approvalSnapshot,
            scope: round.scope,
        })),
        escalation: record.escalation,
        finalFindings: record.finalFindings,
    };
}
