/**
 * AT-16 B3 真机探针（ego 真浏览器，只读）：**建议行锚点的图片确实可见**（naturalWidth>0，不是只挂了个 DOM 壳）。
 *
 * 为什么必须真机：SSR/离线探针（probe-at16-b3.mjs）只能证结构，证不了图真的加载出来——缺陷形态正是
 * 「DOM 里有 img 但图裂/尺寸 0」。本探针在真浏览器里跑两段：
 *   [A] 真实任务只读走查：自动挑一个「拆解完且带建议」的真实任务，打开详情逐行断言——
 *       锚点图 naturalWidth>0 且渲染 >20px、锚点来源与后端 asset_cards 一致、own 框行号=本行行号
 *       （有自身框）/ 不出 own 框（有帧无框）、行内 #编号=建议行号、四类行同锚、缩略缩放一致。
 *   [B] 组成员 + 缺帧（真实任务里无现成组，给真实任务点「成组」属写操作=不允许）：在**同一页**把
 *       GET /api/hub/task/* 的响应换成本次真实快照派生的夹具（只动两处：1/5 行置成确认组、2 行清空帧），
 *       重开详情后断言组成员行走 GroupFrame 单帧单框（框=主行、两行同图）、缺帧行诚实占位（零 img）。
 *       真实任务数据与磁盘文件一字未改（只读走查）。
 *
 * 跑法（git-bash）：cd web && ego-browser nodejs < scripts/ego-at16-b3-anchor.mjs
 */
const HUB_URL = "http://localhost:3000/hub";
const POST_DECOMPOSE = ["awaiting_assets", "rendering", "awaiting_approval", "approved", "generating", "done", "failed"];

const result = { pass: 0, fails: [] };
function check(name, cond, detail = "") {
    if (cond) {
        result.pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 200)}` : ""}`);
    } else {
        result.fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 500)}` : ""}`);
    }
}

/** 页面内收集每行 DOM 实测（返回值只含纯数据）。 */
function collectRows() {
    const rectOf = (node) => {
        if (!node) return null;
        const r = node.getBoundingClientRect();
        return { w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 };
    };
    return [...document.querySelectorAll("[data-hub-suggest-row]")].map((row) => {
        const anchor = row.querySelector("[data-hub-suggest-anchor]");
        const frame = anchor ? anchor.querySelector("[data-hub-frame-state]") : null;
        const stage = anchor ? anchor.querySelector("[data-hub-frame-stage]") : null;
        const images = anchor ? [...anchor.querySelectorAll("img")].map((node) => ({
            src: node.getAttribute("src") || "",
            naturalWidth: node.naturalWidth || 0,
            naturalHeight: node.naturalHeight || 0,
            complete: Boolean(node.complete),
            rect: rectOf(node),
        })) : [];
        const scope = anchor ? anchor.querySelector("[data-hub-frame-content-box]") : null;
        const caption = anchor ? anchor.querySelector("[data-hub-frame-caption]") : null;
        const boxes = (scope ? [...scope.querySelectorAll("[data-hub-box-row]")] : []).map((node) => ({
            row: Number(node.getAttribute("data-hub-box-row")),
            own: node.getAttribute("data-hub-box-own"),
            rect: rectOf(node),
        }));
        return {
            n: Number(row.getAttribute("data-hub-suggest-row")),
            tone: row.getAttribute("data-hub-suggest-tone") || "",
            text: (row.textContent || "").replace(/\s+/g, " ").trim(),
            anchorSource: anchor ? anchor.getAttribute("data-hub-suggest-anchor-source") || "" : "",
            groupKey: anchor ? anchor.getAttribute("data-hub-suggest-group-key") || "" : "",
            placeholder: anchor ? Boolean(anchor.querySelector("[data-hub-suggest-placeholder]")) : false,
            frameState: frame ? frame.getAttribute("data-hub-frame-state") || "" : "",
            frameScale: frame ? frame.getAttribute("data-hub-frame-scale") || "" : "",
            frameStage: stage !== null,
            isGroupFrame: anchor ? anchor.querySelector("[data-hub-group-frame]") !== null : false,
            stageRect: rectOf(stage),
            captionRect: rectOf(caption),
            images,
            boxes,
        };
    });
}

const frameUrlOf = (card) =>
    String((card.代表帧 && card.代表帧.url) || "").trim()
    || (card.帧 || []).map((f) => String(f.url || "").trim()).find(Boolean)
    || "";

/** 截图只是佐证：窗口最小化时 ego 截不了（已知限制），DOM 量测才是证据，不因此判红。 */
async function shot(filePath) {
    try {
        console.log(`[shot] ${await page.screenshot({ path: filePath })}`);
    } catch (err) {
        console.log(`[shot] 跳过截图（${err && err.message}）`);
    }
}

