/**
 * 质检面板就地处理闭环 · 第 1 批真机探针（ego 真浏览器，**真实任务只读**）。
 *
 * 为什么必须真机：离线探针（probe-at23-a1.mjs）只证结构与状态矩阵，证不了「点下去真的就地展开」
 * 「正文真的滚过去并高亮 2 秒」「连点不出 Toast」——这些是交付验收里被点名的行为。
 *
 * 断言三段（对真实任务，全程零写请求；只点开/收起编辑器、只滚正文）：
 *  [A] 待审批态（auto-0068）：卡片上**不存在** `data-hub-selfcheck-locator`；动作位收敛成「改入口」
 *      （不出现只读定位按钮）；点 1 次「改这一行」→ 出现且只出现 1 个 `[data-hub-text-editor="cell"]`，
 *      URL / 任务状态 / 提示词三样一字不变（0 离页、0 状态变更）。
 *      **第 2 批（字符级常驻高亮）**：打开抽屉即见正文命中段的常驻 `<mark data-hub-prompt-hit="persistent">`
 *      （尺寸 > 0、底色与正文可区分、只包命中词、2.6 秒后仍在）；正文文本与 `prompt_md` 逐字一致
 *      （markdown 完整性）；收起/展开正文后高亮重新挂上且只有一份。
 *  [B] 连点 5 次：编辑器开/关交替、同时最多 1 份、全程 0 条 Toast；关闭再打开**不丢已输入的草稿**。
 *  [C] 已交付态（auto-0067）：改入口 disabled（起态门判据）+ tooltip 是真实原因（不是「退回置换」指路）；
 *      「看正文里的这一句」可点 → 点击把**已存在的**命中段滚进视口、2.4 秒后高亮仍在（常驻）、全程无 Toast。
 *
 * 跑法（git-bash）：cd web && ego-browser nodejs < scripts/ego-at23-a1.mjs
 */
const HUB_URL = "http://localhost:3000/hub";
const SHOT_DIR = "C:/Users/leon3/AppData/Local/hermes/cache/scratch";

const result = { pass: 0, fails: [] };
function check(name, cond, detail = "") {
    if (cond) {
        result.pass += 1;
        console.log(`  [PASS] ${name}${detail ? ` —— ${String(detail).slice(0, 240)}` : ""}`);
    } else {
        result.fails.push(name);
        console.log(`  [FAIL] ${name}${detail ? ` —— ${String(detail).slice(0, 500)}` : ""}`);
    }
}

/** 页面内取质检面板现状（返回值只含纯数据）。 */
function panelState() {
    const cards = [...document.querySelectorAll("[data-hub-selfcheck-card]")];
    const fix = cards.map((card) => card.querySelector("[data-hub-selfcheck-fix]")).filter(Boolean);
    const locate = cards.map((card) => card.querySelector("[data-hub-selfcheck-locate]")).filter(Boolean);
    const editor = [...document.querySelectorAll('[data-hub-text-editor="cell"]')];
    const body = document.querySelector("[data-hub-prompt-body]");
    const hit = document.querySelector("[data-hub-prompt-hit]");
    // 第 2 批：正文里的**常驻**高亮（mark）——全部按真实 DOM 断言，不看源码
    const marks = body ? [...body.querySelectorAll("mark[data-hub-prompt-hit]")] : [];
    const firstMark = marks[0] || null;
    const markStyle = firstMark ? getComputedStyle(firstMark) : null;
    const bodyStyle = body ? getComputedStyle(body.querySelector("p, li, div") || body) : null;
    const rect = (node) => {
        if (!node) return null;
        const r = node.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom) };
    };
    const markRect = rect(firstMark);
    return {
        url: location.href,
        cards: cards.length,
        locator: document.querySelectorAll("[data-hub-selfcheck-locator]").length,
        locatorText: [...document.querySelectorAll("[data-hub-selfcheck-card] button")].some((b) => (b.textContent || "").includes("去这一行")),
        fix: fix.length,
        fixDisabled: fix.map((b) => Boolean(b.disabled)),
        fixGate: fix.map((b) => b.getAttribute("data-hub-selfcheck-fix-gate") || ""),
        locate: locate.length,
        locateDisabled: locate.map((b) => Boolean(b.disabled)),
        editors: editor.length,
        toasts: document.querySelectorAll(".ant-message-notice").length,
        promptBody: Boolean(body),
        hit: Boolean(hit),
        hitText: hit ? (hit.textContent || "").slice(0, 60) : "",
        hitRect: markRect,
        hitInsideBody: Boolean(hit && body && body.contains(hit)),
        marks: marks.length,
        markText: marks.map((node) => (node.textContent || "").trim()),
        markState: marks.map((node) => node.getAttribute("data-hub-prompt-hit") || ""),
        markRect,
        markInsideBody: marks.length > 0 && Boolean(body && marks.every((node) => body.contains(node))),
        markNested: marks.some((node) => Boolean(node.querySelector("mark"))),
        markBg: markStyle ? markStyle.backgroundColor : "",
        markDeco: markStyle ? markStyle.textDecorationColor || markStyle.textDecorationLine : "",
        bodyBg: bodyStyle ? bodyStyle.backgroundColor : "",
        markInView: markRect ? markRect.top >= 0 && markRect.bottom <= window.innerHeight : false,
        bodyText: body ? (body.textContent || "") : "",
        inputValue: editor.length ? (editor[0].querySelector("[data-hub-desc-input]") || {}).value ?? "" : "",
    };
}

