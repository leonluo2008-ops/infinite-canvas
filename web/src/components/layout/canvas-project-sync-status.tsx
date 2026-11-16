import { Modal } from "antd";
import { AlertTriangle, CloudOff, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { acceptForeignCanvasImport, declineForeignCanvasImport, useCanvasProjectSyncStore } from "@/services/canvas-project-sync";

/**
 * S3-B2 画布同步状态（全局挂一次，位置在 AppProviders 内、路由外）。
 *
 * 三件事，都必须**可见**（任务书 §2.3 / §2.4 的判据靠人眼也能验）：
 *  1. 离线态横幅：「离线，改动将在联网后同步」——启动门超时/断网时进入离线态，本地照常可写；
 *  2. 同步状态入口：「N 项改动待同步 / 同步失败：<原因> / N 个项目时间戳异常」——
 *     `mode=remote` 时也要能看见「还有东西没上去」（②：`setSync({error})` 以前没有任何人消费，
 *     用户在正常联网状态下完全看不到推送失败；③：时间戳非法、永远进不了 dirty 的行也在这里露头）；
 *  3. 跨账号确认弹窗：「检测到本机 N 个项目来自其他账号，导入到当前账号？」——**绝不自动迁移**，
 *     用户点「导入」才并入当前账号并上推；点「不导入」则隔离留档 + 记住不再询问这一对账号。
 *
 * 单人模式（mode=local、dirtyCount=0、invalidCount=0、无错误、无弹窗）不渲染任何东西 —— 零改动。
 */
export function CanvasProjectSyncStatus() {
    const { t } = useTranslation();
    const mode = useCanvasProjectSyncStore((state) => state.mode);
    const dirtyCount = useCanvasProjectSyncStore((state) => state.dirtyCount);
    const invalidCount = useCanvasProjectSyncStore((state) => state.invalidCount);
    const error = useCanvasProjectSyncStore((state) => state.error);
    const brokenArchiveCount = useCanvasProjectSyncStore((state) => state.brokenArchiveCount);
    const importPrompt = useCanvasProjectSyncStore((state) => state.importPrompt);

    /**
     * residual#2（S3-B2.6）：offline 分支以前只显示通用文案，`enterOffline` 传进来的**具体原因**
     * （启动门超时 / 清单 unit 不合约 / 合并结果写盘失败）在离线态完全不可见——用户只看到「离线」，
     * 无从判断是网络问题还是本地缓存写不进去。这里：有 error 就显示它，没有才退回通用文案。
     */
    const offlineText = error ? t("canvasSync.offlineError", { message: error }) : t("canvasSync.offline");
    const showSyncEntry = mode === "remote" && Boolean(error || dirtyCount > 0 || invalidCount > 0 || brokenArchiveCount > 0);

    return (
        <>
            {mode === "offline" ? (
                <div className="pointer-events-none fixed bottom-4 left-4 z-50 flex max-w-[min(92vw,360px)] items-start gap-2 rounded-lg border border-amber-300/70 bg-amber-50/95 px-3 py-2 text-xs text-amber-900 shadow-sm backdrop-blur dark:border-amber-500/40 dark:bg-amber-950/90 dark:text-amber-100">
                    <CloudOff className="mt-0.5 size-4 shrink-0" />
                    <span className="leading-5">
                        <span className={`block ${error ? "font-medium" : ""}`}>{offlineText}</span>
                        {dirtyCount > 0 ? <span className="block opacity-80">{t("canvasSync.offlineDirty", { count: dirtyCount })}</span> : null}
                        {brokenArchiveCount > 0 ? (
                            <span className="block opacity-80">{t("canvasSync.brokenArchive", { count: brokenArchiveCount })}</span>
                        ) : null}
                    </span>
                </div>
            ) : null}

            {showSyncEntry ? (
                <div
                    role="status"
                    className="pointer-events-none fixed bottom-4 left-4 z-50 flex max-w-[min(92vw,360px)] items-start gap-2 rounded-lg border border-sky-300/70 bg-sky-50/95 px-3 py-2 text-xs text-sky-900 shadow-sm backdrop-blur dark:border-sky-500/40 dark:bg-sky-950/90 dark:text-sky-100"
                >
                    {error ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" /> : <RefreshCw className="mt-0.5 size-4 shrink-0" />}
                    <span className="leading-5">
                        {error ? <span className="block">{error}</span> : null}
                        {dirtyCount > 0 ? <span className="block opacity-80">{t("canvasSync.pendingDirty", { count: dirtyCount })}</span> : null}
                        {invalidCount > 0 ? <span className="block opacity-80">{t("canvasSync.invalidTimestamp", { count: invalidCount })}</span> : null}
                        {brokenArchiveCount > 0 ? <span className="block opacity-80">{t("canvasSync.brokenArchive", { count: brokenArchiveCount })}</span> : null}
                    </span>
                </div>
            ) : null}

            <Modal
                open={Boolean(importPrompt)}
                title={t("canvasSync.crossAccountTitle", { count: importPrompt?.count ?? 0 })}
                okText={t("canvasSync.crossAccountImport")}
                cancelText={t("canvasSync.crossAccountSkip")}
                closable={false}
                maskClosable={false}
                onOk={() => void acceptForeignCanvasImport()}
                onCancel={() => void declineForeignCanvasImport()}
            >
                <p className="text-sm leading-6">
                    {t("canvasSync.crossAccountBody", {
                        owner: importPrompt?.fromOwner ?? "",
                        target: importPrompt?.toOwner ?? "",
                        count: importPrompt?.count ?? 0,
                    })}
                </p>
            </Modal>
        </>
    );
}
