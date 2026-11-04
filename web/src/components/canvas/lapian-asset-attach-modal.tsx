import { useEffect, useMemo, useState } from "react";
import { Empty, Input, Modal, Radio, Typography } from "antd";
import { ImagePlus } from "lucide-react";
import { useTranslation } from "react-i18next";

export type LapianAssetAttachImage = {
    id: string;
    title: string;
    previewUrl?: string;
};

/**
 * 资产位挂图（T3-H2，决策 4）。
 *
 * 资产位是 Text 节点，入端连线能力未证实，所以挂图不走连线：选中画布上的一张图，
 * 存进资产位 metadata.attachedImageId + attachedImageDesc，生成提示词时按资产位序收进清单。
 * 确认空选择 = 摘图（沿用原片描述）。
 */
export function LapianAssetAttachModal({
    open,
    slotTitle,
    images,
    currentImageId,
    currentCaption,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    slotTitle: string;
    images: LapianAssetAttachImage[];
    currentImageId?: string;
    currentCaption?: string;
    onCancel: () => void;
    onConfirm: (imageId: string, caption: string) => void;
}) {
    const { t } = useTranslation();
    const initialImageId = useMemo(() => (images.some((image) => image.id === currentImageId) ? String(currentImageId) : ""), [currentImageId, images]);
    const [draftImageId, setDraftImageId] = useState(initialImageId);
    const [draftCaption, setDraftCaption] = useState(currentCaption || "");

    useEffect(() => {
        if (!open) return;
        setDraftImageId(initialImageId);
        setDraftCaption(currentCaption || "");
    }, [open, initialImageId, currentCaption]);

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <ImagePlus className="size-4" />
                    {t("canvas.lapianAsset.attachModalTitle", { name: slotTitle })}
                </span>
            }
            open={open}
            centered
            width={640}
            destroyOnHidden
            okText={t("canvas.lapianAsset.attachApply")}
            cancelText={t("common.cancel")}
            onCancel={onCancel}
            onOk={() => onConfirm(draftImageId, draftCaption.trim())}
            data-lapian-asset-attach-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-3">
                {t("canvas.lapianAsset.attachDescription")}
            </Typography.Paragraph>

            <div className="mb-3">
                <div className="mb-1.5 text-xs text-stone-500 dark:text-stone-400">{t("canvas.lapianAsset.captionLabel")}</div>
                <Input
                    value={draftCaption}
                    maxLength={6}
                    showCount
                    allowClear
                    placeholder={t("canvas.lapianAsset.captionPlaceholder")}
                    onChange={(event) => setDraftCaption(event.target.value)}
                    data-lapian-asset-caption
                />
                <div className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">{t("canvas.lapianAsset.captionHint")}</div>
            </div>

            {images.length ? (
                <Radio.Group value={draftImageId} onChange={(event) => setDraftImageId(event.target.value as string)} className="flex max-h-[320px] w-full flex-col gap-2 overflow-auto pr-1">
                    <div
                        data-lapian-asset-attach-option=""
                        className={`rounded-lg border px-3 py-2 text-sm transition ${draftImageId === "" ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                    >
                        <Radio value="">{t("canvas.lapianAsset.attachNone")}</Radio>
                    </div>
                    {images.map((image) => (
                        <div
                            key={image.id}
                            data-lapian-asset-attach-option={image.id}
                            className={`rounded-lg border px-3 py-2 transition ${draftImageId === image.id ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                        >
                            <div className="flex items-center gap-3">
                                {image.previewUrl ? (
                                    <img src={image.previewUrl} alt={image.title} className="size-12 rounded border border-stone-200 object-cover dark:border-stone-700" />
                                ) : (
                                    <span className="grid size-12 place-items-center rounded border border-stone-200 text-[10px] text-stone-400 dark:border-stone-700">{t("canvas.lapianAsset.noPreview")}</span>
                                )}
                                <Radio value={image.id} className="min-w-0 flex-1">
                                    <span className="line-clamp-2 break-all text-[13px]">{image.title || image.id}</span>
                                </Radio>
                            </div>
                        </div>
                    ))}
                </Radio.Group>
            ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("canvas.lapianAsset.attachEmpty")} className="py-8" />
            )}
        </Modal>
    );
}
