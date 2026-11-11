import { Button, Popover, Tag } from "antd";

import type { HubSubmitForm, HubSubmitView, HubTaskSummary } from "./hub-api";

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

/**
 * 红线 25 对位（AT-16 B5a-1）：画布侧的**交互合同**是下面这套，hub 只引合同不搬组件——
 *
 *   · canvas-node-hover-toolbar.tsx:122-150  带 taskId 的「重查」与「重试」分流
 *     （有 id 只能查、明确失败才重试）
 *   · pages/canvas/project.tsx:3651-3656    先续查可恢复的视频，再决定要不要新建
 *   · components/canvas/canvas-node.tsx:528-545  常驻失败正文 + 重试（不是一次性 toast）
 *
 * 为什么不能直接把 canvas-node.tsx / 它的 onRetry / theme 搬来承载表格行：那套组件是
 * ReactFlow 节点渲染件，props 绑死 node（含 id/位置/selected）、theme 与画布事件通道；
 * hub 表格行没有画布节点实例，也没有 flow 交互上下文，硬塞只会造出第二个「节点壳」。
 * 所以这里做的是**同一交互合同**的两件展示件：状态标签按四类形态说实话（HubStateTag），
 * 结论/可能原因/原文常驻可展开（SubmitResultLine）；动作面（行内重提/续查入口）留给 B5a-2，
 * 且一律受后端 blocks_retry 闸门约束（禁盲目重提）。
 */

/**
 * 提交结果四类形态 → 标签配色（AT-16 B5a-1）。
 *
 * **文案不在这里**：结论/短标签由后端 `automation/submit_view.py` 一处生产（同源），
 * 这里只给配色。标签优先级：有四类形态 → 用形态（否则「已批准·待提交」这类按 state
 * 的口径会在提交已经失败后继续说谎）；没有 → 回落 HUB_STATE_META。
 */
export const HUB_SUBMIT_META: Record<Exclude<HubSubmitForm, "none">, { label: string; color: string }> = {
    unknown: { label: "结果未知·先核对", color: "warning" },
    rejected: { label: "已拒绝·可改后重提", color: "orange" },
    inflight: { label: "云端处理中·续查", color: "processing" },
    failed: { label: "失败·可重试", color: "error" },
};

/**
 * 状态展示口径（唯一入口）：有提交形态就用形态（标签文案取后端人话 label），
 * 否则按 state 映射。表格「状态」列、详情头、抽屉顶部三处都走本函数。
 */
export function hubStateMeta(state: string | undefined, submit?: HubSubmitView | null) {
    const form = submit?.form;
    if (form && form !== "none") {
        const meta = HUB_SUBMIT_META[form];
        return { label: submit?.label || meta.label, color: meta.color };
    }
    return HUB_STATE_META[state || ""] || { label: state || "未知状态", color: "default" };
}

/** 状态彩色标签（表格「状态」列、详情头、抽屉顶部共用同一映射）。 */
export function HubStateTag({ state, submit }: { state: string | undefined; submit?: HubSubmitView | null }) {
    const meta = hubStateMeta(state, submit);
    return (
        <Tag color={meta.color} className="m-0">
            {meta.label}
        </Tag>
    );
}

/**
 * 提交结果一句话（主表行内 + 详情头共用**同一个组件**，红线 25：不各写一套）。
 *
 * 显示面：人话结论（后端生产）+ 短错误码 + 「详情」弹层（可能原因并列 / 处理指引 /
 * 可展开的脱敏原文）。列表靠摘要字段就能显示，5 秒轮询整表替换也不会把错误抹掉。
 */
export function SubmitResultLine({ submit }: { submit?: HubSubmitView | null }) {
    if (!submit || submit.form === "none") return null;
    const more = (
        <div className="max-w-[420px] space-y-2 text-xs" data-hub-submit-detail={submit.form}>
            {submit.causes.length ? (
                <div>
                    <div className="font-medium text-stone-700 dark:text-stone-200">可能原因（并列，不锁定唯一原因）</div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-stone-600 dark:text-stone-300">
                        {submit.causes.map((cause) => (
                            <li key={cause} className="break-words">
                                {cause}
                            </li>
                        ))}
                    </ul>
                </div>
            ) : null}
            {submit.guidance.length ? (
                <div>
                    <div className="font-medium text-stone-700 dark:text-stone-200">怎么处理</div>
                    <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-stone-600 dark:text-stone-300">
                        {submit.guidance.map((step) => (
                            <li key={step} className="break-words">
                                {step}
                            </li>
                        ))}
                    </ol>
                </div>
            ) : null}
            {submit.raw ? (
                <div>
                    <div className="font-medium text-stone-700 dark:text-stone-200">原文（已脱敏）</div>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-stone-100 p-2 font-mono text-[11px] text-stone-600 dark:bg-stone-900 dark:text-stone-300">
                        {submit.raw}
                    </pre>
                </div>
            ) : null}
        </div>
    );
    return (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" data-hub-submit-line={submit.form}>
            <span className="min-w-0 truncate text-stone-600 dark:text-stone-300" data-hub-submit-conclusion>
                {submit.conclusion}
            </span>
            {submit.error_code ? (
                <Tag className="m-0 font-mono text-[11px]" data-hub-submit-code>
                    {submit.error_code}
                </Tag>
            ) : null}
            <Popover trigger="click" placement="bottomLeft" title="提交结果：可能原因 / 处理 / 原文" content={more}>
                <Button type="link" size="small" className="h-auto px-0 text-xs" data-hub-submit-more>
                    详情
                </Button>
            </Popover>
        </div>
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
