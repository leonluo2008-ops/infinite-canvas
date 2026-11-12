/**
 * AT-16 B6 前端探针（本地免费、不联网、不动真实任务）：**删除确认简化**（缺陷 D7）。
 *
 * 缺陷 D7（真机实证）：删除任务要手动输入任务号/任务名，过重——员工只想删掉一条建错的
 * 任务，却要先把任务号抄一遍。原实现 = `task-delete-modal.tsx` 里的输名 Modal。
 *
 * 本探针锁五件事：
 *
 *  [A] 运行态守卫没被削弱：hub-status.deleteGuard 矩阵原样
 *      · ingesting/discussing/rendering/generating → disabled + 「运行中不可删」
 *      · awaiting_assets/awaiting_approval → 可删（needName=true 只用于「已拆解会连产物一起删」提示）
 *      · ingested/failed/done → 可删、免确认
 *
 *  [B] 承接件（task-delete-confirm.tsx）就是二键确认，不是输名弹窗
 *      · 用 antd Popconfirm（红线 25 对位=canvas-side-panel.tsx:522「移除画布资产」同款）
 *      · 确认键危险红（okButtonProps.danger），有取消键
 *      · 文件里**没有** Input / 没有 confirmText / 没有 placeholder —— 用户零键入
 *      · 取消不接任何请求（onCancel 缺席），onConfirm 是唯一发请求路径
 *
 *  [C] confirm_name 由组件自动带 = 当前任务号（唯一出口 deleteConfirmName）
 *      · 注入 fetch 直测真 deleteHubTask → POST /api/hub/task/<id>/delete，body.confirm_name=<id>
 *
 *  [D] 列表与详情两入口都换成同一个件（不许只改一处留输名弹窗）
 *      · index.tsx 行内 + task-detail-drawer.tsx 头部都 import/render TaskDeleteButton
 *      · web/src 全域不再有 TaskDeleteModal / task-delete-modal.tsx / 输入任务号
 *      · deleteHubTask 调用点只有一处（同承接）
 *
 *  [E] 后端删除合同零改动（保留 confirm_name 校验，前端不是绕过它）
 *      · git diff HEAD -- wanrepro/ui/hub_api.py 为空
 *      · delete_task 仍对 awaiting_* 校验 confirm_name、仍 409 拒运行态
 *
 * 跑法：cd web && node scripts/probe-at16-b6.mjs      （失败 rc=1）
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { createRequire } from "node:module";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const REPO = path.dirname(WEB);
const CACHE = path.join(WEB, "node_modules", ".cache", "at16-b6");
mkdirSync(CACHE, { recursive: true });
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");
const exists = (rel) => {
    try {
        return statSync(path.join(WEB, rel)).isFile();
    } catch {
        return false;
    }
};

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 240)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 400)}` : ""}`);
    }
}

// ════════════════════════════════════════════════════════════ 打包真代码
const entry = path.join(CACHE, "entry.ts");
writeFileSync(
    entry,
    [
        'export { deleteGuard, HUB_RUNNING_STATES, HUB_DELETE_CONFIRM_STATES } from "../../../src/pages/hub/hub-status";',
        'export { deleteHubTask } from "../../../src/pages/hub/hub-api";',
        "",
    ].join("\n"),
);
const aliasPlugin = {
    name: "at16b6-alias",
    setup(build) {
        build.onResolve({ filter: /^@\// }, (args) => {
            const basePath = path.join(WEB, "src", args.path.slice(2));
            for (const candidate of [basePath, `${basePath}.ts`, `${basePath}.tsx`, path.join(basePath, "index.ts"), path.join(basePath, "index.tsx")]) {
                try {
                    if (statSync(candidate).isFile()) return { path: candidate };
                } catch {
                    /* 试下一个后缀 */
                }
            }
            return { path: basePath };
        });
    },
};
const out = path.join(CACHE, "bundle.mjs");
const built = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: out,
    jsx: "automatic",
    packages: "external",
    plugins: [aliasPlugin],
    define: { "import.meta.env": "{}" },
    loader: { ".css": "empty" },
    logLevel: "silent",
});
check("[0] 真代码打包成功（hub-status / hub-api 真模块）", built.errors.length === 0, built.errors.map((err) => `${err.location?.file}: ${err.text}`).join("; "));