async function shot(name) {
    try {
        console.log(`[shot] ${await page.screenshot({ path: `${SHOT_DIR}/${name}.png` })}`);
    } catch (err) {
        console.log(`[shot] 跳过截图（${err && err.message}）`);
    }
}

const space = await taskSpace("质检面板就地处理闭环 第1批 真机验收");
const page = space.page("p1");
console.log(`[space] ${space.spaceId} page=${page.label}`);

await page.goto(HUB_URL, { timeout: 60_000 });
await page.waitForSelector("tr[data-row-key]", { state: "attached", timeout: 45_000 });

// ── 挑真实任务：待审批（可编辑）与已交付（不可编辑）各一，只读
const picked = await page.evaluate(async () => {
    const list = await (await fetch("/api/hub/tasks")).json();
    const all = (list.tasks || []).map((t) => `${t.local_id}:${t.state}`);
    const byState = (state, prefer) => {
        const rows = (list.tasks || []).filter((t) => t.state === state);
        return (rows.find((t) => t.local_id === prefer) || rows[0] || {}).local_id || "";
    };
    return {
        editable: byState("awaiting_approval", "auto-0068"),
        done: byState("done", "auto-0067"),
        all,
    };
});
check("[0] 找到待审批态的真实任务（自动优先 auto-0068）", Boolean(picked.editable), `tasks=${picked.all.join(" ")}`);
check("[0] 找到已交付态的真实任务（自动优先 auto-0067）", Boolean(picked.done), `done=${picked.done}`);
if (!picked.editable || !picked.done) {
    console.log(`\nego-at23-a1: ${result.pass} PASS, ${result.fails.length} FAIL`);
    process.exit(1);
}
console.log(`[0] 只读走查对象：待审批=${picked.editable}，已交付=${picked.done}`);

async function openTask(localId) {
    // 切任务一律重载 /hub（抽屉关闭按钮/遮罩在真实页面里可被弹层盖住，Esc 不一定被抽屉接住——
    // 用「重载再点行」这条最稳的路，且完全不碰任务数据）。
    await page.goto(HUB_URL, { timeout: 60_000 });
    await page.waitForSelector("tr[data-row-key]", { state: "attached", timeout: 45_000 });
    await page.click(`tr[data-row-key="${localId}"]`, { label: `打开 ${localId} 详情` });
    await page.waitForSelector("[data-hub-selfcheck-card]", { state: "attached", timeout: 45_000 });
    await page.waitForTimeout(1200);
    const section = await page.evaluate(() => {
        const node = document.querySelector("[data-hub-prompt-section]");
        if (node) node.scrollIntoView({ block: "start" });
        return Boolean(node);
    });
    await page.waitForTimeout(700);
    return section;
}

