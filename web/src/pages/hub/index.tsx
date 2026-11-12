import { History, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import { App, Button, Table, Tag, Tooltip, type TableColumnsType } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { readApprovedSignature, readGenDraft } from "@/lib/hub/gen-settings";
import { useConfigStore } from "@/stores/use-config-store";

import {
    errorText,
    hubQuickAction,
    hubQuickChannels,
    hubQuickPhaseText,
    listHubTasks,
    runHubQuickAction,
    type HubQuickAction,
    type HubQuickChannel,
    type HubTaskSummary,
} from "./hub-api";
import { HubStateTag, SubmitResultLine } from "./hub-status";
import { lapianClockTexts, lapianStageName, useLapianClock } from "./lapian-clock";
import { LegacyHubView } from "./legacy-view";
import { NewTaskModal } from "./new-task-modal";
import { TaskDeleteButton } from "./task-delete-confirm";
import { TaskDetailDrawer } from "./task-detail-drawer";

/**
 * 自动化工作台 · 表格主视图（AT-9 B3/B4）：
 * 摘要端点驱动表格（任务名/状态/已换资产数/创建时间/操作），点行开详情 Drawer；
 * 顶部「新建任务」弹窗；删除二次确认（运行中禁用+tooltip；awaiting_* 输任务名确认）；
 * 5s 列表轮询；?legacy=1 走旧卡片流（B6 验收过后删）。
 */
export default function HubPage() {
    const [searchParams] = useSearchParams();
    if (searchParams.get("legacy") === "1") return <LegacyHubView />;
    return <HubTableView />;
}

function HubTableView() {
    const { message } = App.useApp();
    const config = useConfigStore((state) => state.config);
    const channels = useMemo(() => hubQuickChannels(config), [config]);
    const [tasks, setTasks] = useState<HubTaskSummary[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState("");
    const [createOpen, setCreateOpen] = useState(false);
    const [openId, setOpenId] = useState<string | null>(null);
    /** AT-16 B5a-2：行内快捷入口正在处理的任务号（同一时刻只允许一条）。 */
    const [quickBusy, setQuickBusy] = useState("");

    const load = useCallback(async (manual = false) => {
        if (manual) setRefreshing(true);
        try {
            const next = await listHubTasks();
            setTasks(next);
            setError("");
        } catch (loadError) {
            setError(errorText(loadError));
        } finally {
            setLoading(false);
            if (manual) setRefreshing(false);
        }
    }, []);

    /**
     * 列表轮询（摘要端点，列表不拉详情）。
     * AT-16 B8：提交在途时加密到 2s——主状态条要跟着 submit 形态走（提交处理中 → 结果未知），
     * 5s 粒度会把短于一轮的在途窗口整个跳过去（auto-0043 实测在途只有 4.5s，全程零变化）。
     */
    useEffect(() => {
        void load();
        const timer = window.setInterval(() => void load(), quickBusy ? 2000 : 5000);
        return () => window.clearInterval(timer);
    }, [load, quickBusy]);

    const patchState = useCallback((localId: string, state: string) => {
        setTasks((prev) => prev.map((item) => (item.local_id === localId ? { ...item, state } : item)));
    }, []);

    const openTask = useMemo(() => tasks.find((item) => item.local_id === openId) || null, [tasks, openId]);

    // 删除成功：从列表摘除；若删的是打开中的任务 → 关 Drawer。
    // 二键确认件（TaskDeleteButton）在列表行内与抽屉头共用，成功后都回调这里。
    const handleDeleted = useCallback(
        (localId: string) => {
            setTasks((prev) => prev.filter((item) => item.local_id !== localId));
            setOpenId((prev) => (prev === localId ? null : prev));
            void load();
        },
        [load],
    );

    /**
     * 行内快捷入口（AT-16 B5a-2）：与抽屉顶部 / 底部按钮**同一条链**（hubQuickAction → runHubQuickAction）。
     * 受理只报「提交处理中」；轮询确认 generating / done 才报「云端已收单」；重复点击被拒（本地在跑 + 后端 409）。
     */
    const fireQuick = useCallback(
        async (task: HubTaskSummary, action: HubQuickAction) => {
            if (!action.enabled) {
                message.warning(action.reason || "这个入口现在不可用");
                return;
            }
            setQuickBusy(task.local_id);
            try {
                await runHubQuickAction(task.local_id, action, {
                    onPhase: (phase, state, note) => {
                        // AT-16 B8：受理/收尾都**立刻重取主状态**，不等 runHubQuickAction 内部
                        // 轮询跑完（它最长 40s）——点提交后 1 秒内列表行必须从「已批准·待提交」
                        // 走到「提交处理中」；这条刷新是三处入口共用的一次动作，不是第二套判定。
                        void load();
                        const line = hubQuickPhaseText(phase, state);
                        const text = note || line.text;
                        if (!text) return;
                        if (line.level === "success") message.success(text);
                        else if (line.level === "warning") message.warning(text);
                        else message.info(text);
                    },
                });
                void load(true);
            } catch (quickError) {
                // 后端原文（如「已有提交在跑」409）直接透出——不吞、不美化
                message.error(errorText(quickError));
                // 被拒也要回到后端事实（主状态不留在旧文案上）
                void load();
            } finally {
                setQuickBusy("");
            }
        },
        [load, message],
    );

    const columns: TableColumnsType<HubTaskSummary> = [
        {
            title: "任务名",
            dataIndex: "task_name",
            key: "task_name",
            ellipsis: true,
            render: (_, task) => (
                <div className="min-w-0">
                    <div className="truncate font-mono text-sm font-medium">{task.task_name || task.local_id}</div>
                    <div className="truncate text-xs text-stone-400 dark:text-stone-500">{task.video_name || "参考视频"}</div>
                </div>
            ),
        },
        {
            title: "状态",
            dataIndex: "state",
            key: "state",
            width: 240,
            render: (_, task) => (
                <div className="space-y-1">
                    {/* AT-16 B5a-1：有提交结果就用四类形态说话（不再只按 state 显示「已批准·待提交」）；
                        结论/原文来自摘要的 submit 字段，5 秒轮询刷新后照旧在——不靠临时 toast。 */}
                    <HubStateTag state={task.state} submit={task.submit} />
                    <SubmitResultLine submit={task.submit} />
                    {task.state === "discussing" ? <LapianStageCell task={task} /> : null}
                </div>
            ),
        },
        {
            title: "已换资产数",
            dataIndex: "replaced",
            key: "replaced",
            width: 100,
            render: (_, task) =>
                task.replaced ? (
                    <span className="font-mono text-sm">{task.replaced}</span>
                ) : (
                    <span className="text-xs text-stone-400 dark:text-stone-500">—</span>
                ),
        },
        {
            title: "创建时间",
            dataIndex: "created_at",
            key: "created_at",
            width: 140,
            render: (value: string) => <span className="text-sm text-stone-500 dark:text-stone-400">{formatCreatedAt(value)}</span>,
        },
        {
            title: "操作",
            key: "actions",
            width: 230,
            render: (_, task) => {
                // AT-16 B5a-2：行内重提 / 续查入口（缺陷 D6：入口不再埋在抽屉 4-5 屏深）
                const quick = hubRowQuickAction(task, channels, quickBusy === task.local_id);
                return (
                    <div className="flex items-center gap-1.5" onClick={(event) => event.stopPropagation()}>
                        <Button
                            size="small"
                            type="primary"
                            ghost
                            onClick={(event) => {
                                event.stopPropagation();
                                setOpenId(task.local_id);
                            }}
                        >
                            打开
                        </Button>
                        {quick.kind !== "none" ? (
                            <Tooltip title={quick.enabled ? quick.hint : quick.reason}>
                                <span>
                                    <Button
                                        size="small"
                                        type={quick.kind === "submit" ? "primary" : "default"}
                                        ghost
                                        loading={quickBusy === task.local_id}
                                        disabled={!quick.enabled || quickBusy === task.local_id}
                                        data-hub-quick-action={quick.kind}
                                        data-hub-quick-enabled={quick.enabled ? "1" : "0"}
                                        data-hub-quick-source={quick.source}
                                        data-hub-quick-needs-config={quick.needsConfig ? "1" : "0"}
                                        onClick={(event) => {
                                            event.stopPropagation();
                                            void fireQuick(task, quick);
                                        }}
                                    >
                                        {quick.label}
                                    </Button>
                                </span>
                            </Tooltip>
                        ) : null}
                        {/* AT-16 B6：删除=二键确认（危险红钮），不再手输任务名；运行中由 deleteGuard 禁用。
                            与详情头同一个组件（同承接同守卫），确认后自动带任务号作 confirm_name。 */}
                        <TaskDeleteButton task={task} size="small" stopPropagation onDeleted={handleDeleted} />
                    </div>
                );
            },
        },
    ];

    return (
        <div className="flex h-full flex-col overflow-hidden bg-stone-50 text-stone-900 dark:bg-stone-950 dark:text-stone-100">
            <main className="thin-scrollbar min-h-0 flex-1 overflow-y-auto p-3 md:p-5">
                <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4">
                    <header className="flex flex-wrap items-end justify-between gap-3">
                        <div>
                            <h1 className="text-2xl font-semibold text-stone-950 dark:text-stone-100">自动化工作台</h1>
                            <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">参考片 → 拆解 → 置换 → 提示词 → 出片；点行进任务详情。5 秒自动刷新。</p>
                        </div>
                        <div className="flex items-center gap-2">
                            <Link to="/hub?legacy=1" className="text-xs text-stone-400 underline decoration-dotted transition hover:text-stone-600 dark:text-stone-500 dark:hover:text-stone-300">
                                <History className="mr-1 inline size-3.5" />
                                旧版卡片流
                            </Link>
                            <Tag className="m-0 px-2 py-1">{tasks.length} 个任务</Tag>
                            <Button icon={<RefreshCw className={`size-4 ${refreshing ? "animate-spin" : ""}`} />} loading={refreshing} onClick={() => void load(true)}>
                                刷新
                            </Button>
                            <Button type="primary" icon={<Plus className="size-4" />} onClick={() => setCreateOpen(true)}>
                                新建任务
                            </Button>
                        </div>
                    </header>

                    {error ? (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300">
                            <span className="min-w-0 break-words">任务列表刷新失败：{error}</span>
                            <Button size="small" onClick={() => void load(true)}>
                                再试一次
                            </Button>
                        </div>
                    ) : null}

                    <div className="rounded-lg border border-stone-200 bg-card p-2 shadow-sm dark:border-stone-800 md:p-3">
                        <Table<HubTaskSummary>
                            rowKey="local_id"
                            columns={columns}
                            dataSource={tasks}
                            loading={loading && !tasks.length}
                            size="middle"
                            pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
                            onRow={(task) => ({
                                onClick: () => setOpenId(task.local_id),
                                className: "cursor-pointer",
                            })}
                            locale={{
                                emptyText: (
                                    <div className="flex min-h-[200px] flex-col items-center justify-center gap-2 text-sm text-stone-500 dark:text-stone-400">
                                        还没有任务，点右上「新建任务」拖一条参考视频开始。
                                    </div>
                                ),
                            }}
                        />
                    </div>
                </div>
            </main>

            <NewTaskModal
                open={createOpen}
                onClose={() => setCreateOpen(false)}
                onCreated={(localId, hit) => {
                    void load(true);
                    // AT-10 C5：幂等命中已有任务 → 直接把它的详情打开（提示文案由弹窗点名给出）
                    if (hit) setOpenId(localId);
                }}
            />

            {/* 详情 Drawer：key=任务号，切任务强制重挂载（轮询序号守卫双保险） */}
            {openTask ? (
                <TaskDetailDrawer
                    key={openTask.local_id}
                    task={openTask}
                    open={Boolean(openId)}
                    onClose={() => setOpenId(null)}
                    onStateChange={patchState}
                    onRefresh={() => void load()}
                    onDeleted={handleDeleted}
                />
            ) : null}
        </div>
    );
}

/**
 * 行内快捷入口的准入判定（AT-16 B5a-2；导出给探针直测纯函数）。
 *
 * 凭据解析（按批准渠道匹配 key）与守卫（active / blocksRetry / sizeBlocked / 缺 key）全在
 * `hub-api.hubQuickAction` 一处——这里只负责把列表摘要 + 本机批准签名 / 任务草稿喂进去。
 * 抽屉顶部入口与底部 submitOnly 也调同一个函数，所以「同承接同凭据同守卫」只有一份实现。
 */
export function hubRowQuickAction(task: HubTaskSummary, channels: HubQuickChannel[], busy = false) {
    return hubQuickAction({
        localId: task.local_id,
        state: String(task.state || ""),
        submit: task.submit,
        cloudTaskId: task.submit?.cloud_task_id || "",
        channels,
        approvedSignature: readApprovedSignature(task.local_id),
        draft: readGenDraft(task.local_id),
        busy,
    });
}

function formatCreatedAt(value: string) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/**
 * 主表「状态」列的拆解进度行（AT-16 B4a）：阶段文案 + 两个计时值（总耗时 / 本阶段已等待）。
 *
 * 走表口径全部来自 ./lapian-clock（1 秒展示钟与 5 秒列表轮询解耦，读数不随轮询冻结）；
 * 缺锚 → 明说「计时不可用」，不拿 created_at / 日志尾行凑数。抽成组件是为了让探针
 * 与详情的 ProgressSection 用**同一份**渲染代码断言两处覆盖（红线 25）。
 */
export function LapianStageCell({ task }: { task: HubTaskSummary }) {
    const clock = useLapianClock(task.lapian);
    const texts = lapianClockTexts(clock);
    return (
        <div className="space-y-0.5 text-xs text-stone-500 dark:text-stone-400" data-hub-lapian-cell>
            <div className="flex items-center gap-1">
                <LoaderCircle className="size-3 animate-spin" />
                <span className="truncate" data-hub-lapian-stage-name>{lapianStageName(task.stage) || "拆解中"}</span>
            </div>
            <Tooltip title={texts.note || undefined}>
                <div className="flex flex-wrap items-center gap-x-2 font-mono text-[11px] tabular-nums" data-hub-lapian-frozen={clock.frozen ? "1" : "0"}>
                    <span data-hub-lapian-total>{texts.total}</span>
                    <span data-hub-lapian-stage-wait>{texts.stage}</span>
                </div>
            </Tooltip>
        </div>
    );
}
