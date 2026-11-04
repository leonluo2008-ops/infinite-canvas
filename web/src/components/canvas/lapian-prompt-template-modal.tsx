import { useEffect, useState } from "react";
import { Modal, Radio, Tag, Typography } from "antd";
import { LayoutTemplate } from "lucide-react";
import { useTranslation } from "react-i18next";

import { DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID, LAPIAN_PROMPT_TEMPLATES, getLapianPromptTemplate, type LapianPromptTemplateId } from "@/lib/canvas/lapian-prompt-templates";

/**
 * 转换指令模板切换器（T3-C）。
 *
 * 范式照 ImageToolSettingsModal：extraTools 按钮打开、弹窗里单选、确认才落盘。
 * 区别：这里只做一件事——把选中的指令正文写回指令节点，不重建节点、不动连线
 * （配置节点组装串不变，重新生成即得新骨架终稿）。
 */
export function LapianPromptTemplateModal({
    open,
    templateId,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    templateId?: string;
    onCancel: () => void;
    onConfirm: (templateId: LapianPromptTemplateId) => void;
}) {
    const { t } = useTranslation();
    const [draft, setDraft] = useState<LapianPromptTemplateId>(getLapianPromptTemplate(templateId).id);

    useEffect(() => {
        if (open) setDraft(getLapianPromptTemplate(templateId).id);
    }, [open, templateId]);

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <LayoutTemplate className="size-4" />
                    {t("canvas.lapianPrompt.templateModalTitle")}
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
            data-lapian-template-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-4">
                {t("canvas.lapianPrompt.templateModalDescription")}
            </Typography.Paragraph>

            <Radio.Group value={draft} onChange={(event) => setDraft(event.target.value as LapianPromptTemplateId)} className="flex w-full flex-col gap-2">
                {LAPIAN_PROMPT_TEMPLATES.map((template) => (
                    <div
                        key={template.id}
                        data-lapian-template-option={template.id}
                        className={`rounded-lg border px-3 py-2 transition ${draft === template.id ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                    >
                        <div className="flex items-center gap-2">
                            <Radio value={template.id}>{t(template.labelKey)}</Radio>
                            {template.id === DEFAULT_LAPIAN_PROMPT_TEMPLATE_ID ? <Tag className="m-0 text-[10px]">{t("canvas.lapianPrompt.templateDefault")}</Tag> : null}
                        </div>
                        <div className="pl-6 text-xs leading-5 text-stone-500 dark:text-stone-400">{template.instruction.split("\n").find((line) => line.startsWith("# 结构"))?.replace("# 结构", "").replace(/^[（(]/, "").replace(/[）)]$/, "") || ""}</div>
                    </div>
                ))}
            </Radio.Group>
        </Modal>
    );
}
