const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('camAPI', {
  /** 保存一帧 PNG（dataURL）到磁盘，返回 { ok, filePath?, canceled? } */
  saveSnapshot: (dataUrl, suggestedName) =>
    ipcRenderer.invoke('snapshot:save', dataUrl, suggestedName),

  vision: {
    /** 启动 Python 侧车，返回是否成功（失败原因见 onLog） */
    start: () => ipcRenderer.invoke('vision:start'),
    stop: () => ipcRenderer.invoke('vision:stop'),
    /** 送一帧 JPEG（Uint8Array），拿这一帧的分析结果 */
    analyze: (jpegBytes) => ipcRenderer.invoke('vision:analyze', jpegBytes),
    onLog: (handler) => ipcRenderer.on('vision:log', (_event, line) => handler(line)),
  },

  // STM32 拼图机的串口控制台。command 走一条 JSON 命令，事件（状态/收发/掉线）走 onEvent
  host: {
    start: () => ipcRenderer.invoke('host:start'),
    stop: () => ipcRenderer.invoke('host:stop'),
    command: (payload) => ipcRenderer.invoke('host:command', payload),
    onEvent: (handler) => ipcRenderer.on('host:event', (_event, message) => handler(message)),
    onLog: (handler) => ipcRenderer.on('host:log', (_event, line) => handler(line)),
  },
})
