const fs = require('node:fs/promises')

/** 只接受 canvas 导出的 PNG dataURL，其余一律拒绝，避免把任意内容当图片写盘 */
function decodeDataUrl(dataUrl) {
  const match = /^data:image\/png;base64,([A-Za-z0-9+/=]+)$/.exec(String(dataUrl))
  if (!match) throw new Error('不是合法的 PNG dataURL')
  return Buffer.from(match[1], 'base64')
}

async function writeSnapshot(dataUrl, filePath) {
  const bytes = decodeDataUrl(dataUrl)
  await fs.writeFile(filePath, bytes)
  return { filePath, bytes: bytes.length }
}

module.exports = { decodeDataUrl, writeSnapshot }
