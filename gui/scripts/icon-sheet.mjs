// SVG 图标自检图（§4.4）：按真实 16px 渲染，再用 4× 采样截图，便于在 PR 里看清。
// 小窗/大窗用参考图遮罩（位图），其对比图由 scripts/icon_mask_compare.py 生成
// —— 浏览器在 setContent 页面里会拦 file:// 位图（实测两次空白），所以不在这里做。
// 浏览器路径必须由 EDGE_PATH 提供，不写死任何本机/系统路径。
// 用法：EDGE_PATH=<浏览器可执行文件> node gui/scripts/icon-sheet.mjs
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

import { ICONS } from "../src/icons/icons.js";

const EDGE = process.env.EDGE_PATH;
if (!EDGE) {
  console.error("[icon-sheet] 缺少 EDGE_PATH（浏览器可执行文件路径）");
  process.exit(2);
}

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, "../../docs/images");

const cells = [
  ["maximize", "最大化 icon-maximize"],
  ["restore", "还原 icon-restore"],
  ["pin", "已置顶 icon-pin-filled"],
  ["pinOff", "未置顶 icon-pin-outline"],
  ["minimize", "最小化 icon-minimize"],
  ["close", "关闭 icon-close"],
]
  .map(
    ([key, label]) =>
      `<figure><div class="box">${ICONS[key]}</div><figcaption>${label}</figcaption></figure>`,
  )
  .join("");

const html = `<!doctype html><meta charset="utf-8"><style>
  body{margin:0;background:#1E1F22;color:#E6E6E6;font:12px system-ui;padding:12px}
  .row{display:flex;gap:20px}
  figure{margin:0;text-align:center}
  .box{display:grid;place-items:center;width:32px;height:32px;background:#2B2D31;
       border-radius:6px;margin:0 auto 6px}
  .icon{width:16px;height:16px;color:#E6E6E6}
  figcaption{color:#9AA0A6;font-size:11px}
</style><div class="row">${cells}</div>`;

mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ executablePath: EDGE, headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 560, height: 90 },
    deviceScaleFactor: 4, // 图标本体仍是 16px，只是截图按 4x 采样
  });
  await page.setContent(html, { waitUntil: "load" });
  await page.waitForTimeout(150);
  await page.screenshot({ path: resolve(outDir, "icon-16px-check.png") });
} finally {
  await browser.close();
}
console.log(`[icon-sheet] 已生成 ${outDir}/icon-16px-check.png`);
