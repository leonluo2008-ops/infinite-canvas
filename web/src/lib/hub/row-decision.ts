/**
 * AT-12 N1 单行资产决策纯判定（无 React / 无 DOM / 无 fetch，可被 esbuild 打包探针直接断言）。
 *
 * 合同 = docs/nodes/AT-12.md v2（AT-11 §3.2/§3.5 语义升级）+ N1 执行卡：
 * - 决策入口只挂在**非组卡**上（组=原子单位，组内单行决策后端硬拒——先拆开再单独处理）；
 * - 仅「必须换 / 建议准备」行显示「移除（不需要替换，按原片处理）」→ decision=dismissed；
 * - 已 dismissed / 旧 keep_origin 行显示「恢复替换」→ decision=restore（后端**清键**，不写 "restore"）；
 * - dismissed 与旧 keep_origin 在渲染/发号/锁点/入组互斥上**语义全同**（都是「按原片，不要替换」）：
 *   置换区都归「已移除 · 按原片」折叠条，锁点都豁免；
 * - 锁点豁免：有按原片决策的必换行**不进锁点列表**（与后端 lock_remaining 同口径）。
 */

import type { HubAssetCard } from "@/pages/hub/hub-api";

/** 「按原片」决策值域：keep_origin=AT-11 旧值（历史数据继续读）、dismissed=AT-12 移除。 */
export const ORIGIN_DECISIONS = ["keep_origin", "dismissed"] as const;
export type OriginDecision = (typeof ORIGIN_DECISIONS)[number];

/** 该决策值是否「按原片」语义（keep_origin / dismissed 皆算）。 */
export function isOriginDecision(value: string | undefined | null): value is OriginDecision {
    return value === "keep_origin" || value === "dismissed";
}

/** 单行卡当前可用的决策菜单项：null = 不显示入口（可沿用行 / 组内行）。 */
export type RowDecisionAction = { action: "dismissed" | "restore"; label: string } | null;

/**
 * 决策菜单项判定（AT-12 §2.2）：
 * - 组内行（同实体确认 ≥2）→ null（组卡上不出现；竞态窗口里行刚被成组时后端也会硬拒，文案走 decisionErrorText）；
 * - 已按原片（dismissed / keep_origin）→ restore「恢复替换」；
 * - 建议 ∈ {必须换, 建议准备} → dismissed「移除（不需要替换，按原片处理）」；
 * - 其余（可沿用/待核对）→ null。
 */
export function rowDecisionAction(
    card: Pick<HubAssetCard, "建议" | "决策" | "同实体确认">,
): RowDecisionAction {
    if (card.同实体确认.length >= 2) return null;
    if (isOriginDecision(card.决策)) return { action: "restore", label: "恢复替换" };
    if (card.建议 === "必须换" || card.建议 === "建议准备") {
        return { action: "dismissed", label: "移除（不需要替换，按原片处理）" };
    }
    return null;
}

/** 决策行徽标文字（dismissed=「已移除·按原片」，旧 keep_origin=「按原片」）；无覆盖 → ""。 */
export function decisionBadgeText(card: Pick<HubAssetCard, "决策">): string {
    if (card.决策 === "dismissed") return "已移除·按原片";
    if (card.决策 === "keep_origin") return "按原片";
    return "";
}

/** 折叠条文案（AT-12 §2.2）；N=按原片行数（keep_origin ∪ dismissed）。 */
export function originStripLabel(count: number): string {
    return `已移除 · 按原片 ${count} 项`;
}

/** 按原片行号（keep_origin ∪ dismissed，升序去重）——置换区过滤与折叠条成员真源。 */
export function originRowNumbers(cards: Pick<HubAssetCard, "n" | "决策">[]): number[] {
    return [...new Set(cards.filter((card) => isOriginDecision(card.决策)).map((card) => card.n))].sort((a, b) => a - b);
}

/** 置换区在列卡（剔除按原片行）；旧 keep_origin 与 AT-12 dismissed 同归折叠，旧数据不炸。 */
export function activeCards<T extends Pick<HubAssetCard, "n" | "决策">>(cards: T[]): T[] {
    return cards.filter((card) => !isOriginDecision(card.决策));
}

/**
 * 锁点列表（前端即时重算口径，与后端 lock_remaining 同构）：
 * 必换行 −(已传图 ∪ 按原片覆盖行)。移除/恢复后由轮询回刷 detail 派生重算，无本地影子状态。
 */
export function lockRows(cards: Pick<HubAssetCard, "n" | "建议" | "已传图" | "决策">[]): HubAssetCard["n"][] {
    return cards
        .filter((card) => card.建议 === "必须换" && !card.已传图.length && !isOriginDecision(card.决策))
        .map((card) => card.n)
        .sort((a, b) => a - b);
}

/** 锁点计数器（置换区文案行）：必换总数 / 已换 / 未换（进锁点列表的）/ 按原片（豁免的，含已移除）。 */
export function lockCounters(cards: Pick<HubAssetCard, "建议" | "已传图" | "决策">[]): {
    must: number;
    replaced: number;
    unfilled: number;
    keptOrigin: number;
    dismissed: number;
} {
    const mustCards = cards.filter((card) => card.建议 === "必须换");
    const replaced = mustCards.filter((card) => card.已传图.length).length;
    const keptOrigin = mustCards.filter((card) => !card.已传图.length && isOriginDecision(card.决策)).length;
    const dismissed = mustCards.filter((card) => !card.已传图.length && card.决策 === "dismissed").length;
    return { must: mustCards.length, replaced, unfilled: mustCards.length - replaced - keptOrigin, keptOrigin, dismissed };
}

/** 操作成功反馈（点名行号）：dismissed=已移除；keep_origin=旧文案；restore=已恢复替换。 */
export function decisionFeedback(n: number, decision: "dismissed" | "keep_origin" | "restore"): string {
    if (decision === "dismissed") return `#${n} 已移除，按原片处理（锁点已更新，可在「已移除·按原片」里恢复）`;
    if (decision === "keep_origin") return `#${n} 这行不换了，按原片处理（锁点已更新）`;
    return `#${n} 已恢复替换（回到待换状态，锁点已更新）`;
}

/**
 * 降级相关报错文案如实核对（I3）：后端 HubError 原文透出基础上，撞组/锁库两类给出前置行动指引；
 * 其余原文透传，不吞不美化。
 */
export function decisionErrorText(text: string): string {
    if (/确认组|组=原子单位|先拆开/.test(text)) return `该行在确认组里，先拆开再单独处理（${text}）`;
    return text;
}
