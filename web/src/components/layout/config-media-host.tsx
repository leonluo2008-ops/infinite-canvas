/**
 * S3.12 B3：媒体出口（图床首选源）设置面板。
 *
 * 交互定死（任务书 B3 条目 ④⑤）：
 *  * 单选 onChange 即 PUT（无独立保存按钮）；成功以服务端回读值刷新本地选中项；
 *    失败回滚到上一次服务端值，并在区块顶部落一行红字（Alert error，showIcon，error 原文）。
 *    该红字的唯一写入者与清除者都是本组件：下一次保存成功或组件卸载时清空。
 *  * 每个源行各带一个「测试」按钮，POST {id: 该源}；结果行紧贴该源行显示，不吞不改写。
 *  * 未配置源（configured=false）的单选项 disabled，徽标显示「需设置环境变量」。
 */

import { Alert, Button, Radio, Spin, Tag } from "antd";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchMediaHost, saveMediaHost, testMediaHost, type MediaHostSource, type MediaHostState, type MediaHostTestResult } from "@/services/api/media-host";

/** 每个源行本地的测试状态（互不共享；请求中该行按钮 disabled 防连点）。 */
type SourceTestState = { testing: boolean; result: MediaHostTestResult | null; error: string };

function emptyTestState(): SourceTestState {
    return { testing: false, result: null, error: "" };
}

export function ConfigMediaHost({ active }: { active: boolean }) {
    const { t } = useTranslation();
    const [data, setData] = useState<MediaHostState | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadError, setLoadError] = useState("");
    /** 保存失败红字：唯一写入者与清除者都是本组件（下一次保存成功或卸载时清空）。 */
    const [saveError, setSaveError] = useState("");
    /** 上一次服务端确认过的 selected：保存失败时回滚到这里。 */
    const [serverSelected, setServerSelected] = useState<string | null>(null);
    /** 界面当前选中的单选项（保存成功后才与 serverSelected 合流）。 */
    const [selected, setSelected] = useState<string | null>(null);
    const [savingId, setSavingId] = useState<string | null>(null);
    const [testStates, setTestStates] = useState<Record<string, SourceTestState>>({});
    /** 组件卸载标志：防止卸载后 setState（红字在卸载路径里自然消失 = 「卸载时清空」）。 */
    const unmountedRef = useRef(false);

    useEffect(
        () => () => {
            unmountedRef.current = true;
        },
        [],
    );

    useEffect(() => {
        if (!active || data || loading) return;
        void (async () => {
            setLoading(true);
            setLoadError("");
            try {
                const state = await fetchMediaHost();
                if (unmountedRef.current) return;
                setData(state);
                setServerSelected(state.selected);
                setSelected(state.selected);
            } catch (reason) {
                if (!unmountedRef.current) setLoadError(reason instanceof Error ? reason.message : String(reason));
            } finally {
                if (!unmountedRef.current) setLoading(false);
            }
        })();
    }, [active, data, loading]);

    const handleSelect = (next: string | null) => {
        if (next === selected) return;
        setSelected(next);
        setSavingId(next ?? "__default__");
        void (async () => {
            try {
                const saved = await saveMediaHost(next);
                // 成功：以服务端回读值刷新本地选中项，并清掉上一次的红字（红字唯一清除点之一）
                setServerSelected(saved.selected);
                setSelected(saved.selected);
                setSaveError("");
            } catch (reason) {
                // 失败：回滚到上一次服务端值 + 顶部落红字（error 原文）
                setServerSelected((previous) => {
                    setSelected(previous);
                    return previous;
                });
                setSaveError(`${t("config.mediaHost.saveFailed")} ${reason instanceof Error ? reason.message : String(reason)}`);
            } finally {
                setSavingId(null);
            }
        })();
    };

    const handleTest = (source: MediaHostSource) => {
        setTestStates((previous) => ({ ...previous, [source.id]: { ...emptyTestState(), testing: true } }));
        void (async () => {
            try {
                const result = await testMediaHost(source.id);
                setTestStates((previous) => ({ ...previous, [source.id]: { testing: false, result, error: "" } }));
            } catch (reason) {
                // 含 429 冷却文案原文；走该行内文案显示，不弹全局 message
                setTestStates((previous) => ({ ...previous, [source.id]: { testing: false, result: null, error: reason instanceof Error ? reason.message : String(reason) } }));
            }
        })();
    };

    if (!active) return null;

    return (
        <div className="space-y-3">
            <section className="rounded-lg border border-stone-200 p-4 dark:border-stone-800">
                <div className="text-sm font-semibold">{t("config.mediaHost.title")}</div>
                <div className="mt-1 text-xs text-stone-500">{t("config.mediaHost.description")}</div>
                {saveError ? (
                    <Alert className="mt-4" type="error" showIcon message={saveError} />
                ) : null}
                {loadError ? (
                    <Alert className="mt-4" type="error" showIcon message={t("config.mediaHost.failed")} description={loadError} />
                ) : null}
                {loading || !data ? (
                    <div className="flex min-h-32 items-center justify-center">
                        <Spin />
                    </div>
                ) : (
                    <Radio.Group value={selected === null ? "__default__" : selected} onChange={(event) => handleSelect(event.target.value === "__default__" ? null : String(event.target.value))} className="mt-4 flex flex-col gap-2" disabled={savingId !== null}>
                        {/* 最上面一行：按默认顺序（不指定首选）= selected null */}
                        <div className="flex items-center gap-3 rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800">
                            <Radio value="__default__" className="min-w-0 flex-1">
                                <span className="text-sm font-medium">{t("config.mediaHost.followDefault")}</span>
                            </Radio>
                        </div>
                        {data.sources.map((source) => {
                            const testState = testStates[source.id] || emptyTestState();
                            return (
                                <div key={source.id} className="rounded-lg border border-stone-200 px-4 py-3 dark:border-stone-800">
                                    <div className="flex items-center gap-3">
                                        <Radio value={source.id} disabled={!source.configured} className="min-w-0 flex-1">
                                            <span className="text-sm font-medium">{source.label}</span>
                                        </Radio>
                                        {source.configured ? (
                                            <Tag className="shrink-0">{t("config.mediaHost.configured")}</Tag>
                                        ) : (
                                            <Tag className="shrink-0" color="warning">{t("config.mediaHost.notConfigured")}</Tag>
                                        )}
                                        <Button size="small" disabled={testState.testing} loading={testState.testing} onClick={() => handleTest(source)}>
                                            {testState.testing ? t("config.mediaHost.testing") : t("config.mediaHost.test")}
                                        </Button>
                                    </div>
                                    {testState.testing ? null : testState.error ? (
                                        <div className="mt-2 text-xs text-red-600 dark:text-red-400">
                                            {t("config.mediaHost.failed")} {testState.error}
                                        </div>
                                    ) : testState.result ? (
                                        testState.result.ok ? (
                                            <div className="mt-2 text-xs text-stone-500">
                                                {t("config.mediaHost.ok", { ms: testState.result.ms ?? 0 })}
                                                {testState.result.url ? (
                                                    <>
                                                        {" · "}
                                                        <a className="break-all" href={testState.result.url} target="_blank" rel="noreferrer">{testState.result.url}</a>
                                                    </>
                                                ) : null}
                                            </div>
                                        ) : (
                                            <div className="mt-2 text-xs text-red-600 dark:text-red-400">
                                                {t("config.mediaHost.failed")} {testState.result.reason || ""}
                                            </div>
                                        )
                                    ) : null}
                                </div>
                            );
                        })}
                    </Radio.Group>
                )}
            </section>
        </div>
    );
}
