import { useEffect, useMemo, useState } from "react";
import { Empty, Modal, Radio, Tag, Typography } from "antd";
import { FolderPlus } from "lucide-react";
import { useTranslation } from "react-i18next";

export type LapianAssetGroupResource = {
    id: string;
    title: string;
    kind: string;
    previewUrl?: string;
};

export type LapianAssetGroupOption = {
    id: string;
    title: string;
    /** 该组已经连到这个配置节点（组装串里已有 token），不重复挂。 */
    mounted: boolean;
    resources: LapianAssetGroupResource[];
};

/**
 * 资产组一键挂载（T3-D）。
 *
 * 组的事实载体是画布 Group 节点（不改 use-asset-store：资产库本来无组概念）。
 * 确认后由 caller 双写：setConnections(group→config) + composerContent 追加 @[node:groupId]。
 * 只写连线不够——composer 只发送 token 选中的资源，缺 token 图片不会进请求（路A-A1）。
 */
export function LapianAssetGroupModal({
    open,
    groups,
    onCancel,
    onConfirm,
}: {
    open: boolean;
    groups: LapianAssetGroupOption[];
    onCancel: () => void;
    onConfirm: (groupId: string) => void;
}) {
    const { t } = useTranslation();
    const firstAvailable = useMemo(() => groups.find((group) => !group.mounted)?.id ?? "", [groups]);
    const [draft, setDraft] = useState(firstAvailable);

    useEffect(() => {
        if (open) setDraft(firstAvailable);
    }, [open, firstAvailable]);

    const selected = groups.find((group) => group.id === draft);

    return (
        <Modal
            title={
                <span className="inline-flex items-center gap-2">
                    <FolderPlus className="size-4" />
                    {t("canvas.lapianPrompt.mountGroupModalTitle")}
                </span>
            }
            open={open}
            centered
            width={620}
            onCancel={onCancel}
            destroyOnHidden
            okText={t("canvas.lapianPrompt.mountGroupApply")}
            cancelText={t("common.cancel")}
            okButtonProps={{ disabled: !selected || selected.mounted }}
            onOk={() => {
                if (selected && !selected.mounted) onConfirm(selected.id);
            }}
            data-lapian-asset-group-modal
        >
            <Typography.Paragraph type="secondary" className="!mb-4">
                {t("canvas.lapianPrompt.mountGroupDescription")}
            </Typography.Paragraph>

            {groups.length ? (
                <Radio.Group value={draft} onChange={(event) => setDraft(event.target.value as string)} className="flex w-full flex-col gap-2">
                    {groups.map((group) => (
                        <div
                            key={group.id}
                            data-lapian-asset-group-option={group.id}
                            className={`rounded-lg border px-3 py-2.5 transition ${group.mounted ? "opacity-55" : draft === group.id ? "border-blue-400 bg-blue-50/60 dark:bg-blue-500/10" : "border-stone-200 dark:border-stone-700"}`}
                        >
                            <div className="flex items-center gap-2">
                                <Radio value={group.id} disabled={group.mounted}>
                                    <span className="font-medium">{group.title}</span>
                                </Radio>
                                <Tag className="m-0 text-[10px]">{t("canvas.lapianPrompt.resourceCount", { count: group.resources.length })}</Tag>
                                {group.mounted ? <Tag className="m-0 text-[10px]" color="blue">{t("canvas.lapianPrompt.mountGroupMounted")}</Tag> : null}
                            </div>
                            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 pl-6">
                                {group.resources.slice(0, 8).map((resource) =>
                                    resource.previewUrl ? (
                                        <img key={resource.id} src={resource.previewUrl} alt={resource.title} className="size-10 rounded border border-stone-200 object-cover dark:border-stone-700" />
                                    ) : (
                                        <span key={resource.id} className="inline-flex h-10 max-w-[120px] items-center truncate rounded border border-stone-200 px-2 text-[11px] text-stone-500 dark:border-stone-700 dark:text-stone-400">
                                            {resource.title || resource.kind}
                                        </span>
                                    ),
                                )}
                                {group.resources.length > 8 ? <span className="text-[11px] text-stone-500">+{group.resources.length - 8}</span> : null}
                            </div>
                        </div>
                    ))}
                </Radio.Group>
            ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("canvas.lapianPrompt.mountGroupEmpty")} className="py-10" />
            )}
        </Modal>
    );
}