// 纯 node 里跑真前端模块：补最小浏览器全局（store 在 import 期读 localStorage）
if (!globalThis.localStorage) {
    const store = new Map();
    globalThis.localStorage = {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, value) => store.set(key, String(value)),
        removeItem: (key) => store.delete(key),
        clear: () => store.clear(),
    };
}
if (!globalThis.window) globalThis.window = globalThis;

const api = await import(pathToFileURL(out).href + `?t=${Date.now()}`);
const { deleteGuard, HUB_RUNNING_STATES, HUB_DELETE_CONFIRM_STATES, deleteHubTask } = api;

// ════════════════════════════════════════════════════════════ [A] 运行态守卫矩阵
console.log("1) deleteGuard：运行中四态仍禁删，拆解过的 idle 态可删（needName 只作提示）");

check("[A] 运行中四态常量原样（ingesting/discussing/rendering/generating）",
    JSON.stringify(HUB_RUNNING_STATES) === JSON.stringify(["ingesting", "discussing", "rendering", "generating"]),
    JSON.stringify(HUB_RUNNING_STATES));
check("[A] 拆解过 idle 两态常量原样（awaiting_assets/awaiting_approval）",
    JSON.stringify(HUB_DELETE_CONFIRM_STATES) === JSON.stringify(["awaiting_assets", "awaiting_approval"]),
    JSON.stringify(HUB_DELETE_CONFIRM_STATES));

for (const state of HUB_RUNNING_STATES) {
    const guard = deleteGuard({ state });
    check(`[A] ${state} → 禁删 + 说明「运行中不可删」`,
        guard.disabled === true && guard.reason.includes("运行中") && guard.needName === false,
        JSON.stringify(guard));
}
for (const state of HUB_DELETE_CONFIRM_STATES) {
    const guard = deleteGuard({ state });
    check(`[A] ${state} → 可删，needName=true（只用于「已拆解会连产物清掉」提示，不再是输入框闸门）`,
        guard.disabled === false && guard.needName === true && guard.reason === "",
        JSON.stringify(guard));
}
for (const state of ["ingested", "failed", "done"]) {
    const guard = deleteGuard({ state });
    check(`[A] ${state} → 可删且免确认（needName=false）`,
        guard.disabled === false && guard.needName === false,
        JSON.stringify(guard));
}
check("[A] 空状态不崩（老任务缺 state 字段）", deleteGuard({}).disabled === false, JSON.stringify(deleteGuard({})));

// ════════════════════════════════════════════════════════════ [B] 承接件=二键确认
console.log("2) task-delete-confirm.tsx：Popconfirm 二键 + 零输入框 + 取消不发请求");

const confirmPath = "src/pages/hub/task-delete-confirm.tsx";
check("[B] 承接件存在（旧输名 Modal 文件已删除）",
    exists(confirmPath) && !exists("src/pages/hub/task-delete-modal.tsx"),
    `task-delete-confirm=${exists(confirmPath)} task-delete-modal=${exists("src/pages/hub/task-delete-modal.tsx")}`);
const confirmSrc = read(confirmPath);

check("[B] 用 antd Popconfirm（红线 25 对位：canvas-side-panel.tsx:522 移除画布资产同款）",
    confirmSrc.includes("Popconfirm") && confirmSrc.includes("antd"));