async function detailOf(localId) {
    return page.evaluate(
        async (id) => (await (await fetch(`/api/hub/task/${encodeURIComponent(id)}`)).json()).task || {},
        localId,
    );
}

async function clickByAttr(attr) {
    return page.evaluate((sel) => {
        const node = document.querySelector(sel);
        if (!node) return false;
        node.scrollIntoView({ block: "center" });
        node.click();
        return true;
    }, `[${attr}]`);
}

// ════════════════════════════════════════════════ [A] 待审批态：假动作已删 + 点 1 次就地展开
console.log("1) 待审批态：卡片上不存在 locator；点「改这一行」1 次就地展开，0 离页 0 状态变更");
await openTask(picked.editable);
const before = await detailOf(picked.editable);
const pre = await page.evaluate(panelState);
check("[A] 质检面板渲染出 finding 卡", pre.cards > 0, `cards=${pre.cards}`);
check("[A] 验收 1：卡片上不存在 data-hub-selfcheck-locator 元素（假动作已删）", pre.locator === 0, String(pre.locator));
check("[A] 卡片上不再有「去这一行」文字按钮", pre.locatorText === false);
check("[A] 动作位收敛：不可编辑态没有定位按钮的位置占用，可编辑态只有改入口",
    pre.fix > 0 && pre.locate === 0, `fix=${pre.fix} locate=${pre.locate}`);
check("[A] 待审批态改入口可点（起态门放行）",
    pre.fix > 0 && pre.fixDisabled.every((flag) => flag === false) && pre.fixGate.every((g) => g === "enabled"),
    JSON.stringify(pre.fixDisabled));
check("[A] 点之前没有编辑器、没有 Toast", pre.editors === 0 && pre.toasts === 0, `editors=${pre.editors} toasts=${pre.toasts}`);

// ── 第 2 批：字符级常驻高亮（验收 1/3）——打开抽屉即见、不消失、markdown 逐字未变
console.log("1b) 待审批态：正文命中段常驻高亮（打开即见、不自动消失、markdown 逐字未变）");
const norm = (text) => String(text || "").replace(/\s+/g, "");
check("[A] 验收 1：打开抽屉即可见正文里的常驻高亮（不等点击）",
    pre.marks >= 1 && pre.markInsideBody, `marks=${pre.marks}`);
check("[A] 验收 1：高亮只包命中的那几个字（命中词本身，句首不跟着上色）",
    pre.markText.length === 1 && pre.markText[0].includes("裹亮橙色标签款") && !pre.markText[0].includes("全程使用"),
    JSON.stringify(pre.markText));
check("[A] 验收 1：高亮真的可见（渲染尺寸 > 0）",
    Boolean(pre.markRect) && pre.markRect.w > 0 && pre.markRect.h > 0, JSON.stringify(pre.markRect));
check("[A] 验收 1：高亮样式与正文可区分（有底色、底色不等于正文底色、带下划线色）",
    Boolean(pre.markBg) && pre.markBg !== "rgba(0, 0, 0, 0)" && pre.markBg !== pre.bodyBg && Boolean(pre.markDeco),
    JSON.stringify({ markBg: pre.markBg, bodyBg: pre.bodyBg, deco: pre.markDeco }));
check("[A] 常驻高亮的锚点＝data-hub-prompt-hit=persistent（不是第 1 批的临时 1）",
    pre.markState.every((state) => state === "persistent"), JSON.stringify(pre.markState));
check("[A] 验收 3：markdown 完整性——正文文本与 prompt_md 逐字一致（mark 没吃掉/没重复任何字）",
    norm(pre.bodyText) === norm(before.prompt_md), `body=${norm(pre.bodyText).length} prompt=${norm(before.prompt_md).length}`);
check("[A] 验收 3：命中词在正文里只出现 1 次、mark 不嵌套（无重复文本节点）",
    pre.bodyText.split("裹亮橙色标签款").length - 1 === 1 && pre.markNested === false,
    `occurrences=${pre.bodyText.split("裹亮橙色标签款").length - 1}`);
