/**
 * 实例身份记录的存取（P4；P0a §5.1/§5.3 落地：随项目持久化、随 v3 导出携带、只存身份不存状态）。
 *
 * 落点 = `CanvasProject.workflowInstances`（与 P2 `workflowTemplate` 同一种显式扩展字段），
 * 不进节点 metadata（C5：metadata 会被通用 ops 浅合并，无白名单）、不建第二份 JSON。
 */

import type { WorkflowInstanceIdentityRecord } from "@/lib/canvas/workflow-instance-run";

/** 导入侧校验：形如身份记录就透传（版本号留给后续迁移，不静默丢未知新版本）。 */
export function acceptWorkflowInstances(value: unknown): WorkflowInstanceIdentityRecord[] | undefined {
    if (!Array.isArray(value)) return undefined;
    const records = value.filter((item): item is WorkflowInstanceIdentityRecord => {
        if (!item || typeof item !== "object") return false;
        const candidate = item as Partial<WorkflowInstanceIdentityRecord>;
        return Boolean(candidate.instanceId) && Boolean(candidate.templateId) && Boolean(candidate.canvas);
    });
    return records.length ? records : undefined;
}

/** 同一实例重跑只保留最新一条（身份是「谁是谁」，不是历史日志）。 */
export function upsertInstanceIdentity(records: WorkflowInstanceIdentityRecord[] | undefined, record: WorkflowInstanceIdentityRecord): WorkflowInstanceIdentityRecord[] {
    const list = Array.isArray(records) ? records : [];
    const next = list.filter((item) => !(item.instanceId === record.instanceId && item.templateId === record.templateId));
    return [...next, record];
}

export function findInstanceIdentity(records: WorkflowInstanceIdentityRecord[] | undefined, instanceId: string): WorkflowInstanceIdentityRecord | undefined {
    return (records || []).find((item) => item.instanceId === instanceId);
}

/**
 * 身份记录的「可审批」前置（§4.1：「建到任务」≠「可审批」）。
 * 只回答「四件套是否齐」——不回答现在能不能批（那是 hub 状态的事，现查）。
 */
export function instanceIdentityGaps(record: WorkflowInstanceIdentityRecord | null | undefined): string[] {
    if (!record) return ["没有实例身份记录"];
    const gaps: string[] = [];
    if (!record.hub?.localId) gaps.push("未绑定 hub local_id（只造了画布侧）");
    if (!record.hub?.bridgeId) gaps.push("缺 bridge_id（hub 侧任务身份不闭合）");
    if (!record.hub?.revision.approvalSnapshot) gaps.push("没有审批快照（正文未按 hub 保存语义落库）");
    if (!record.canvas.outputNodeId) gaps.push("没有记录本次组装产物节点（G18 未闭合）");
    if (!record.canvas.lapianTaskId) gaps.push("缺拉片 taskId（同一实例的后端任务号不可查）");
    return gaps;
}
