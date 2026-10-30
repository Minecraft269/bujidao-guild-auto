// 16px 图标自检图（规范 §4.4 强制项）：把四个图标按**真实 16px** 渲染，
// 再用 4× 采样截图，方便在 PR 里一眼看出箭头朝哪。
// 用法：node gui/scripts/icon-sheet.mjs
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

import { ICONS } from "../src/icons/icons.js";

const EDGE =
  process.env.EDGE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../../docs/images/icon-16px-check.png");

const cells = [
  ["compact", "小窗 icon-compact"],
  ["expand", "大窗 icon-expand"],
  ["maximize", "最大化 icon-maximize"],
  ["restore", "还原 icon-restore"],
]
  .map(
    ([key, label]) =>
      `<figure><div class="box">${ICONS[key]}</div><figcaption>${label}</figcaption></figure>`,
  )
  .join("");

const html = `<!doctype html><meta charset="utf-8"><style>
  body{margin:0;background:#1E1F22;color:#E6E6E6;font:12px system-ui;padding:12px}
  .row{display:flex;gap:24px}
  figure{margin:0;text-align:center}
  .box{display:grid;place-items:center;width:32px;height:32px;background:#2B2D31;
       border-radius:6px;margin:0 auto 6px}
  .icon{width:16px;height:16px;color:#E6E6E6}
  figcaption{color:#9AA0A6;font-size:11px}
</style><div class="row">${cells}</div>`;

mkdirSync(dirname(out), { recursive: true });
const browser = await chromium.launch({ executablePath: EDGE, headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 420, height: 90 },
    deviceScaleFactor: 4, // 图标本体仍是 16px，只是截图按 4× 采样
  });
  await page.setContent(html, { waitUntil: "load" });
  await page.waitForTimeout(150);
  await page.screenshot({ path: out });
} finally {
  await browser.close();
}
console.log(`[icon-sheet] 已生成 ${out}（16px 实际渲染，4× 采样）`);
