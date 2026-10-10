const { app, BrowserWindow, ipcMain, dialog } = require('electron')
const path = require('node:path')
const { writeSnapshot } = require('./snapshot')

/**
 * 注册渲染进程可用的全部 IPC 通道。
 *
 * 单独抽出来是为了让冒烟测试能注册**同一份**实现——测试里复制一遍接线的话，
 * 线上接线坏了测试照样绿。
 */
function registerIpcHandlers({ vision, host, getWindow }) {
  /** 渲染进程把 canvas 的 PNG dataURL 传过来，弹保存对话框后落盘 */
  ipcMain.handle('snapshot:save', async (_event, dataUrl, suggestedName) => {
    const win = BrowserWindow.getFocusedWindow() ?? getWindow()
    const { canceled, filePath } = await dialog.showSaveDialog(win, {
      title: '保存照片',
      defaultPath: path.join(app.getPath('pictures'), suggestedName),
      filters: [{ name: 'PNG 图片', extensions: ['png'] }],
    })
    if (canceled || !filePath) return { ok: false, canceled: true }

    try {
      const { bytes } = await writeSnapshot(dataUrl, filePath)
      return { ok: true, filePath, bytes }
    } catch (error) {
      return { ok: false, error: error.message }
    }
  })

  ipcMain.handle('vision:start', () => vision.start())

  ipcMain.handle('vision:stop', () => {
    vision.stop()
    return true
  })

  /** 送一帧 JPEG（Uint8Array）进去，返回这一帧的分析结果 */
  ipcMain.handle('vision:analyze', async (_event, jpeg) => {
    try {
      return { ok: true, result: await vision.analyze(Buffer.from(jpeg)) }
    } catch (error) {
      return { ok: false, error: error.message }
    }
  })

  // ── 串口控制台（STM32 拼图机）────────────────────────────────────
  if (host) {
    // 事件和日志的转发**放在这里**、而不是 main.js：这样冒烟测试/界面测试注册同一份接线时，
    // 走的也是真实路径，不会出现「测试自己复刻一遍转发逻辑，线上坏了照样绿」
    host.on('log', (line) => getWindow()?.webContents.send('host:log', line))
    host.on('event', (message) => getWindow()?.webContents.send('host:event', message))

    ipcMain.handle('host:start', () => host.start())

    ipcMain.handle('host:stop', () => {
      host.stop()
      return true
    })

    /** 一条命令，比如 {cmd:'open', target:'COM3'} / {cmd:'set', x:1000} */
    ipcMain.handle('host:command', async (_event, payload) => {
      try {
        return { ok: true, reply: await host.command(payload) }
      } catch (error) {
        return { ok: false, error: error.message }
      }
    })
  }
}

module.exports = { registerIpcHandlers }
