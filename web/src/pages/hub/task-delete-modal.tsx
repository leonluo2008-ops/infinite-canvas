import { App, Input, Modal, Tag } from "antd";
import { useState } from "react";

import { deleteHubTask, errorText, type HubTaskSummary } from "./hub-api";
import { deleteGuard } from "./hub-status";

/**
 * 删除二次确认（AT-9 §六删除合同的前端半边）：
 *  - 运行中四态：入口按钮已禁用，本弹窗不会打开（兜底同款提示）；
 *  - awaiting_assets / awaiting_approval（拆解过但 idle）：必须输入任务号或完整任务名才能调 delete（confirm_name）；
 *  - ingested/failed/done：普通二次确认。
 * 错误原文透出（桥端 409 带 error 字段，如「运行中删除拒绝」）。
 */
export function TaskDeleteModal({ task, open, onClose, onDeleted }: { task: HubTaskSummary | null; open: boolean; onClose: () => void; onDeleted: (localId: string) => void }) {
    const { message } = App.useApp();
    const [confirmText, setConfirmText] = useState("");
    const [busy, setBusy] = useState(false);

    if (!task) return null;
    const guard = deleteGuard(task);
    const fullName = task.task_name || task.local_id;
    const nameOk = !guard.needName || confirmText.trim() === task.local_id || confirmText.trim() === fullName;

    const close = () => {
        setConfirmText("");
        onClose();
    };

    const doDelete = async () => {
        if (busy) return;
        setBusy(true);
        try {
            await deleteHubTask(task.local_id, guard.needName ? confirmText.trim() : undefined);
            message.success(`任务 ${task.local_id} 已删除`);
            onDeleted(task.local_id);
            close();
        } catch (error) {
            message.error(`删除失败：${errorText(error)}`);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            title="删除任务"
            open={open}
            onCancel={close}
            okText={busy ? "删除中…" : "确认删除"}
            okButtonProps={{ danger: true, disabled: !nameOk }}
            onOk={() => void doDelete()}
            confirmLoading={busy}
            maskClosable={!busy}
            destroyOnHidden
        >
            <div className="space-y-3 pt-1 text-sm">
                <div>
                    将删除任务 <span className="font-mono font-semibold">{task.local_id}</span>
                    <span className="mx-1">·</span>
                    <span className="text-stone-500 dark:text-stone-400">{fullName}</span>
                    <Tag className="ml-2 m-0">{task.state}</Tag>
                </div>
                <div className="text-stone-500 dark:text-stone-400">
                    删除范围：任务目录 + 该任务专属的拆解产物 + 参考视频（无其他任务共用时）；已写墓碑防任务号复用，不可恢复。
                </div>
                {guard.needName ? (
                    <div className="space-y-1.5">
                        <div className="text-amber-600 dark:text-amber-400">该任务已拆解（{task.state === "awaiting_assets" ? "待置换" : "待审批"}），输入任务号 <span className="font-mono font-semibold">{task.local_id}</span> 或完整任务名确认：</div>
                        <Input value={confirmText} onChange={(event) => setConfirmText(event.target.value)} placeholder={task.local_id} disabled={busy} />
                    </div>
                ) : null}
            </div>
        </Modal>
    );
}
