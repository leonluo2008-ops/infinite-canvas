/**
 * 探针专用桩：把「设置弹窗」整块替换成 null。
 *
 * 原因：app-top-nav → AppConfigModal → model-script-editor → @codemirror，
 * 后者在模块求值期就做 DOM 特性探测（需要真 document/view），node 侧无法满足。
 * 设置弹窗本身与「Agent 入口门控」无关（本探针单独渲染 ConfigLocalProxy 来断言本地代理 tab），
 * 所以只在打包时替换这一个模块，不触碰任何被测源码。
 */
export function AppConfigModal() {
    return null;
}
