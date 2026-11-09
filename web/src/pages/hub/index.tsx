import { LoaderCircle, RefreshCw } from "lucide-react";
import { Button, Empty, Tag } from "antd";
import { useCallback, useEffect, useState } from "react";

import { errorText, listHubTasks, type HubTaskSummary } from "./hub-api";
import { NewTaskCard } from "./new-task-card";
import { TaskCard } from "./task-card";

/**
 * 自动化工作台中控台：任务卡片流 + 按状态操作。
 * 列表常开 5s 轮询；渲染/生成等阶段由卡片自己按状态补轮询。
 */
export default function HubPage() {
    const [tasks, setTasks] = useState<HubTaskSummary[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState("");

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

    useEffect(() => {
        void load();
        const timer = window.setInterval(() => void load(), 5000);
        return () => window.clearInterval(timer);
    }, [load]);

    const handleCreated = useCallback((task: HubTaskSummary) => {
        setTasks((prev) => [task, ...prev.filter((item) => item.local_id !== task.local_id)]);
    }, []);

    const patchState = useCallback((localId: string, state: string) => {
        setTasks((prev) => prev.map((item) => (item.local_id === localId ? { ...item, state } : item)));
    }, []);

    const handleRefresh = useCallback(() => {
        void load(true);
    }, [load]);

    return (
        <div className="flex h-full flex-col overflow-hidden bg-stone-50 text-stone-900 dark:bg-stone-950 dark:text-stone-100">
            <main className="thin-scrollbar min-h-0 flex-1 overflow-y-auto p-3 md:p-5">
                <div className="mx-auto grid w-full max-w-[1680px] gap-4">
                    <header className="flex flex-wrap items-end justify-between gap-3">
                        <div>
                            <h1 className="text-2xl font-semibold text-stone-950 dark:text-stone-100">自动化工作台</h1>
                            <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">参考片 → 拆解 → 置换 → 提示词 → 出片；任务状态自动刷新，按卡片上的操作往下走。</p>
                        </div>
                        <div className="flex items-center gap-2">
                            <Tag className="m-0 px-2 py-1">{tasks.length} 个任务</Tag>
                            <Button icon={<RefreshCw className={`size-4 ${refreshing ? "animate-spin" : ""}`} />} loading={refreshing} onClick={handleRefresh}>
                                刷新
                            </Button>
                        </div>
                    </header>

                    <NewTaskCard onCreated={handleCreated} onSubmitted={handleRefresh} />

                    {error ? (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700 dark:border-amber-950 dark:bg-amber-950/20 dark:text-amber-300">
                            <span className="min-w-0 break-words">任务列表刷新失败：{error}</span>
                            <Button size="small" onClick={handleRefresh}>
                                再试一次
                            </Button>
                        </div>
                    ) : null}

                    {loading && !tasks.length ? (
                        <div className="flex min-h-[240px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-stone-300 text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400">
                            <LoaderCircle className="size-5 animate-spin" />
                            正在读取任务列表…
                        </div>
                    ) : tasks.length ? (
                        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                            {tasks.map((task) => (
                                <TaskCard key={task.local_id} task={task} onStateChange={patchState} onRefresh={handleRefresh} />
                            ))}
                        </div>
                    ) : (
                        <div className="flex min-h-[280px] flex-col items-center justify-center rounded-lg border border-dashed border-stone-300 text-center dark:border-stone-700">
                            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有任务，先在上方拖一条参考视频建任务。" />
                        </div>
                    )}
                </div>
            </main>
        </div>
    );
}