const space = await taskSpace("AT-16 B3 建议锚点真机验收");
const page = space.page("p1");
console.log(`[space] ${space.spaceId} page=${page.label}`);

await page.goto(HUB_URL, { timeout: 60_000 });
await page.waitForSelector("tr[data-row-key]", { state: "attached", timeout: 45_000 });

// ── 选一个「拆解完且带建议」的真实任务（只读挑，不写）
const picked = await page.evaluate(async (states) => {
    const list = await (await fetch("/api/hub/tasks")).json();
    const all = (list.tasks || []).map((t) => `${t.local_id}:${t.state}`);
    for (const summary of list.tasks || []) {
        if (!states.includes(summary.state)) continue;
        const detail = await (await fetch(`/api/hub/task/${encodeURIComponent(summary.local_id)}`)).json();
        const task = detail.task || {};
        const suggestions = Array.isArray(task.suggestions) ? task.suggestions : [];
        const cards = Array.isArray(task.asset_cards) ? task.asset_cards : [];
        if (suggestions.length && cards.length) return { localId: summary.local_id, state: task.state || "", all };
    }
    return { localId: "", state: "", all };
}, POST_DECOMPOSE);

check("[A] 找到可只读走查的真实任务（拆解完成 + 有建议 + 有资产卡）", Boolean(picked.localId), `tasks=${picked.all.join(" ")}`);
if (!picked.localId) {
    console.log(`\nego-at16-b3-anchor: ${result.pass} PASS, ${result.fails.length} FAIL`);
    process.exit(1);
}
console.log(`[A] 走查对象：${picked.localId}（state=${picked.state}，只读）`);

await page.click(`tr[data-row-key="${picked.localId}"]`, { label: `打开 ${picked.localId} 详情` });
await page.waitForSelector("[data-hub-suggest-row]", { state: "visible", timeout: 45_000 });
await page.waitForTimeout(1500);
// 展开折叠区：可沿用行也在里面，折叠不改变渲染，但要能看到它们也挂了锚点
await page.evaluate(() => document.querySelectorAll(".hub-detail-drawer details").forEach((node) => { node.open = true; }));
await page.waitForTimeout(600);

const realTask = await page.evaluate(
    async (taskId) => (await (await fetch(`/api/hub/task/${encodeURIComponent(taskId)}`)).json()).task || {},
    picked.localId,
);
const realRows = await page.evaluate(collectRows);
const realCards = (realTask.asset_cards || []).map((card) => ({ ...card, n: Number(card.n) }));
const realCardsByN = new Map(realCards.map((card) => [card.n, card]));
const realRowsByN = new Map(realRows.map((row) => [row.n, row]));
const realSuggestions = realTask.suggestions || [];

console.log(`2) 真实任务 ${picked.localId}：建议 ${realSuggestions.length} 条 / 资产卡 ${realCards.length} 张 / 渲染行 ${realRows.length} 条`);
check("[A] 建议行全部渲染（条数与后端建议一致）", realRows.length === realSuggestions.length && realRows.length > 0,
    `${realRows.length} vs ${realSuggestions.length}`);
check("[A] 真实数据里同时存在「有帧有框」与「有帧无框」两类行",
    realCards.some((card) => !card.组 && card.自身框 && frameUrlOf(card))
    && realCards.some((card) => !card.组 && !card.自身框 && frameUrlOf(card)),
    realCards.map((card) => `${card.n}:${card.组 ? "组" : card.自身框 ? "框" : "无框"}`).join(" "));

console.log("3) 锚点图真的可见（naturalWidth>0 且渲染尺寸 >20px）");
for (const item of realSuggestions) {
    const row = realRowsByN.get(Number(item.n));
    const card = realCardsByN.get(Number(item.n));
    if (!row || !card) {
        check(`#${item.n} 行与资产卡都在场`, Boolean(row && card));
        continue;
    }
    const wantSource = card.组 ? "group" : frameUrlOf(card) ? "frame" : "none";
    check(`#${item.n} 锚点来源=${wantSource}（与后端 asset_cards 一致）`, row.anchorSource === wantSource, `实测=${row.anchorSource}`);
    check(`#${item.n} 行文本里的 #编号 = 建议行号`, row.text.includes(`#${item.n}`), row.text.slice(0, 48));
    if (wantSource === "none") {
        check(`#${item.n} 无帧行诚实占位（零 img、无帧舞台）`, row.placeholder && row.images.length === 0 && !row.frameStage);
    } else {
        const visible = row.images.length > 0 && row.images.every((img) =>
            img.complete && img.naturalWidth > 0 && img.naturalHeight > 0 && img.rect && img.rect.w > 20 && img.rect.h > 20);
        check(`#${item.n} 帧图实际可见（naturalWidth>0 且渲染 >20px）`, visible,
            JSON.stringify(row.images.map((img) => ({ nw: img.naturalWidth, nh: img.naturalHeight, rect: img.rect, src: img.src.slice(0, 40) }))));
    }
}

