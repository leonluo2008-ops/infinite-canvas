/**
 * AT-11 M3 单行降级纯判定（无 React / 无 DOM / 无 fetch，可被 esbuild 打包探针直接断言）。
 *
 * 合同 = docs/nodes/AT-11.md v4 §3.2/§3.5/§3.6（后端 set_asset decision 已在 M1 落地，9775195）：
 * - 降级入口只挂在**非组卡**上（组=原子单位，组内单行降级后端硬拒——先拆开再单独处理）；
 * - 仅「必须换 / 建议准备」行显示「这行不换了（按原片）」；可沿用行不需要降级，不给入口；
 * - 已 keep_origin 行显示「恢复替换」（restore）；
 * - 锁点豁免：有 keep_origin 覆盖的必换行**不进锁点列表**（与后端 lock_remaining 同口径）；
 * - 徽标「按原片」与必换红 / 建议蓝区分（灰·虚线由渲染层落样式，本模块管文字非空）。
 */

import type { HubAssetCard } from "@/pages/hub/hub-api";

/** 单行卡当前可用的降级菜单项：null = 不显示入口（可沿用行 / 组内行）。 */
export type RowDecisionAction = { action: "keep_origin" | "restore"; label: string } | null;

/**
 * 降级菜单项判定（§3.2）：
 * - 组内行（同实体确认 ≥2）→ null（组卡上不出现；竞态窗口里行刚被成组时后端也会硬拒，文案走 decisionErrorText）；
 * - 决策=keep_origin → restore「恢复替换」；
 * - 建议 ∈ {必须换, 建议准备} → keep_origin「这行不换了（按原片）」；
 * - 其余（可沿用/待核对）→ null。
 */
export function rowDecisionAction(
    card: Pick<HubAssetCard, "建议" | "决策" | "同实体确认">,
): RowDecisionAction {
    if (card.同实体确认.length >= 2) return null;
    if (card.决策 === "keep_origin") return { action: "restore", label: "恢复替换" };
    if (card.建议 === "必须换" || card.建议 === "建议准备") {
        return { action: "keep_origin", label: "这行不换了（按原片）" };
    }
    return null;
}

/** 降级行徽标文字（§3.2「按原片」）；恒非空仅当该行确有 keep_origin 覆盖，供探针断言。 */
export function decisionBadgeText(card: Pick<HubAssetCard, "决策">): string {
    return card.决策 === "keep_origin" ? "按原片" : "";
}

/**
 * 锁点列表（前端即时重算口径，与后端 lock_remaining 同构）：
 * 必换行 −(已传图 ∪ keep_origin 覆盖行)。降级/恢复后由轮询回刷 detail 派生重算，无本地影子状态。
 */
export function lockRows(cards: Pick<HubAssetCard, "n" | "建议" | "已传图" | "决策">[]): HubAssetCard["n"][] {
    return cards
        .filter((card) => card.建议 === "必须换" && !card.已传图.length && card.决策 !== "keep_origin")
        .map((card) => card.n)
        .sort((a, b) => a - b);
}

/** 锁点计数器（置换区文案行）：必换总数 / 已换 / 未换（进锁点列表的）/ 按原片（豁免的）。 */
export function lockCounters(cards: Pick<HubAssetCard, "建议" | "已传图" | "决策">[]): {
    must: number;
    replaced: number;
    unfilled: number;
    keptOrigin: number;
} {
    const mustCards = cards.filter((card) => card.建议 === "必须换");
    const replaced = mustCards.filter((card) => card.已传图.length).length;
    const keptOrigin = mustCards.filter((card) => !card.已传图.length && card.决策 === "keep_origin").length;
    return { must: mustCards.length, replaced, unfilled: mustCards.length - replaced - keptOrigin, keptOrigin };
}

/** 操作成功反馈（点名行号）：keep = 这行不换了（按原片）；restore = 已恢复替换。 */
export function decisionFeedback(n: number, decision: "keep_origin" | "restore"): string {
    return decision === "keep_origin" ? `#${n} 这行不换了，按原片处理（锁点已更新）` : `#${n} 已恢复替换（回到待换状态，锁点已更新）`;
}

/**
 * 降级相关报错文案如实核对（I3）：后端 HubError 原文透出基础上，撞组/锁库两类给出前置行动指引；
 * 其余原文透传，不吞不美化。
 */
export function decisionErrorText(text: string): string {
    if (/确认组|组=原子单位|先拆开/.test(text)) return `该行在确认组里，先拆开再单独处理（${text}）`;
    return text;
}
