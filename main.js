const { app, BrowserWindow, session, shell } = require('electron')
const path = require('node:path')
const { VisionClient } = require('./lib/vision-client')
const { HostClient } = require('./lib/host-client')
const { registerIpcHandlers } = require('./lib/ipc')

const vision = new VisionClient()
const host = new HostClient()

let mainWindow = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 720,
    minWidth: 640,
    minHeight: 480,
    backgroundColor: '#111317',
    show: false,
    // 摄像头预览用不上默认菜单，隐起来；按 Alt 仍可唤出（需要 DevTools 时有用）
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())
  mainWindow.on('closed', () => {
    mainWindow = null
  })

  // 外部链接交给系统浏览器，不在应用内打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))
}

app.whenReady().then(() => {
  registerIpcHandlers({ vision, host, getWindow: () => mainWindow })

  // 摄像头需要媒体权限；本应用内容全部来自本地文件，直接放行
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
    callback(permission === 'media' || permission === 'videoCapture')
  })
  session.defaultSession.setPermissionCheckHandler((_wc, permission) => {
    return permission === 'media' || permission === 'videoCapture'
  })

  // 侧车的日志转发到界面，出问题时不至于只能看终端
  vision.on('log', (line) => {
    console.log('[vision]', line)
    mainWindow?.webContents.send('vision:log', line)
  })

  // 串口桥接：日志打到终端。转发给界面的那部分在 registerIpcHandlers 里，
  // 测试注册同一份接线时才能一并验到
  host.on('log', (line) => console.log('[host]', line))

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  vision.stop()
  host.stop()
})
