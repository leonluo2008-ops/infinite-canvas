import { useEffect, useState } from "react";
import { Checkbox, Modal, Typography } from "antd";
import { ListChecks } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * 拉片可选段勾选（T6-B5 画布侧）。
 *
 * 范式照 LapianPromptTemplateModal：extraTools 按钮打开、弹窗里勾选、确认才落盘。
 * 只做一件事——把勾选的可选段字母（如 "A,C"）写回拉片节点 metadata.lapianOptional；
 * 渲染请求（project.tsx lapianPromptPreset）读它拼 `?optional=` 透传给 bridge，
 * 勾了的段才进提示词正文，全不勾 = 现状行为（可选段不出现）。
 */

export const LAPIAN_OPTIONAL_SEGMENTS = [
    { value: "A", labelKey: "canvas.lapianOptional.segmentA", descKey: "canvas.lapianOptional.segmentADesc" },
    { value: "B", labelKey: "canvas.lapianOptional.segmentB", descKey: "canvas.lapianOptional.segmentBDesc" },
    { value: "C", labelKey: "canvas.lapianOptional.segmentC", descKey: "canvas.lapianOptional.segmentCDesc" },
] as const;

export const LAPIAN_OPTIONAL_VALUES = LAPIAN_OPTIONAL_SEGMENTS.map((segment) => segment.value);

/** metadata 里存的是逗号分隔字母（"A,C"）；解析时容错空白/大小写，不认识的字母丢弃。 */
export function parseLapianOptional(value?: string): string[] {
    const picked = String(value || "")
        .split(",")
        .map((item) => item.trim().toUpperCase())
        .filter((item) => LAPIAN_OPTIONAL_VALUES.includes(item as (typeof LAPIAN_OPTIONAL_VALUES)[number]));
    return LAPIAN_OPTIONAL_VALUES.filter((value) => picked.includes(value));
}

export function LapianOptionalModal({
    open,
    value,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    value?: string;
    onCancel: () => void;
    onConfirm: (segments: string[]) => void;
}) {
    const { t } = useTranslation();
    const [draft, setDraft] = useState<string[]>(() => parseLapianOptional(value));

    useEffect(() => {
        if (open) setDraft(parseLapianOptional(value));
    }, [open, value]);

    const toggle = (checked: boolean, segment: string) =>
        setDraft((prev) => (checked ? LAPIAN_OPTIONAL_VALUES.filter((item) => prev.includes(item) || item === segment) : prev.filter((item) => item !== segment)));

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <ListChecks className="size-4" />
                    {t("canvas.lapianOptional.modalTitle")}
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
            data-lapian-optional-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-4">
                {t("canvas.lapianOptional.modalDescription")}
            </Typography.Paragraph>

            <div className="flex w-full flex-col gap-2">
                {LAPIAN_OPTIONAL_SEGMENTS.map((segment) => (
                    <label
                        key={segment.value}
                        data-lapian-optional-option={segment.value}
                        className={`cursor-pointer rounded-lg border px-3 py-2 transition ${draft.includes(segment.value) ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                    >
                        <div className="flex items-center gap-2">
                            <Checkbox checked={draft.includes(segment.value)} onChange={(event) => toggle(event.target.checked, segment.value)}>
                                {t(segment.labelKey)}
                            </Checkbox>
                        </div>
                        <div className="pl-6 text-xs leading-5 text-stone-500 dark:text-stone-400">{t(segment.descKey)}</div>
                    </label>
                ))}
            </div>
        </Modal>
    );
}
