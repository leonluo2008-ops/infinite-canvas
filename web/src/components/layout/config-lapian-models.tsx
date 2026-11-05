import { Button, Checkbox, Tag } from "antd";
import { ListChecks } from "lucide-react";
import { useTranslation } from "react-i18next";

import { decodeChannelModelRef } from "@/lib/canvas/lapian-model";
import { encodeChannelModel, useConfigStore } from "@/stores/use-config-store";

/**
 * 设置面板「拉片模型」组（M6-T7-R ②）：与渠道并列的独立 tab，范式照 ConfigPromptSources。
 * 职责：从所有渠道的所有模型里勾选哪些可作为拉片模型（独立清单，不动渠道归属），并可设其中一个为全局默认。
 * 存储 = useConfigStore（AiConfig.lapianModels / lapianDefaultModel，persist merge 归一失效引用）。
 */
export function ConfigLapianModels() {
    const { t } = useTranslation();
    const config = useConfigStore((state) => state.config);
    const updateConfig = useConfigStore((state) => state.updateConfig);

    const checked = config.lapianModels;
    const defaultRef = config.lapianDefaultModel;
    const defaultName = defaultRef ? decodeChannelModelRef(defaultRef)?.model || defaultRef : "";

    const toggleModel = (ref: string, next: boolean) => {
        updateConfig("lapianModels", next ? [...checked, ref] : checked.filter((item) => item !== ref));
        // 取消勾选的若是全局默认，默认一并撤掉（persist merge 归一也会兜底，这里显式同步）。
        if (!next && defaultRef === ref) updateConfig("lapianDefaultModel", "");
    };

    const setDefault = (ref: string) => {
        // 再点一次取消默认（回到「未设默认」）。
        updateConfig("lapianDefaultModel", defaultRef === ref ? "" : ref);
    };

    const hasAnyModel = config.channels.some((channel) => channel.models.length > 0);

    return (
        <div data-config-lapian-models>
            <div className="mb-1 flex items-center gap-2 text-sm font-semibold">
                <ListChecks className="size-4" />
                {t("config.lapianModels.title")}
            </div>
            <div className="mb-3 text-xs leading-relaxed text-stone-500">{t("config.lapianModels.description")}</div>
            <div className="mb-4 text-xs text-stone-500">
                {defaultRef ? <Tag color="processing" className="m-0">{t("config.lapianModels.defaultSet", { name: defaultName })}</Tag> : <span>{t("config.lapianModels.defaultUnset")}</span>}
            </div>

            {!hasAnyModel ? <div className="rounded-lg border border-stone-200 px-4 py-3 text-xs text-stone-500 dark:border-stone-800">{t("config.lapianModels.empty")}</div> : (
                <div className="space-y-2">
                    {config.channels.map((channel) => (
                        <div key={channel.id} className="rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800" data-lapian-model-channel={channel.id}>
                            <div className="mb-2 flex items-center justify-between gap-3">
                                <span className="truncate text-sm font-semibold">{channel.name}</span>
                                <span className="shrink-0 text-xs text-stone-500">{t("config.lapianModels.modelCount", { count: channel.models.length })}</span>
                            </div>
                            <div className="flex flex-col gap-1.5">
                                {channel.models.map((model) => {
                                    const ref = encodeChannelModel(channel.id, model.name);
                                    const isChecked = checked.includes(ref);
                                    const isDefault = defaultRef === ref;
                                    return (
                                        <div key={ref} className="flex items-center gap-3" data-lapian-model-option={ref}>
                                            <Checkbox checked={isChecked} onChange={(event) => toggleModel(ref, event.target.checked)}>
                                                <span className="text-sm">{model.name}</span>
                                            </Checkbox>
                                            {isDefault ? <Tag color="processing" className="m-0">{t("config.lapianModels.isDefault")}</Tag> : null}
                                            {isChecked && !isDefault ? (
                                                <Button size="small" className="ml-auto" onClick={() => setDefault(ref)}>
                                                    {t("config.lapianModels.setDefault")}
                                                </Button>
                                            ) : null}
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
