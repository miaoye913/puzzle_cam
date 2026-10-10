/**
 * Electron 启动器。
 *
 * 某些环境（如 VS Code 扩展宿主及其派生的终端）会带着 ELECTRON_RUN_AS_NODE=1，
 * 这会让 electron 退化成纯 Node 进程：不显示窗口，require('electron').app 也是 undefined。
 * 这里显式剔除该变量后再拉起 electron，避免那种「双击没反应」的困惑。
 *
 * 用法：node scripts/start.js [入口脚本]
 *   node scripts/start.js                 → 正常启动应用（main.js）
 *   node scripts/start.js scripts/smoke.js → 跑冒烟测试
 */
const { spawn } = require('node:child_process')
const electronPath = require('electron')

delete process.env.ELECTRON_RUN_AS_NODE

const target = process.argv[2] ?? '.'
const child = spawn(electronPath, [target], { stdio: 'inherit', env: process.env })

child.on('close', (code) => process.exit(code ?? 0))
child.on('error', (error) => {
  console.error(`无法启动 Electron: ${error.message}`)
  process.exit(1)
})
