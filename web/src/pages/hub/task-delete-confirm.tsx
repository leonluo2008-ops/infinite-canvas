import { App, Button, Popconfirm, Tooltip } from "antd";
import { useState } from "react";

import { deleteHubTask, errorText, type HubTaskSummary } from "./hub-api";
import { deleteGuard, HubStateTag } from "./hub-status";
import { useHubMessage } from "./use-hub-message";

/**
 * 删除确认（AT-16 B6 / 缺陷 D7：删除任务不再手输任务名）。
 *
 * 交互合同 = 红线 25 的对位件 `components/canvas/canvas-side-panel.tsx:522`「移除画布资产」：
 * Popconfirm 二键确认（确认键危险红）+ 取消**零请求**。原先的输名 Modal 已整体删除，
 * `confirm_name` 不再由用户键入，而是由本组件按后端合同**自动带当前任务号**。
 *
 * 后端合同一字未改（`ui/hub_api.py` delete_task）：awaiting_assets / awaiting_approval 要求
 * confirm_name ∈ {任务号, 完整任务名}，运行中四态 + 排队 job 一律 409。删掉输入框不等于放宽后端，
 * 只是把「确认」从打字降级成按一次危险红钮。
 *
 * **列表行内与详情头共用本组件**（同承接、同守卫、同错误透出）：不许一处 Popconfirm、
 * 另一处还留输名弹窗。运行态守卫仍在 `hub-status.deleteGuard`（禁用 + tooltip），桥端 409 是第二道。
 */
export function TaskDeleteButton({
    task,
    label = "删除",
    size,
    disabled = false,
    stopPropagation = false,
    onDeleted,
    onError,
}: {
    task: HubTaskSummary;
    label?: string;
    size?: "small" | "middle" | "large";
    /** 额外禁用（如抽屉里正有写操作在跑）。 */
    disabled?: boolean;
    /** 行内/抽屉容器点击会开详情——默认拦掉，别让点删除顺带把抽屉也打开。 */
    stopPropagation?: boolean;
    onDeleted: (localId: string) => void;
    /**
     * AT-18 §4 错误处理：桥端拒绝（如「任务运行中（discussing），删除拒绝」）除了 toast，
     * 再回调一次，让**调用方在行内**留下可读文案（行内文案比一闪而过的 toast 活得久）。
     * 可选：不传就还是只有 toast（既有调用点行为零变化）。
     */
    onError?: (message: string) => void;
}) {
    const message = useHubMessage();
    const [busy, setBusy] = useState(false);
    const guard = deleteGuard(task);
    const blocked = guard.disabled || disabled || busy;

    const doDelete = async () => {
        if (busy) return;
        setBusy(true);
        try {
            // 二键确认已成立 → 自动带任务号满足桥端 confirm_name 合同（用户零键入）
            await deleteHubTask(task.local_id, deleteConfirmName(task));
            message.success(`任务 ${task.local_id} 已删除`);
            onDeleted(task.local_id);
        } catch (error) {
            // 桥端原文透出（如「任务运行中（generating），删除拒绝」）——不吞、不美化
            const text = errorText(error);
            message.error(`删除失败：${text}`);
            onError?.(text);
        } finally {
            setBusy(false);
        }
    };

    const trigger = (
        <Button
            size={size}
            danger
            ghost
            loading={busy}
            disabled={blocked}
            data-hub-delete-trigger={task.local_id}
            data-hub-delete-need-name={guard.needName ? "1" : "0"}
            onClick={stopPropagation ? (event) => event.stopPropagation() : undefined}
        >
            {label}
        </Button>
    );

    const confirm = (
        <Popconfirm
            title={`删除任务 ${task.local_id}？`}
            description={
                <div className="max-w-[320px] space-y-1.5 text-xs" data-hub-delete-confirm={task.local_id}>
                    <div className="break-words">
                        <span className="text-stone-500 dark:text-stone-400">{task.task_name || task.local_id}</span>
                        {/* 状态标签走 hub-status 唯一映射（与列表/详情同源，不在这儿另写一份枚举） */}
                        <span className="ml-2 inline-flex">
                            <HubStateTag state={task.state} submit={task.submit} />
                        </span>
                    </div>
                    <div className="text-stone-500 dark:text-stone-400">
                        删除范围：任务目录 + 该任务专属的拆解产物 + 参考视频（无其他任务共用时）。
                    </div>
                    {guard.needName ? (
                        <div className="text-amber-600 dark:text-amber-400">该任务已拆解，删除会连同拆解产物一起清掉。</div>
                    ) : null}
                    <div className="text-stone-500 dark:text-stone-400">已写墓碑防任务号复用，不可恢复。</div>
                </div>
            }
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true, "data-hub-delete-ok": task.local_id }}
            cancelButtonProps={{ "data-hub-delete-cancel": task.local_id }}
            // 返回 Promise → 确认键进 loading，删完才收起；取消只关弹层，零请求
            onConfirm={() => doDelete()}
            disabled={blocked}
        >
            {trigger}
        </Popconfirm>
    );

    return guard.disabled ? (
        <Tooltip title={guard.reason || "运行中不可删"}>
            <span className="inline-flex">{confirm}</span>
        </Tooltip>
    ) : (
        confirm
    );
}

/**
 * 二键确认后交给桥端的 confirm_name（唯一出口）。
 *
 * 恒带本任务号：awaiting_assets / awaiting_approval 的「确认」已经由用户亲手按下危险红钮体现，
 * 不再要求把任务号打一遍。桥端校验（`delete_task`）保持原样，仍是防误删的第二道门。
 */
export function deleteConfirmName(task: Pick<HubTaskSummary, "local_id">): string {
    return task.local_id;
}
