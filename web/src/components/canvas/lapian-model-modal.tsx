import { useEffect, useState } from "react";
import { Modal, Radio, Typography } from "antd";
import { Cpu } from "lucide-react";
import { useTranslation } from "react-i18next";

import { decodeChannelModelRef, type LapianModelGroup } from "@/lib/canvas/lapian-model";

/**
 * 拉片节点「模型」选择弹窗（M6-T7-R ③）。
 * 范式照 LapianOptionalModal：extraTools 按钮打开、弹窗里选、确认才落盘。
 * 只做一件事——把选中的拉片模型引用（`channelId::model`）写回节点 metadata.lapianModel；
 * 空 = 跟随全局默认（仍未设默认 = 走 bridge 默认模型）。
 * 数据源 = 全局拉片模型清单（设置 → 拉片模型），按渠道分组；不是 capability 过滤（T7-R 推翻项）。
 */
export const LAPIAN_MODEL_FOLLOW_GLOBAL = "";

export function LapianModelModal({
    open,
    value,
    globalDefault,
    groups,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    value?: string;
    globalDefault?: string;
    groups: LapianModelGroup[];
    onCancel: () => void;
    onConfirm: (ref: string) => void;
}) {
    const { t } = useTranslation();
    const [draft, setDraft] = useState<string>(() => (value || "").trim());

    useEffect(() => {
        if (open) setDraft((value || "").trim());
    }, [open, value]);

    const followLabel = globalDefault
        ? `${t("canvas.lapianModel.followGlobal")}${t("canvas.lapianModel.globalDefaultSuffix", { name: decodeChannelModelRef(globalDefault)?.model || globalDefault })}`
        : t("canvas.lapianModel.followGlobal");

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <Cpu className="size-4" />
                    {t("canvas.lapianModel.modalTitle")}
                </span>
            }
            open={open}
            centered
            width={560}
            onCancel={onCancel}
            destroyOnHidden
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onOk={() => onConfirm(draft)}
            data-lapian-model-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-4">
                {t("canvas.lapianModel.modalDescription")}
            </Typography.Paragraph>

            <Radio.Group value={draft} onChange={(event) => setDraft(event.target.value)} className="flex w-full flex-col gap-2">
                <label
                    data-lapian-model-option="follow-global"
                    className={`cursor-pointer rounded-lg border px-3 py-2 transition ${draft === LAPIAN_MODEL_FOLLOW_GLOBAL ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                >
                    <Radio value={LAPIAN_MODEL_FOLLOW_GLOBAL}>{followLabel}</Radio>
                </label>
                {groups.map((group) => (
                    <div key={group.channelId} className="rounded-lg border border-stone-200 px-3 py-2 dark:border-stone-700" data-lapian-model-group={group.channelId}>
                        <div className="mb-1 text-xs font-semibold text-stone-500">{group.channelName}</div>
                        <div className="flex flex-col gap-1.5">
                            {group.models.map((model) => (
                                <label
                                    key={model.value}
                                    data-lapian-model-option={model.value}
                                    className={`cursor-pointer rounded-md px-2 py-1 transition ${draft === model.value ? "bg-blue-50/60 dark:bg-blue-500/10" : ""}`}
                                >
                                    <Radio value={model.value}>{model.name}</Radio>
                                </label>
                            ))}
                        </div>
                    </div>
                ))}
            </Radio.Group>
        </Modal>
    );
}