check("[B] 确认键是危险红钮（okButtonProps={{ danger: true … }}）",
    /okButtonProps=\{\{\s*danger:\s*true/.test(confirmSrc));
check("[B] 有取消键且**不接任何请求**（无 onCancel、cancelButtonProps 只带标记）",
    confirmSrc.includes('cancelText="取消"') && !/onCancel=/.test(confirmSrc),
    (confirmSrc.match(/onCancel=(.{0,40})/) || ["无"])[0]);
check("[B] 唯一发请求路径是 onConfirm（onConfirm={() => doDelete()}）",
    confirmSrc.includes("onConfirm={() => doDelete()}") && (confirmSrc.match(/doDelete\(\)/g) || []).length === 1,
    `doDelete() 次数=${(confirmSrc.match(/doDelete\(\)/g) || []).length}`);
check("[B] 文件中没有任何输入控件 / 输入状态（Input / confirmText / placeholder 全无）",
    !confirmSrc.includes("<Input") && !/[{,]\s*Input[\s,}]/.test(confirmSrc) &&
    !confirmSrc.includes("confirmText") && !confirmSrc.includes("placeholder"),
    `Input=${confirmSrc.includes("<Input")} confirmText=${confirmSrc.includes("confirmText")} placeholder=${confirmSrc.includes("placeholder")}`);
check("[B] 运行态由 deleteGuard 管住（blocked = guard.disabled || disabled || busy），弹层也一起禁用",
    confirmSrc.includes("deleteGuard(task)") && confirmSrc.includes("const blocked = guard.disabled || disabled || busy;")
    && /disabled=\{blocked\}/.test(confirmSrc));
check("[B] 确认弹层写明任务号与「不可恢复」（二键也让人看清删的是谁）",
    confirmSrc.includes("删除任务 ${task.local_id}？") && confirmSrc.includes("不可恢复"));
check("[B] 桥端原文透出（409「运行中删除拒绝」不被吞成通用失败）",
    // AT-18：同一句「删除失败：<原文>」现在除了 toast 还会回调 onError 交给调用方在行内留痕；
    // 断言口径改为「文案前缀在 + 走 errorText 原文 + 有行内回调」，语义只增不减。
    confirmSrc.includes("message.error(`删除失败：") && confirmSrc.includes("errorText(error)")
    && confirmSrc.includes("onError?.(text)"));

// ════════════════════════════════════════════════════════════ [C] confirm_name 自动带任务号
console.log("3) confirm_name：二键确认后自动带当前任务号（用户零键入），请求形状直测");

check("[C] 唯一出口 deleteConfirmName(task) 返回 local_id",
    confirmSrc.includes("export function deleteConfirmName") &&
    /export function deleteConfirmName\(task: Pick<HubTaskSummary, "local_id">\): string \{\s*return task\.local_id;/.test(confirmSrc));
check("[C] 调用点 = deleteHubTask(task.local_id, deleteConfirmName(task))",
    confirmSrc.includes("await deleteHubTask(task.local_id, deleteConfirmName(task));"));

const calls = [];
globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, deleted: "auto-9001" }) };
};
const deleted = await deleteHubTask("auto-9001", "auto-9001");
check("[C] 真 deleteHubTask → POST /api/hub/task/auto-9001/delete",
    calls.length === 1 && calls[0].url === "/api/hub/task/auto-9001/delete" && calls[0].method === "POST", JSON.stringify(calls));
check("[C] 请求体 confirm_name = 任务号（桥端 awaiting_* 的合同要求）",
    JSON.parse(calls[0].body).confirm_name === "auto-9001", calls[0].body);
check("[C] 回包原样透出（不改写 ok/deleted）", deleted.ok === true && deleted.deleted === "auto-9001", JSON.stringify(deleted));

// ════════════════════════════════════════════════════════════ [D] 双入口同承接
console.log("4) 列表行内 + 详情头两个入口都换成同一个件（不许留输名弹窗）");

const indexSrc = read("src/pages/hub/index.tsx");
const drawerSrc = read("src/pages/hub/task-detail-drawer.tsx");

check("[D] 列表：import TaskDeleteButton from ./task-delete-confirm",
    indexSrc.includes('import { TaskDeleteButton } from "./task-delete-confirm";'));
check("[D] 列表：行内操作列渲染 TaskDeleteButton（带 stopPropagation，点删除不开抽屉）",
    // AT-18 B3b：行内操作列抽成导出组件 HubRowActions（探针要能 SSR 真渲染同一份代码），
    // 按钮仍是同一个 TaskDeleteButton + 同一个 stopPropagation；断言改为语义匹配而非整行字面量。
    /<TaskDeleteButton\s+task=\{task\}\s+size="small"\s+stopPropagation/.test(indexSrc));
check("[D] 列表：删完回调 handleDeleted（摘行 + 关 Drawer 若有）",
    indexSrc.includes("onDeleted={handleDeleted}") && indexSrc.includes("const handleDeleted = useCallback("));
