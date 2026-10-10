const { app, BrowserWindow, ipcMain, dialog } = require('electron')
const path = require('node:path')
const { writeSnapshot } = require('./snapshot')

/**
 * 注册渲染进程可用的全部 IPC 通道。
 *
 * 单独抽出来是为了让冒烟测试能注册**同一份**实现——测试里复制一遍接线的话，
 * 线上接线坏了测试照样绿。
 */
function registerIpcHandlers({ vision, getWindow }) {
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
}

module.exports = { registerIpcHandlers }