console.log("4) 框归属正确（own 框行号=本行；有帧无框不出 own 框）");
for (const item of realSuggestions) {
    const card = realCardsByN.get(Number(item.n));
    const row = realRowsByN.get(Number(item.n));
    if (!card || !row || card.组 || !frameUrlOf(card)) continue;
    const own = row.boxes.filter((box) => box.own === "1");
    if (card.自身框) {
        check(`#${item.n} 有自身框 → 恰一枚 own 框、行号=#${item.n}、框渲染 >4px`,
            own.length === 1 && own[0].row === Number(item.n) && own[0].rect.w > 4 && own[0].rect.h > 4,
            JSON.stringify(own));
    } else {
        check(`#${item.n} 有帧无框 → 零 own 框（不搬他人框）`, own.length === 0, JSON.stringify(own));
    }
}

console.log("5) 缩略锚点规格统一 + 三态诚实");
const realFramed = realRows.filter((row) => row.anchorSource !== "none");
check("[A] 有帧锚点全部走同一缩略缩放 0.5（不按行另造尺寸）",
    realFramed.length > 0 && realFramed.every((row) => row.frameScale === "0.5"),
    realFramed.map((row) => `${row.n}:${row.frameScale}`).join(" "));
check("[A] 帧舞台渲染出真实尺寸（非 0×0）",
    realFramed.every((row) => row.stageRect && row.stageRect.w > 20 && row.stageRect.h > 40),
    JSON.stringify(realFramed.slice(0, 4).map((row) => `${row.n}:${row.stageRect && `${row.stageRect.w}x${row.stageRect.h}`}`)));
check("[A] 真实数据无伪造占位（所有行都有可用帧 → 占位 0 处）",
    realRows.every((row) => !row.placeholder), realRows.filter((row) => row.placeholder).map((row) => `#${row.n}`).join(" "));
const keepRows = realRows.filter((row) => row.tone === "keep");
check("[A] 可沿用行（折叠区）展开后同样挂着可见锚点",
    keepRows.length > 0 && keepRows.every((row) => row.images.length > 0 && row.images.every((img) => img.naturalWidth > 0)),
    keepRows.map((row) => `#${row.n}:${row.images.map((img) => img.naturalWidth).join("/")}`).join(" "));
check("[A] 缩略图说明文字按锚点列宽排版（不被缩成一条竖排）",
    realFramed.every((row) => row.captionRect && row.captionRect.w > 120),
    JSON.stringify(realFramed.slice(0, 4).map((row) => `${row.n}:${row.captionRect && row.captionRect.w}`)));
const noOwn = realCards.find((card) => !card.组 && !card.自身框 && frameUrlOf(card));
if (noOwn) {
    const row = realRowsByN.get(noOwn.n);
    check(`[A] #${noOwn.n}（有帧无框）明说「未能标注位置」`, Boolean(row && row.text.includes("未能标注位置")), row && row.text.slice(0, 60));
}
await shot("C:/Users/leon3/AppData/Local/hermes/cache/scratch/at16-b3-real-anchors.png");

// ── [B] 组成员 + 缺帧：真实快照派生夹具（只换响应，不动真实数据/文件）
console.log("6) 组成员 + 缺帧（真实快照派生夹具：页面内替换 GET 响应；真实任务数据未动）");
const fixture = JSON.parse(JSON.stringify(realTask));
const fixCards = (fixture.asset_cards || []).map((card) => ({ ...card, n: Number(card.n) }));
const fixByN = new Map(fixCards.map((card) => [card.n, card]));
fixture.asset_cards = fixCards;
const groupMain = fixByN.get(1);
const groupMember = fixByN.get(5);
const noFrameCard = fixByN.get(2);