check("[D] 列表：旧的 deleteTarget 状态 / TaskDeleteModal 渲染全部删除",
    !indexSrc.includes("deleteTarget") && !indexSrc.includes("TaskDeleteModal") && !/\baskDelete\b/.test(indexSrc));

check("[D] 详情：import 同一个件", drawerSrc.includes('import { TaskDeleteButton } from "./task-delete-confirm";'));
check("[D] 详情：HeaderActions 用它（label=删除任务，写操作在跑时一并禁用）",
    /function HeaderActions\([\s\S]{0,600}<TaskDeleteButton task=\{summary\} label="删除任务" disabled=\{busy !== null\} onDeleted=\{onDeleted\} \/>/.test(drawerSrc));
check("[D] 详情：父级只收删除结果（onDeleted），不再「请求删除」回调",
    drawerSrc.includes("onDeleted: (localId: string) => void;") && !drawerSrc.includes("onRequestDelete"));
check("[D] 详情里没有第二套 Popconfirm（同承接，不各写一套）",
    !drawerSrc.includes("<Popconfirm"));

// 全域：旧形态彻底没了 + 调用点唯一
const hubDir = path.join(WEB, "src", "pages", "hub");
const hubFiles = readdirSync(hubDir).filter((name) => /\.tsx?$/.test(name));
const hubTexts = hubFiles.map((name) => [name, readFileSync(path.join(hubDir, name), "utf-8")]);
const withOldModal = hubTexts.filter(([, text]) => text.includes("TaskDeleteModal") || text.includes("task-delete-modal")).map(([name]) => name);
check("[D] hub 目录里没有任何 TaskDeleteModal / task-delete-modal 残留", withOldModal.length === 0, withOldModal.join(","));
const withTypeName = hubTexts.filter(([, text]) => text.includes("输入任务号")).map(([name]) => name);
check("[D] hub 目录里再没有「输入任务号」类文案", withTypeName.length === 0, withTypeName.join(","));
const callSites = hubTexts.filter(([, text]) => text.includes("deleteHubTask(") && !text.includes("export async function deleteHubTask(")).map(([name]) => name);
check("[D] deleteHubTask 消费方唯一（同承接：只有承接件发删除请求；hub-api.ts 只是定义/封装）",
    callSites.length === 1 && callSites[0] === "task-delete-confirm.tsx", callSites.join(","));

// ════════════════════════════════════════════════════════════ [E] 后端合同零改动
console.log("5) 后端删除合同零改动：confirm_name 校验仍在，前端不是绕过它");

let backendDiff = "";
try {
    backendDiff = execFileSync("git", ["-C", REPO, "diff", "--name-only", "HEAD", "--", "wanrepro/ui/hub_api.py", "wanrepro/ui/server.py"], { encoding: "utf-8" }).trim();
} catch (error) {
    backendDiff = `git 调用失败：${error.message}`;
}
check("[E] wanrepro/ui/hub_api.py 与 ui/server.py 本次零改动（git diff HEAD 为空）", backendDiff === "", backendDiff);

const hubApiPy = readFileSync(path.join(REPO, "wanrepro", "ui", "hub_api.py"), "utf-8");
check("[E] delete_task 仍对 awaiting_assets/awaiting_approval 校验 confirm_name",
    /if st in TEARDOWN_IDLE_STATES and \(confirm_name or ""\)\.strip\(\) not in \{local_id, task_name\}/.test(hubApiPy));
check("[E] TEARDOWN_IDLE_STATES / RUNNING_STATES 定义原样",
    hubApiPy.includes('TEARDOWN_IDLE_STATES = ("awaiting_assets", "awaiting_approval")') &&
    hubApiPy.includes('RUNNING_STATES = ("ingesting", "discussing", "rendering", "generating")'));
check("[E] 运行态仍 409 拒删（桥端第二道门没被拆）",
    /if st in RUNNING_STATES:\s*\n\s*raise HubError\(f"任务运行中/.test(hubApiPy));

// ════════════════════════════════════════════════════════════ 收尾
console.log(`\n[probe-at16-b6] ${pass}/${pass + fails.length} PASS`);
for (const name of fails) console.log(`  FAIL ${name}`);
process.exit(fails.length ? 1 : 0);
