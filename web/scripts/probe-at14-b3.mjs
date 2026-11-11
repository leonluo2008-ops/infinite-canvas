/**
 * AT-14 B3 前端自检（免费探针）：esbuild 打包 hub-api.ts 后**真调用**本地引用换算函数，
 * 再对 drawer / task-card 做源码接线护栏（防「改了后端前端没接」「旧调用形态回潮」）。
 *
 * 覆盖：
 *   [1] 引用串形态：isLocalAssetRef 正例/负例（越界、大小写、错目录、多一层、错扩展名）
 *   [2] 显示换算：local 引用 → /api/hub/asset-file/<任务号>/<文件名>；非引用原样透传
 *   [3] 上传链：drawer 与 task-card 两个入口都带 taskId 上传、都把 ref 写进任务数据
 *   [4] 三态展示：expired → 「图已过期，请重传」、unknown → 「图片不可用，请重试」+ 状态属性
 *   [5] 探针/上下文接线：probeHubAssets → /api/hub/asset-probe、AssetStateContext 注入与消费
 *
 * 跑法：cd web && node scripts/probe-at14-b3.mjs   （失败 rc=1）
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(path.resolve("package.json"));
const esbuild = require("esbuild");

const WEB = process.cwd();
const read = (rel) => readFileSync(path.join(WEB, rel), "utf-8");

let pass = 0;
const fails = [];
function check(name, cond, detail = "") {
    if (cond) {
        pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 200)}` : ""}`);
    } else {
        fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 300)}` : ""}`);
    }
}

// ---------------------------------------------------------------- [1][2] 真函数
const outDir = path.join(WEB, "node_modules", ".cache", "at14-b3-probe");
mkdirSync(outDir, { recursive: true });
const bundlePath = path.join(outDir, "hub-api.bundle.mjs");
const built = await esbuild.build({
    entryPoints: [path.join(WEB, "src/pages/hub/hub-api.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    outfile: bundlePath,
    logLevel: "silent",
    define: { "import.meta.env": "{}" },
});
check("esbuild 打包 hub-api.ts 成功", !built.errors.length, built.errors.map((e) => e.text).join("; "));
const mod = await import(pathToFileURL(bundlePath).href + `?t=${Date.now()}`);
check("导出 isLocalAssetRef / hubAssetDisplayUrl",
    typeof mod.isLocalAssetRef === "function" && typeof mod.hubAssetDisplayUrl === "function");

const REF = "local:auto-0002/asset-uploads/e955c2ea7800b90c.png";
check("[1] 合法引用 → true", mod.isLocalAssetRef(REF) === true, REF);
const negatives = {
    "公网直链不是本地引用": "https://h.uguu.se/WYrMMuFR.png",
    "dataUrl 不是本地引用": "data:image/png;base64,AAAA",
    "越界 ..": "local:../etc/asset-uploads/e955c2ea7800b90c.png",
    "大写 hex 不认（本模块只写小写）": "local:auto-0002/asset-uploads/E955C2EA7800B90C.png",
    "目录名不对": "local:auto-0002/uploads/e955c2ea7800b90c.png",
    "多一层子目录": "local:auto-0002/asset-uploads/sub/e955c2ea7800b90c.png",
    "扩展名不在白名单": "local:auto-0002/asset-uploads/e955c2ea7800b90c.exe",
    "hex 位数不足 16": "local:auto-0002/asset-uploads/e955c2ea.png",
    "任务号带斜杠": "local:a/b/asset-uploads/e955c2ea7800b90c.png",
};
for (const [name, bad] of Object.entries(negatives)) {
    check(`[1] 负例拒：${name}`, mod.isLocalAssetRef(bad) === false, bad);
}
check("[2] local 引用 → 取图 URL",
    mod.hubAssetDisplayUrl(REF) === "/api/hub/asset-file/auto-0002/e955c2ea7800b90c.png",
    mod.hubAssetDisplayUrl(REF));
check("[2] 任务号做 URL 编码（防注入路径）",
    mod.hubAssetDisplayUrl("local:a_b-9/asset-uploads/0123456789abcdef.jpg")
        === "/api/hub/asset-file/a_b-9/0123456789abcdef.jpg");
const URL_OLD = "https://h.uguu.se/WYrMMuFR.png";
check("[2] 非引用原样透传（旧任务不吃亏）", mod.hubAssetDisplayUrl(URL_OLD) === URL_OLD);

// ---------------------------------------------------------------- [3] 上传链接线
const drawer = read("src/pages/hub/task-detail-drawer.tsx");
const card = read("src/pages/hub/task-card.tsx");
check("[3] drawer 上传带任务号 uploadHubImage(file, taskId)",
    drawer.includes("await uploadHubImage(file, taskId)"));
check("[3] drawer 写任务数据的是引用串 image_url: uploadedRef.ref",
    drawer.includes("image_url: uploadedRef.ref"));
check("[3] drawer 不再有旧形态 uploadHubImage(file)（防回退）",
    !/uploadHubImage\(file\)/.test(drawer));
check("[3] 卡片入口同样带任务号 + 写引用串",
    card.includes("await uploadHubImage(file, taskId)") && card.includes("image_url: uploadedRef.ref"));
check("[3] 卡片入口无旧形态", !/uploadHubImage\(file\)/.test(card));

// ---------------------------------------------------------------- [4] 三态展示
check("[4] expired 文案「图已过期，请重传」", drawer.includes("图已过期，请重传"));
check("[4] unknown 文案「图片不可用，请重试」", drawer.includes("图片不可用，请重试"));
check("[4] 失效态出占位块且带状态属性（可被 DOM 断言）", drawer.includes("data-hub-asset-state"));
check("[4] 三态只由服务端判定驱动（不在前端猜 403）", !drawer.includes("status === 403") && !drawer.includes("status === 404"));
check("[4] 已传图缩略统一走 UploadedThumb（不再裸 <img> 直出旧 URL）",
    drawer.includes("<UploadedThumb") && !drawer.includes('<a key={url} href={url}'));

// ---------------------------------------------------------------- [5] 探针/上下文
const api = read("src/pages/hub/hub-api.ts");
check("[5] hub-api 声明 probeHubAssets", api.includes("export async function probeHubAssets"));
check("[5] 探针走 /api/hub/asset-probe/<id>", api.includes("/api/hub/asset-probe/"));
check("[5] 上传请求带 X-Task-Id 头", api.includes('headers["X-Task-Id"]'));
check("[5] drawer 拉探针并注入上下文",
    drawer.includes("probeHubAssets(localId)") && drawer.includes("AssetStateContext.Provider"));
check("[5] AssetUploadZone 消费上下文状态", drawer.includes("useContext(AssetStateContext)"));

// ---------------------------------------------------------------- 结果
console.log(`probe-at14-b3: ${pass} PASS, ${fails.length} FAIL`);
for (const item of fails) console.log(`  FAIL ${item}`);
process.exit(fails.length ? 1 : 0);