if (groupMain && groupMember && noFrameCard) {
    const ownBox = (groupMain.帧内框 || []).find((box) => box.本卡) || (groupMain.帧内框 || [])[0] || null;
    const group = {
        键: "1-5",
        成员: [1, 5],
        主行: 1,
        组名: "同件道具（夹具）",
        代表帧: groupMain.代表帧,
        单框: ownBox,
        单框降级: !ownBox,
        镜次并集: [2],
    };
    groupMain.组 = group;
    groupMember.组 = group;
    groupMain.同实体确认 = [1, 5];
    groupMember.同实体确认 = [1, 5];
    noFrameCard.代表帧 = { url: "", 秒: 0, 镜次: null, 定位: "未定位" };
    noFrameCard.帧 = [];
    noFrameCard.帧内框 = [];
    noFrameCard.自身框 = false;
    fixture.state = "awaiting_assets";

    await page.evaluate((task) => {
        if (!window.__egoOrigFetch) window.__egoOrigFetch = window.fetch.bind(window);
        window.__egoFixture = task;
        window.fetch = async (input, init) => {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (window.__egoFixture && url.includes("/api/hub/task/")) {
                return new Response(JSON.stringify({ task: window.__egoFixture }), {
                    status: 200,
                    headers: { "content-type": "application/json" },
                });
            }
            return window.__egoOrigFetch(input, init);
        };
    }, fixture);

    // 重开详情 → 抽屉重新拉详情（打到夹具）
    await page.keyboard.press("Escape");
    await page.waitForTimeout(900);
    await page.click(`tr[data-row-key="${picked.localId}"]`, { label: "重开详情（夹具）" });
    await page.waitForSelector('[data-hub-suggest-anchor-source="group"]', { state: "visible", timeout: 20_000 });
    await page.waitForTimeout(1200);
    await page.evaluate(() => document.querySelectorAll(".hub-detail-drawer details").forEach((node) => { node.open = true; }));
    await page.waitForTimeout(500);

    const fixRows = await page.evaluate(collectRows);
    const fixRowsByN = new Map(fixRows.map((row) => [row.n, row]));
    const r1 = fixRowsByN.get(1);
    const r5 = fixRowsByN.get(5);
    const r2 = fixRowsByN.get(2);
    const ownBoxes = (row) => (row ? row.boxes.filter((box) => box.own === "1") : []);

    check("[B] 组成员行 #1 走 GroupFrame（组卡单帧单框）",
        Boolean(r1) && r1.anchorSource === "group" && r1.isGroupFrame && r1.groupKey === "1-5",
        r1 && JSON.stringify({ src: r1.anchorSource, group: r1.groupKey, isGroup: r1.isGroupFrame }));
    check("[B] 组成员行 #5 走同一组锚点（同组键同图，不按成员行号另画）",
        Boolean(r5) && r5.anchorSource === "group" && r5.groupKey === "1-5"
        && Boolean(r5.images[0] && r1 && r5.images[0].src === r1.images[0].src),
        r5 && JSON.stringify({ group: r5.groupKey, src: r5.images.map((img) => img.src.slice(0, 40)) }));
    check("[B] 组卡恰一枚框且=组主行 #1（成员行 #5 的框行号也是 #1）",
        ownBoxes(r1).length === 1 && ownBoxes(r1)[0].row === 1
        && ownBoxes(r5).length === 1 && ownBoxes(r5)[0].row === 1,
        JSON.stringify({ r1: ownBoxes(r1), r5: ownBoxes(r5) }));
    check("[B] 组成员锚点图真的可见（naturalWidth>0 且渲染 >20px）",
        Boolean(r1 && r5) && [...r1.images, ...r5.images].every((img) => img.naturalWidth > 0 && img.rect && img.rect.w > 20),
        JSON.stringify([...(r1 ? r1.images : []), ...(r5 ? r5.images : [])].map((img) => ({ nw: img.naturalWidth, rect: img.rect }))));
    check("[B] 缺帧行 #2 诚实占位（零 img、无帧舞台、写明无对应帧）",
        Boolean(r2) && r2.anchorSource === "none" && r2.placeholder && r2.images.length === 0 && !r2.frameStage && r2.text.includes("无对应帧"),
        r2 && JSON.stringify({ src: r2.anchorSource, img: r2.images.length, stage: r2.frameStage, text: r2.text.slice(0, 40) }));
    check("[B] 夹具下其余行仍与数据真相一致（锚点来源/编号映射不串行）",
        fixRows.every((row) => {
            const card = fixByN.get(row.n);
            if (!card) return true;
            const want = card.组 ? "group" : frameUrlOf(card) ? "frame" : "none";
            return row.anchorSource === want && row.text.includes(`#${row.n}`);
        }),
        fixRows.map((row) => `${row.n}:${row.anchorSource}`).join(" "));
    await shot("C:/Users/leon3/AppData/Local/hermes/cache/scratch/at16-b3-group-noframe.png");
} else {
    check("[B] 真实快照里能派生组/缺帧夹具（需要 #1 #5 #2 三行）", false, fixCards.map((card) => card.n).join(","));
}

console.log(`\nego-at16-b3-anchor: ${result.pass} PASS, ${result.fails.length} FAIL`);
for (const name of result.fails) console.log(`  FAIL ${name}`);
process.exit(result.fails.length ? 1 : 0);
