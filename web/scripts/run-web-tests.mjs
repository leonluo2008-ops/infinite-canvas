/**
 * 跑 web/tests/*.test.ts（仓库里是 `bun:test` 风格；本机没有 bun）。
 *
 * 用 esbuild 打包测试文件，把 `bun:test` alias 到下面这个最小实现（test/expect/afterAll），
 * 然后在 node 里按顺序执行并汇总结果：
 *   node scripts/run-web-tests.mjs            # 全部
 *   node scripts/run-web-tests.mjs model      # 按文件名过滤
 *
 * 只实现仓库测试实际用到的匹配器（toBe/toBeCloseTo/toBeGreaterThan(OrEqual)/toBeLessThan(OrEqual)。
 * 不实现 bun 的其它能力，也不要在这里加业务断言——它只是本机跑既有测试的替代入口。
 */
import { mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const filter = process.argv[2] || "";

async function loadEsbuild() {
    try {
        return await import("esbuild");
    } catch {
        const store = path.join(root, "node_modules", ".pnpm");
        const dir = readdirSync(store).find((name) => /^esbuild@/.test(name));
        if (!dir) throw new Error("esbuild 不可用：请在 web/ 下执行 pnpm install");
        return import(pathToFileURL(path.join(store, dir, "node_modules", "esbuild", "lib", "main.js")).href);
    }
}
const esbuild = await loadEsbuild();

const workdir = mkdtempSync(path.join(tmpdir(), "run-web-tests-"));
const shimFile = path.join(workdir, "bun-test-shim.mjs");
writeFileSync(
    shimFile,
    `const registry = [];
export function test(name, fn) { registry.push({ name, fn }); }
export const it = test;
export function describe(name, fn) { fn(); }
export function afterAll(fn) { registry.push({ __afterAll: true, fn }); }
function fail(message) { throw new Error(message); }
export function expect(actual) {
    const api = {
        toBe: (expected) => { if (!Object.is(actual, expected)) fail(\`期望 \${JSON.stringify(expected)}，实际 \${JSON.stringify(actual)}\`); },
        toEqual: (expected) => { if (JSON.stringify(actual) !== JSON.stringify(expected)) fail(\`期望 \${JSON.stringify(expected)}，实际 \${JSON.stringify(actual)}\`); },
        toBeCloseTo: (expected, digits = 2) => { const tol = Math.pow(10, -digits) / 2; if (Math.abs(actual - expected) > tol) fail(\`期望 ≈\${expected}，实际 \${actual}\`); },
        toBeGreaterThan: (expected) => { if (!(actual > expected)) fail(\`期望 > \${expected}，实际 \${actual}\`); },
        toBeGreaterThanOrEqual: (expected) => { if (!(actual >= expected)) fail(\`期望 >= \${expected}，实际 \${actual}\`); },
        toBeLessThan: (expected) => { if (!(actual < expected)) fail(\`期望 < \${expected}，实际 \${actual}\`); },
        toBeLessThanOrEqual: (expected) => { if (!(actual <= expected)) fail(\`期望 <= \${expected}，实际 \${actual}\`); },
    };
    api.not = { toBe: (expected) => { if (Object.is(actual, expected)) fail(\`期望不等于 \${JSON.stringify(expected)}\`); } };
    return api;
}
export function runAll() { return registry; }
`,
);

const files = readdirSync(path.join(root, "tests"))
    .filter((name) => name.endsWith(".test.ts") && name.includes(filter))
    .sort();

let passed = 0;
const failures = [];
for (const file of files) {
    const entry = `export { runAll as __runAll } from "bun:test";\nimport ${JSON.stringify(path.join(root, "tests", file).replace(/\\/g, "/"))};\n`;
    const entryFile = path.join(workdir, `${file}.entry.ts`);
    writeFileSync(entryFile, entry);
    const built = await esbuild.build({
        entryPoints: [entryFile],
        bundle: true,
        format: "esm",
        platform: "browser",
        write: false,
        absWorkingDir: path.join(root, "tests"),
        alias: { "bun:test": shimFile, "@": path.join(root, "src") },
        logLevel: "silent",
    });
    const outFile = path.join(workdir, `${file}.mjs`);
    writeFileSync(outFile, built.outputFiles[0].text);
    const mod = await import(pathToFileURL(outFile).href);
    for (const item of mod.__runAll()) {
        if (item.__afterAll) continue;
        try {
            await item.fn();
            passed += 1;
            console.log(`PASS | ${file} :: ${item.name}`);
        } catch (error) {
            failures.push(`${file} :: ${item.name} —— ${error instanceof Error ? error.message : String(error)}`);
            console.log(`FAIL | ${file} :: ${item.name}`);
        }
    }
}

console.log(`=== web tests：${passed} passed / ${failures.length} failed（${files.length} 个文件，bun:test 兼容垫片）===`);
for (const item of failures) console.log(`FAIL | ${item}`);
process.exit(failures.length ? 1 : 0);
