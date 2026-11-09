import { Tag } from "antd";

import type { HubTaskSummary } from "./hub-api";

/**
 * 状态→展示合同（AT-9 §四操作合同的状态全集，含短暂中间态 ingesting）。
 * 颜色用 antd 预设，表格与详情共用同一映射，两处永不漂移。
 */
export const HUB_STATE_META: Record<string, { label: string; color: string }> = {
    ingested: { label: "已建任务", color: "default" },
    ingesting: { label: "入库中", color: "processing" },
    discussing: { label: "拆解中", color: "blue" },
    awaiting_assets: { label: "待置换", color: "gold" },
    rendering: { label: "渲染提示词中", color: "purple" },
    awaiting_approval: { label: "待审批", color: "cyan" },
    approved: { label: "已批准·待提交", color: "green" },
    generating: { label: "生成中", color: "geekblue" },
    done: { label: "已交付", color: "success" },
    failed: { label: "失败", color: "error" },
};

/** 运行中四态：只读+进度，删除一律拒绝（§六删除合同，桥端 409）。 */
export const HUB_RUNNING_STATES = ["ingesting", "discussing", "rendering", "generating"];

/** 拆解过但 idle 的删除需任务名确认（免确认=ingested/failed/done）。 */
export const HUB_DELETE_CONFIRM_STATES = ["awaiting_assets", "awaiting_approval"];

export function hubStateMeta(state: string | undefined) {
    return HUB_STATE_META[state || ""] || { label: state || "未知状态", color: "default" };
}

/** 状态彩色标签（表格「状态」列与详情页头部共用）。 */
export function HubStateTag({ state }: { state: string | undefined }) {
    const meta = hubStateMeta(state);
    return (
        <Tag color={meta.color} className="m-0">
            {meta.label}
        </Tag>
    );
}

/** 删除可用性（表格「操作」列 + 详情页头部共用同一判定）。 */
export function deleteGuard(task: Pick<HubTaskSummary, "state">): { disabled: boolean; reason: string; needName: boolean } {
    const state = task.state || "";
    if (HUB_RUNNING_STATES.includes(state)) {
        return { disabled: true, reason: "运行中不可删", needName: false };
    }
    return { disabled: false, reason: "", needName: HUB_DELETE_CONFIRM_STATES.includes(state) };
}
