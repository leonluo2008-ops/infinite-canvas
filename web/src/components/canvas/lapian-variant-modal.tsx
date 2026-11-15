import { useEffect, useMemo, useState } from "react";
import { Empty, Input, Modal, Select, Typography } from "antd";
import { GitFork } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * 源实例拉片节点「派生变体」弹窗（卡C4，方案 docs/方案-卡C一拆多用变体派生-20261115.md §三 D1）。
 * 范式照 LapianModelModal：extraTools 按钮打开、弹窗里选、确认才发起（**取消零请求**）。
 * 弹层两件事：变体意图文本（缺省继承源实例意图，留空即继承）+ 每个资产槽选替换图（从画布图片节点选，留空交人工）。
 * 确认后调与 agent 工具 derive_workflow_variant 同一个页面侧集成函数（workflow-variant-entry）。
 */
export type LapianVariantSlot = { slotNodeId: string; slotName: string };
export type LapianVariantImage = { id: string; title: string; previewUrl?: string };

export function LapianVariantModal({
    open,
    sourceTitle,
    slots,
    images,
    sourceIntentText,
    deriving,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    sourceTitle: string;
    slots: LapianVariantSlot[];
    images: LapianVariantImage[];
    sourceIntentText?: string;
    deriving: boolean;
    onCancel: () => void;
    onConfirm: (intentText: string, bindings: { slotName: string; imageNodeId: string }[]) => void;
}) {
    const { t } = useTranslation();
    const [draftIntent, setDraftIntent] = useState("");
    const [draftPicks, setDraftPicks] = useState<Record<string, string>>({});

    useEffect(() => {
        if (!open) return;
        setDraftIntent("");
        setDraftPicks({});
    }, [open]);

    const imageOptions = useMemo(
        () => images.map((image) => ({ value: image.id, label: image.title })),
        [images],
    );

    const picked = slots.filter((slot) => draftPicks[slot.slotName]).map((slot) => ({ slotName: slot.slotName, imageNodeId: draftPicks[slot.slotName] }));

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <GitFork className="size-4" />
                    {t("canvas.lapianVariant.modalTitle", { name: sourceTitle })}
                </span>
            }
            open={open}
            centered
            width={640}
            destroyOnHidden
            okText={t("canvas.lapianVariant.confirm")}
            cancelText={t("common.cancel")}
            okButtonProps={{ loading: deriving }}
            onCancel={onCancel}
            onOk={() => onConfirm(draftIntent.trim(), picked)}
            data-lapian-variant-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-3">
                {t("canvas.lapianVariant.modalDescription")}
            </Typography.Paragraph>
            <Typography.Paragraph type="secondary" className="!mb-3">
                {t("canvas.lapianVariant.inheritIntent", { intent: sourceIntentText || t("canvas.lapianVariant.noSourceIntent") })}
            </Typography.Paragraph>

            <div className="mb-4">
                <div className="mb-1 text-xs font-semibold text-stone-500">{t("canvas.lapianVariant.intentLabel")}</div>
                <Input.TextArea
                    value={draftIntent}
                    onChange={(event) => setDraftIntent(event.target.value)}
                    placeholder={t("canvas.lapianVariant.intentPlaceholder")}
                    rows={3}
                    data-lapian-variant-intent
                />
            </div>

            <div className="mb-2 text-xs font-semibold text-stone-500">{t("canvas.lapianVariant.slotsLabel")}</div>
            {slots.length === 0 ? (
                <Empty description={t("canvas.lapianVariant.noSlots")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
                <div className="flex flex-col gap-2" data-lapian-variant-slots>
                    {slots.map((slot) => (
                        <div key={slot.slotNodeId} className="flex items-center gap-2" data-lapian-variant-slot={slot.slotName}>
                            <span className="w-24 shrink-0 truncate text-sm">{slot.slotName}</span>
                            <Select
                                className="min-w-0 flex-1"
                                value={draftPicks[slot.slotName]}
                                onChange={(value) => setDraftPicks((current) => ({ ...current, [slot.slotName]: value }))}
                                options={imageOptions}
                                allowClear
                                placeholder={t("canvas.lapianVariant.slotPlaceholder")}
                                showSearch
                                optionFilterProp="label"
                            />
                        </div>
                    ))}
                </div>
            )}
            <Typography.Paragraph type="secondary" className="!mb-0 !mt-3 text-xs">
                {t("canvas.lapianVariant.stopNote")}
            </Typography.Paragraph>
        </Modal>
    );
}
