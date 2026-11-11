/**
 * AT-11 M2 / AT-12 N1 快速合并纯判定（无 React / 无 DOM / 无 fetch，可被 esbuild 打包探针直接断言）。
 *
 * 合同 = docs/nodes/AT-11.md v4 §3.1/§3.1.1/§3.6 + AT-12 N1（勾选式合并，替换模式切换）：
 * - 勾选态只存**行号**（I2：轮询换 detail 对象不丢选中，合并成功清空、失败保留）；
 * - 每张非组卡左上常驻 checkbox（不再有「进入/退出多选模式」）；已确认组卡不挂 checkbox
 *   （先在组里=先拆开）——前端只做先行拦截，后端同规则硬拒；
 * - AT-16 B7：**品牌视觉行与普通行同权可合并**（片尾可能无品牌露出、品牌标签可能就是产品本身），
 *   前后端都不再有品牌专用拒绝——通用拒绝只剩「已在组/按原片/已移除/行不存在」；
 * - 按原片行（dismissed / keep_origin）已从置换区折叠移除，不可勾选；
 * - 已传图三分支（B3）：零图 / 恰一张唯一图 / 多张不同图（多张必须用户点名保留一张，绝不悄悄任选）；
 * - 合并卡（单帧单框）：徽标「#1+#3+#5 已合并」、框恒=主行自身框那一枚；主行无框 → null（诚实降级，
 *   绝不搬组内其他行的框充数）。
 */

import type { HubAssetCard, HubCardGroup } from "@/pages/hub/hub-api";
import { isOriginDecision } from "@/lib/hub/row-decision";

/** 一张卡在勾选态里可选吗：selectable=false 时 reason 是给用户看的置灰原因（非空）。 */
export type MergeSelectable = { selectable: boolean; reason: string };

/**
 * 可选性判定（§3.1/§3.1.1 + AT-12；AT-16 B7 取消品牌例外）：
 * 按原片行 → 已折叠；已确认组 → 先拆开；其余（含品牌视觉行）一律可选。
 * 入参形状保留「类别」只为卡对象直传，判定不再按类别分支。
 */
export function mergeSelectable(card: Pick<HubAssetCard, "类别" | "同实体确认"> & { 决策?: string }): MergeSelectable {
    if (isOriginDecision(card.决策)) return { selectable: false, reason: "已移除（按原片）·不参与合并" };
    if (card.同实体确认.length >= 2) return { selectable: false, reason: "已确认组·先拆开" };
    return { selectable: true, reason: "" };
}

/** 纯切换一行选中（返回新 Set，不改原集合；勾选态=行号 Set，禁存卡对象引用）。 */
export function toggleMergeRow(selected: Set<number>, n: number): Set<number> {
    const next = new Set(selected);
    if (next.has(n)) next.delete(n);
    else next.add(n);
    return next;
}

/**
 * 轮询收口（I2）：勾选集合只保留「仍存在且仍可选」的行；被成组/被移除/行消失的自动剔出
 * （底部浮条计数诚实）。**没变化时返回原 Set 引用**——调用方可据此跳过 setState，避免轮询抖动。
 */
export function pruneSelection(
    selected: Set<number>,
    cards: (Pick<HubAssetCard, "n" | "类别" | "同实体确认"> & { 决策?: string })[],
): Set<number> {
    const keep = new Set<number>();
    for (const card of cards) {
        if (selected.has(card.n) && mergeSelectable(card).selectable) keep.add(card.n);
    }
    if (keep.size === selected.size && [...selected].every((n) => keep.has(n))) return selected;
    return keep;
}

/** 底部浮条计数文案（AT-12 N3：浮条同时承载合并/移除 → 改中性「已勾选 N 项」）。 */
export function mergeBarText(count: number): string {
    return `已勾选 ${count} 项`;
}

/** 已传图三分支（B3）判定结果。 */
export type MergeImageBranch =
    | { branch: "empty" }
    | { branch: "single"; keepN: number; url: string }
    | { branch: "multi"; candidates: { n: number; url: string }[] };

/**
 * 组内已有图分支：收集所选行的全部已传图（行升序、行内按原顺序）。
 * - 零图 → empty（合并后传一张全组生效）；
 * - 恰一张唯一图（不同 URL 恰 1）→ single（沿用该行已传图统一到全组）；
 * - 多张不同图 → multi（确认窗列出候选，用户点名保留一张，其余行图清空走既有清空路径）。
 */
export function mergeImageBranch(rows: Pick<HubAssetCard, "n" | "已传图">[]): MergeImageBranch {
    const pairs = [...rows]
        .sort((a, b) => a.n - b.n)
        .flatMap((row) => row.已传图.map((url) => ({ n: row.n, url })));
    if (!pairs.length) return { branch: "empty" };
    const distinct = [...new Set(pairs.map((pair) => pair.url))];
    if (distinct.length === 1) return { branch: "single", keepN: pairs[0].n, url: distinct[0] };
    return { branch: "multi", candidates: pairs };
}

/**
 * 合并卡徽标文字（§3.1：「#1+#3+#5 已合并」）；name=组名（空 → 调用方兜主行短名）。
 * 恒非空（members 非空才可能有组），供探针断言「角标文字非空」。
 */
export function mergeBadge(members: number[]): string {
    const sorted = [...members].sort((a, b) => a - b);
    if (!sorted.length) return "";
    return `${sorted.map((n) => `#${n}`).join("+")} 已合并`;
}

/**
 * 组卡单帧单框（§3.1.2）：有主行自身框 → 恰一枚框（行号=主行）；无框/降级 → null（诚实降级，
 * 不搬他人框）。前端渲染以本函数为唯一取框口——探针断言「data-hub-box-row 恰 1 个且=主行」。
 */
export function groupBoxRow(group: HubCardGroup | null): { row: number } | null {
    if (!group || !group.单框 || group.单框降级) return null;
    return { row: group.主行 };
}

/** 镜次并集预览（确认窗「影响预览」）：成员镜次集合的并集（升序去重）。 */
export function mergeShotUnion(perRow: number[][]): number[] {
    return [...new Set(perRow.flat())].sort((a, b) => a - b);
}

/**
 * 提交前成员复核（§3.6：确认窗提交时复核组成员未变）：快照行号必须仍然全部存在、且仍然都不在任何
 * 确认组里。变了 → 返回差异文案（非空=报错，可重选）；没变 → ""。
 */
export function mergeMembershipChanged(
    snapshotRows: number[],
    currentCards: Pick<HubAssetCard, "n" | "同实体确认">[],
): string {
    const byNo = new Map(currentCards.map((card) => [card.n, card]));
    const gone = snapshotRows.filter((n) => !byNo.has(n));
    if (gone.length) return `行 ${gone.join("、")} 已不存在（清单已变化），请重新选择`;
    const grouped = snapshotRows.filter((n) => (byNo.get(n)?.同实体确认.length ?? 0) >= 2);
    if (grouped.length) return `行 ${grouped.join("、")} 已在别的组里（先拆开），请重新选择`;
    return "";
}