check("[A] 验收 3：正文里没有 markdown 语法泄漏（** / ## / 反引号）",
    !/(\*\*|##|```)/.test(pre.bodyText), pre.bodyText.slice(0, 60));

await page.waitForTimeout(2600);
const stays = await page.evaluate(panelState);
check("[A] 验收 1：2.6 秒后高亮仍在（常驻，不自动消失）",
    stays.marks >= 1 && Boolean(stays.markRect) && stays.markRect.w > 0, `marks=${stays.marks}`);
check("[A] 常驻高亮不反复叠加：2.6 秒后仍是 1 个 mark",
    stays.marks === 1 && stays.markNested === false, `marks=${stays.marks}`);
await shot("at23-a1-editable-persistent");

// 收起 → 展开：正文重新渲染一遍，高亮要重新挂上（且只有一份）
await page.evaluate(() => document.querySelector("[data-hub-prompt-toggle]").click());
await page.waitForTimeout(450);
const collapsedState = await page.evaluate(panelState);
check("[A] 收起正文：高亮随正文容器一起离开 DOM（不留孤儿 mark）",
    collapsedState.promptBody === false && collapsedState.marks === 0, `marks=${collapsedState.marks}`);
await page.evaluate(() => document.querySelector("[data-hub-prompt-toggle]").click());
await page.waitForTimeout(600);
const reopened = await page.evaluate(panelState);
check("[A] 再展开：高亮重新挂上且只有一份（重渲染不叠 mark）",
    reopened.marks === 1 && reopened.markState.every((state) => state === "persistent")
    && norm(reopened.bodyText) === norm(before.prompt_md),
    `marks=${reopened.marks}`);

await clickByAttr("data-hub-selfcheck-fix");
await page.waitForTimeout(600);
const opened = await page.evaluate(panelState);
check("[A] 点 1 次「改这一行」→ 出现且只出现 1 个行内编辑器（data-hub-text-editor=cell）",
    opened.editors === 1, `editors=${opened.editors}`);
check("[A] 就地展开：页面 URL 不变（0 离页）", opened.url === pre.url, `${pre.url} → ${opened.url}`);
const afterOpen = await detailOf(picked.editable);
check("[A] 任务状态不变（0 状态变更）", afterOpen.state === before.state, `${before.state} → ${afterOpen.state}`);
check("[A] 提示词一字不变（只开编辑器不写盘）", (afterOpen.prompt_md || "") === (before.prompt_md || ""),
    `chars ${String((before.prompt_md || "").length)} → ${String((afterOpen.prompt_md || "").length)}`);
check("[A] 开编辑器不弹任何 Toast", opened.toasts === 0, String(opened.toasts));
check("[A] 打开行内编辑器后常驻高亮原样在位（正文没被重渲染踩掉）",
    opened.marks === 1 && norm(opened.bodyText) === norm(before.prompt_md), `marks=${opened.marks}`);
await shot("at23-a1-editable-open");

// ════════════════════════════════════════════════ [B] 连点 5 次 + 草稿不丢
console.log("2) 连点 5 次：开/关交替、最多 1 份编辑器、零 Toast；关→开不丢草稿");
{
    // 先打一段草稿（只在本页内存里，不点保存 = 零写请求）
    const typed = await page.evaluate(() => {
        const input = document.querySelector('[data-hub-text-editor="cell"] [data-hub-desc-input]');
        if (!input) return "";
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
        setter.call(input, "草稿暂存验证-不保存");
        input.dispatchEvent(new Event("input", { bubbles: true }));
        return input.value;
    });
    await page.waitForTimeout(300);
    check("[B] 已输入草稿（只在页面内存里）", typed.includes("草稿暂存验证"), typed.slice(0, 40));

    const seq = [];
    for (let i = 0; i < 4; i += 1) {
        await clickByAttr("data-hub-selfcheck-fix");
        await page.waitForTimeout(350);
        const state = await page.evaluate(panelState);
        seq.push({ editors: state.editors, toasts: state.toasts, input: state.inputValue.includes("草稿暂存验证") });
    }
    const counts = [opened.editors, ...seq.map((s) => s.editors)];
    const alternated = counts.every((n, i) => n === (i % 2 === 0 ? 1 : 0));
    check("[B] 连点 5 次（含首点）：编辑器开/关交替（1,0,1,0,1）", alternated, JSON.stringify(counts));
    check("[B] 任何一次点击都不出现第二份编辑器（行级互斥）", counts.every((n) => n <= 1), JSON.stringify(counts));
    check("[B] 全程 0 条 Toast（不再连点叠提示）",
        [opened.toasts, ...seq.map((s) => s.toasts)].every((n) => n === 0),
        JSON.stringify([opened.toasts, ...seq.map((s) => s.toasts)]));
    check("[B] 关闭再打开不丢已输入的草稿",
        seq[1] && seq[1].input === true, JSON.stringify(seq.map((s) => s.input)));

    // 收尾：显式「取消」清掉暂存与编辑器（不保存 → 零写请求）
    await clickByAttr("data-hub-desc-cancel");
    await page.waitForTimeout(500);
    const cleaned = await page.evaluate(panelState);
    check("[B] 取消后编辑器收起、草稿暂存清掉（再打开回到原值）", cleaned.editors === 0, `editors=${cleaned.editors}`);
    const afterAll = await detailOf(picked.editable);
    check("[B] 全程零写入：任务状态与提示词仍与最初一致",
        afterAll.state === before.state && (afterAll.prompt_md || "") === (before.prompt_md || ""));
}

// ════════════════════════════════════════════════ [C] 已交付态：置灰 + 真原因 + 只读定位
console.log("3) 已交付态：改入口置灰 + tooltip 真原因；「看正文里的这一句」滚到命中段（高亮常驻）");
await openTask(picked.done);
const doneBefore = await detailOf(picked.done);
const doneState = await page.evaluate(panelState);
check("[C] 已交付态自检仍有 finding（同一批红字）", doneState.cards > 0, `cards=${doneState.cards}`);
check("[C] 验收 3：改入口 disabled（起态门判据）",
    doneState.fix > 0 && doneState.fixDisabled.every(Boolean) && doneState.fixGate.every((g) => g === "disabled"),
    JSON.stringify({ disabled: doneState.fixDisabled, gate: doneState.fixGate }));
check("[C] 已交付态不出现 locator 假动作", doneState.locator === 0, String(doneState.locator));

// tooltip：悬停到置灰按钮上，读 portal 里的真实原因
await page.evaluate(() => {
    const node = document.querySelector("[data-hub-selfcheck-fix]");
    if (node) node.scrollIntoView({ block: "center" });
});
await page.waitForTimeout(300);
const fixBox = await page.evaluate(() => {
    const r = document.querySelector("[data-hub-selfcheck-fix]").getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
});
await page.mouse.move(fixBox.x, fixBox.y, { label: "悬停置灰的改这一行" });
await page.waitForTimeout(900);
const tip = await page.evaluate(() => {
    // antd 6 的浮层正文直接挂在 `.ant-tooltip` 里（没有 `.ant-tooltip-inner` 那层）——两处都收。
    const nodes = [...document.querySelectorAll(".ant-tooltip, .ant-tooltip-inner")]
        .map((node) => (node.textContent || "").trim())
        .filter((text) => text.length > 0);
    return [...new Set(nodes)];
});
check("[C] tooltip 给的是起态门的真实原因（已完成 / 不能再改描述）",
    tip.some((text) => text.includes("已完成") && text.includes("不能再改描述")), JSON.stringify(tip));
check("[C] tooltip 不再要求用户退回置换（旧假指引 0 命中）",
    !tip.some((text) => text.includes("退回置换")), JSON.stringify(tip));
check("[C] 「看正文里的这一句」在已交付态可用",
    doneState.locate > 0 && doneState.locateDisabled.every((flag) => flag === false),
    `locate=${doneState.locate} disabled=${JSON.stringify(doneState.locateDisabled)}`);
check("[C] 验收 2：点之前常驻高亮已在正文里（不再等点击才出现）",
    doneState.marks >= 1 && doneState.hit === true && doneState.hitInsideBody === true,
    `marks=${doneState.marks} hit=${doneState.hitText}`);
check("[C] 正文容器在场（所有状态恒渲染）", doneState.promptBody === true);
check("[C] 验收 3：已完成态正文同样逐字未变（mark 不破坏 markdown）",
    norm(doneState.bodyText) === norm(doneBefore.prompt_md) && doneState.markNested === false,
    `body=${norm(doneState.bodyText).length} prompt=${norm(doneBefore.prompt_md).length}`);

await clickByAttr("data-hub-selfcheck-locate");
await page.waitForTimeout(500);
const locating = await page.evaluate(panelState);
check("[C] 点「看正文里的这一句」→ 正文命中段已高亮（data-hub-prompt-hit，常驻）",
    locating.hit === true && locating.hitInsideBody === true && locating.marks >= 1, `hit=${locating.hitText}`);
check("[C] 高亮真的可见（渲染尺寸 > 0）",
    Boolean(locating.hitRect) && locating.hitRect.w > 0 && locating.hitRect.h > 0, JSON.stringify(locating.hitRect));
check("[C] 验收 2：点击后视口滚到该命中段（命中段进入视口）",
    locating.markInView === true, JSON.stringify(locating.markRect));
check("[C] 高亮落在命中的那句话上（含命中词）",
    locating.hitText.includes("裹亮橙色标签款"), locating.hitText);
check("[C] 定位全程 0 条 Toast（这条路径不再弹指路）", locating.toasts === 0, String(locating.toasts));
await shot("at23-a1-done-locate");

await page.waitForTimeout(2400);
const afterHit = await page.evaluate(panelState);
check("[C] 验收 2：2.4 秒后高亮仍在（常驻；第 1 批的「2 秒自动消失」已按升级合同作废）",
    afterHit.marks >= 1 && afterHit.hit === true, `marks=${afterHit.marks}`);
const doneAfter = await detailOf(picked.done);
check("[C] 已交付态只读：状态与提示词零变化",
    doneAfter.state === doneBefore.state && (doneAfter.prompt_md || "") === (doneBefore.prompt_md || ""));
check("[C] 全程 0 条 Toast", afterHit.toasts === 0, String(afterHit.toasts));

// ════════════════════════════════════════════════ [D] Toast 防抖（浏览器里跑同一份包装代码）
console.log("4) Toast 防抖：同文案 1s 内只出一条，>1s 可再弹（真机浏览器内、打生产包装函数）");
const dedupe = await page.evaluate(async () => {
    const mod = await import("/src/pages/hub/use-hub-message.ts");
    const calls = [];
    const fake = {
        info: (content) => { calls.push(String(content)); return () => undefined; },
        success: () => () => undefined,
        warning: () => () => undefined,
        error: () => () => undefined,
        loading: () => () => undefined,
        open: () => undefined,
        destroy: () => undefined,
    };
    const wrapped = mod.withToastDedupe(fake);
    for (let i = 0; i < 5; i += 1) wrapped.info("同一句提示");
    const burst = calls.length;
    await new Promise((resolve) => setTimeout(resolve, 1100));
    wrapped.info("同一句提示");
    const afterWait = calls.length;
    wrapped.info("另一句提示");
    return { burst, afterWait, total: calls.length, texts: calls.slice() };
});
check("[D] 验收 5：连点同一提示 5 次（100ms 间隔量级）→ 只有 1 条真的弹出去",
    dedupe.burst === 1, JSON.stringify(dedupe));
check("[D] 验收 5：>1s 再点 → 允许再弹 1 条", dedupe.afterWait === 2, String(dedupe.afterWait));
check("[D] 别的文案不受影响（只拦同一句）", dedupe.total === 3 && dedupe.texts[2] === "另一句提示", JSON.stringify(dedupe.texts));

console.log("");
console.log(`[ego-at23-a1] ${result.pass}/${result.pass + result.fails.length} PASS`);
if (result.fails.length) {
    console.log("FAIL: " + result.fails.join(" | "));
    process.exit(1);
}
process.exit(0);
