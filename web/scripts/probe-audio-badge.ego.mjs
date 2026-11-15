// E2 验收探针：配置节点面板音频徽标（有声/无声 + 来源标注）
const task = await taskSpace("e2-badge-probe");
const page = await task.newPage();
await page.goto("http://localhost:3000/canvas?mode=new", { timeout: 90000 });
let hook = false;
for (let i = 0; i < 60; i++) { hook = await page.evaluate(() => typeof window.__probeApplyOps === "function"); if (hook) break; await page.waitForTimeout?.(500); }
if (!hook) { console.log("FAIL | 探针钩子就绪"); console.log("EXIT_CODE=1"); }
else {
    // 场景1：config 节点无 generateAudio → 继承全局
    await page.evaluate(() => window.__probeApplyOps([{ type: "add_node", id: "probe-cfg-1", nodeType: "config", title: "探针配置1", position: { x: 0, y: 0 }, metadata: { generationMode: "video", composerContent: "探针", prompt: "探针", status: "idle" } }]));
    await page.waitForTimeout?.(800);
    await page.evaluate(() => window.__probeApplyOps([{ type: "select_nodes", ids: ["probe-cfg-1"] }]));
    await page.waitForTimeout?.(1200);
    const t1 = await page.evaluate(() => (document.body?.innerText || ""));
    const s1 = t1.includes("有声") || t1.includes("无声");
    const g1 = t1.includes("继承全局");
    console.log("场景1 徽标出现(有声/无声): " + (s1 ? "PASS" : "FAIL") + " | 来源标注(继承全局): " + (g1 ? "PASS" : "FAIL"));
    // 场景2：metadata.generateAudio = "false" → 无声 + 节点指定
    await page.evaluate(() => window.__probeApplyOps([{ type: "add_node", id: "probe-cfg-2", nodeType: "config", title: "探针配置2", position: { x: 420, y: 0 }, metadata: { generationMode: "video", composerContent: "探针", prompt: "探针", status: "idle", generateAudio: "false" } }]));
    await page.waitForTimeout?.(800);
    await page.evaluate(() => window.__probeApplyOps([{ type: "select_nodes", ids: ["probe-cfg-2"] }]));
    await page.waitForTimeout?.(1200);
    const t2 = await page.evaluate(() => (document.body?.innerText || ""));
    const s2 = t2.includes("无声");
    const g2 = t2.includes("节点指定");
    console.log("场景2 无声徽标: " + (s2 ? "PASS" : "FAIL") + " | 来源标注(节点指定): " + (g2 ? "PASS" : "FAIL"));
    await page.screenshot({ path: "E:/User/GitHub/infinite-canvas/wanrepro/docs/证据-弹窗根治-20261114/E2-音频徽标.png" });
    const pass = (s1 && g1 && s2 && g2);
    console.log("===SUMMARY=== " + (pass ? "4/4 PASS" : "存在 FAIL"));
    console.log("EXIT_CODE=" + (pass ? 0 : 1));
}
