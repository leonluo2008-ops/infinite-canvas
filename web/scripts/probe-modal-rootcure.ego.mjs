// B3 探针 · 生成失败弹窗根治验收（方案 docs/方案-生成失败弹窗根治-20261114.md §五）
// 判定口径：可见 modal 计数（[role="dialog"] 过滤 getBoundingClientRect 宽高>0）；
// 稳定条件：连续 2 次采样（间隔 500ms）不变再断言；断言前跑，跑后不清理生产数据（独立 ego profile）。
const BASE = "http://localhost:3000";
const SHOT = "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-弹窗根治-20261114";
const results = [];
const check = (name, ok, detail = "") => {
    results.push({ name, ok });
    console.log((ok ? "PASS" : "FAIL") + " | " + name + (detail ? " | " + detail : ""));
};

const task = await taskSpace("b3-modal-probe");
const page = await task.newPage();
try { page.on("pageerror", (e) => console.log("PAGEERROR:", String(e).slice(0, 160))); } catch {}

await page.goto(BASE + "/canvas?mode=new", { timeout: 90000 });
// 等探针钩子就绪
let hookReady = false;
for (let i = 0; i < 60; i++) {
    hookReady = await page.evaluate(() => typeof window.__probeApplyOps === "function");
    if (hookReady) break;
    await page.waitForTimeout?.(500);
}
check("探针钩子 __probeApplyOps 就绪", hookReady);

const visibleModalCount = () => page.evaluate(() =>
    Array.from(document.querySelectorAll('[role="dialog"]')).filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }).length
);
const waitStable = async () => {
    let last = -1;
    for (let i = 0; i < 20; i++) {
        const c = await visibleModalCount();
        const nodeText = await page.evaluate(() => (document.body?.innerText || "").slice(0, 4000));
        const sig = c + "|" + (nodeText.includes("未配置渠道凭据") ? "err" : "noerr");
        if (sig === last) return c;
        last = sig;
        await page.waitForTimeout?.(500);
    }
    return last.split("|")[0] * 1;
};

// ---------- 路径①（agent op）×3 ----------
await page.evaluate(() => {
    window.__probeApplyOps([
        { type: "add_node", id: "probe-vid", nodeType: "video", title: "探针视频节点", position: { x: 0, y: 0 } },
    ]);
});
await page.waitForTimeout?.(800);
let failAny = false;
for (let round = 1; round <= 3; round++) {
    await page.evaluate(() => {
        window.__probeApplyOps([{ type: "run_generation", nodeId: "probe-vid", mode: "video", prompt: "探针测试" }]);
    });
    const modalCount = await waitStable();
    const bodyText = await page.evaluate(() => (document.body?.innerText || ""));
    check("路径① 第" + round + "次程序化失败：可见 modal = 0", modalCount === 0, "count=" + modalCount);
    if (modalCount !== 0) failAny = true;
    if (round === 1) {
        check("路径① 失败信息落节点（含模型与渠道指引）", bodyText.includes("未配置渠道凭据") && bodyText.includes("配置→渠道"), bodyText.includes("未配置渠道凭据") ? "命中" : "未命中");
        check("卡B 引导按钮渲染", bodyText.includes("去配置渠道"));
        await page.screenshot({ path: SHOT + "-路径1-失败态.png" });
    }
}

// ---------- 卡B：点「去配置渠道」→ 渠道页签激活 ----------
const clicked = await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll("button")).find((el) => (el.textContent || "").trim() === "去配置渠道" && el.getBoundingClientRect().width > 0);
    if (!btn) return false;
    btn.click();
    return true;
});
check("卡B 点击「去配置渠道」", clicked);
await page.waitForTimeout?.(1500);
const modalAfterBtn = await visibleModalCount();
check("卡B 点击后配置弹窗打开", modalAfterBtn >= 1, "count=" + modalAfterBtn);
const activeTab = await page.evaluate(() => {
    const tab = document.querySelector('[role="tab"][aria-selected="true"]');
    return tab ? (tab.textContent || "").trim() : "";
});
check("卡B 弹窗落在「渠道」页签", activeTab.includes("渠道"), "activeTab=" + activeTab);
await page.screenshot({ path: SHOT + "-卡B-渠道页签.png" });
// 关闭弹窗
await page.evaluate(() => { const c = document.querySelector(".ant-modal-close"); if (c) c.click(); });
await page.waitForTimeout?.(800);

// ---------- 对照组：用户路径（重试）仍弹窗 ----------
const retryClicked = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll("button")).filter((el) => el.getBoundingClientRect().width > 0 && /重试|retry/i.test(el.textContent || ""));
    if (!btns.length) return false;
    btns[btns.length - 1].click();
    return true;
});
if (retryClicked) {
    await page.waitForTimeout?.(1500);
    const userModal = await visibleModalCount();
    check("对照组 用户路径（重试）弹窗 = 1", userModal === 1, "count=" + userModal);
    if (userModal !== 1) failAny = true;
    await page.screenshot({ path: SHOT + "-对照组-用户路径弹窗.png" });
    await page.evaluate(() => { const c = document.querySelector(".ant-modal-close"); if (c) c.click(); });
} else {
    check("对照组 用户路径（重试）弹窗 = 1", false, "未找到重试按钮，需人工复验");
    failAny = true;
}

// ---------- i18n 英文复验 ----------
await page.evaluate(() => localStorage.setItem("infinite-canvas:locale", "en-US"));
await page.reload({ timeout: 90000 });
let hookEn = false;
for (let i = 0; i < 60; i++) {
    hookEn = await page.evaluate(() => typeof window.__probeApplyOps === "function");
    if (hookEn) break;
    await page.waitForTimeout?.(500);
}
if (hookEn) {
    await page.evaluate(() => {
        window.__probeApplyOps([
            { type: "add_node", id: "probe-vid-en", nodeType: "video", title: "probe video node", position: { x: 200, y: 0 } },
        ]);
    });
    await page.waitForTimeout?.(800);
    await page.evaluate(() => {
        window.__probeApplyOps([{ type: "run_generation", nodeId: "probe-vid-en", mode: "video", prompt: "probe test" }]);
    });
    const enModal = await waitStable();
    const enText = await page.evaluate(() => (document.body?.innerText || ""));
    check("i18n=en：程序化失败 modal = 0", enModal === 0, "count=" + enModal);
    check("i18n=en：英文错误文案命中", enText.includes("has no channel credentials"), enText.includes("has no channel credentials") ? "命中" : "未命中");
    if (enModal !== 0) failAny = true;
    await page.screenshot({ path: SHOT + "-i18n-en-失败态.png" });
} else {
    check("i18n=en 复验", false, "钩子未就绪");
    failAny = true;
}

const pass = results.filter((r) => r.ok).length;
console.log("===SUMMARY=== " + pass + "/" + results.length + " PASS" + (failAny ? "（存在 FAIL）" : ""));
console.log("EXIT_CODE=" + (failAny ? 1 : 0));
