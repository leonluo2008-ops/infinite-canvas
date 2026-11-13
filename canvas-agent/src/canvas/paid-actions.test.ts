import assert from "node:assert/strict";
import test from "node:test";

import { applyPaidDecision, collectPaidActions } from "./paid-actions.js";

const snapshot = {
    projectId: "canvas",
    nodes: [
        { id: "config-image", type: "config" as const, position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { generationMode: "image", composerContent: "参考图提示词", model: "seedream", count: 3 } },
        { id: "config-video", type: "config" as const, position: { x: 0, y: 0 }, width: 320, height: 200, metadata: { generationMode: "video", composerContent: "卖点演示", model: "wan-2.2" } },
    ],
    connections: [],
    selectedNodeIds: [],
    viewport: { x: 0, y: 0, k: 1 },
};

test("付费清单：run_generation 逐条列出节点/渠道/参数摘要/次数，estimate 恒为 null", () => {
    const actions = collectPaidActions("canvas_apply_ops", {
        ops: [
            { type: "update_node", id: "config-image", metadata: { prompt: "x" } },
            { type: "run_generation", nodeId: "config-image" },
            { type: "run_generation", nodeId: "config-video", mode: "video" },
        ],
    }, snapshot);
    assert.equal(actions.length, 2);
    assert.deepEqual(actions.map((action) => action.id), ["run_generation:1", "run_generation:2"]);
    assert.deepEqual(actions.map((action) => action.nodeId), ["config-image", "config-video"]);
    // 节点未带 mode 时按节点 metadata.generationMode 归一。
    assert.equal(actions[0].mode, "image");
    assert.equal(actions[1].mode, "video");
    assert.deepEqual(actions.map((action) => action.channel), ["seedream", "wan-2.2"]);
    assert.deepEqual(actions.map((action) => action.count), [3, 1]);
    assert.deepEqual(actions.map((action) => action.estimate), [null, null]);
});

test("付费清单：非付费写操作不进清单", () => {
    assert.deepEqual(collectPaidActions("canvas_apply_ops", { ops: [{ type: "add_node", nodeType: "text" }, { type: "connect_nodes", fromNodeId: "a", toNodeId: "b" }] }, snapshot), []);
    assert.deepEqual(collectPaidActions("canvas_create_text_node", { text: "无付费" }, snapshot), []);
    // 工作台工具 run=false 只是填参数，不触发生成 → 不算付费动作。
    assert.deepEqual(collectPaidActions("workbench_image_generate", { prompt: "参数预览", run: false }, snapshot), []);
});

test("付费清单：工作台生成默认 run=true 视为付费动作", () => {
    const actions = collectPaidActions("workbench_image_generate", { prompt: "主图", model: "seedream", count: 2 }, snapshot);
    assert.equal(actions.length, 1);
    assert.equal(actions[0].mode, "image");
    assert.equal(actions[0].channel, "seedream");
    assert.equal(actions[0].count, 2);
});

test("下发裁剪：只保留勾选的 run_generation，其余 ops 原样保留", () => {
    const input = {
        ops: [
            { type: "add_node", nodeType: "text", id: "text-1" },
            { type: "run_generation", nodeId: "config-image" },
            { type: "run_generation", nodeId: "config-video", mode: "video" },
        ],
    };
    const actions = collectPaidActions("canvas_apply_ops", input, snapshot);
    const decision = applyPaidDecision("canvas_apply_ops", input, actions, ["run_generation:1"]);
    const ops = (decision.dispatchInput as { ops: Array<Record<string, unknown>> }).ops;
    assert.deepEqual(ops.map((op) => op.type), ["add_node", "run_generation"]);
    assert.deepEqual(decision.approved.map((action) => action.nodeId), ["config-image"]);
    assert.deepEqual(decision.rejected.map((action) => action.nodeId), ["config-video"]);
});

test("下发裁剪：全部拒绝时不下发任何动作", () => {
    const input = { ops: [{ type: "run_generation", nodeId: "config-video", mode: "video" }] };
    const actions = collectPaidActions("canvas_apply_ops", input, snapshot);
    const decision = applyPaidDecision("canvas_apply_ops", input, actions, []);
    assert.equal(decision.dispatchName, null);
    assert.equal(decision.dispatchInput, null);
    assert.equal(decision.rejected.length, 1);
});

test("下发裁剪：清单外 id 不被放行", () => {
    const input = { ops: [{ type: "run_generation", nodeId: "config-video", mode: "video" }] };
    const actions = collectPaidActions("canvas_apply_ops", input, snapshot);
    const decision = applyPaidDecision("canvas_apply_ops", input, actions, ["run_generation:99", "not-an-action"]);
    assert.equal(decision.dispatchName, null);
});
