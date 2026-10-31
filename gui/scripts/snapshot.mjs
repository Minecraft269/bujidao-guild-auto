// 快照采集：用本机 Edge 的无头模式渲染 gui/src，出 4 组基准图（深浅 × 两套标题栏）。
// 由 scripts/gui_snapshot.py 调用；也可单独跑：
//   node gui/scripts/snapshot.mjs <baseURL> <outDir>
import { mkdirSync } from "node:fs";
import { chromium } from "playwright-core";

// 浏览器路径由 EDGE_PATH 提供（不写死任何本机/系统路径）
const EDGE = process.env.EDGE_PATH;
if (!EDGE) {
  console.error("[snapshot] 缺少 EDGE_PATH（浏览器可执行文件路径）");
  process.exit(2);
}
const base = process.argv[2];
const outDir = process.argv[3] || "snapshots";

if (!base) {
  console.error("用法: node snapshot.mjs <baseURL> [outDir]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

const combos = [
  ["dark", "custom"],
  ["dark", "native"],
  ["light", "custom"],
  ["light", "native"],
];

const browser = await chromium.launch({ executablePath: EDGE, headless: true });
try {
  for (const [theme, titlebar] of combos) {
    const page = await browser.newPage({ viewport: { width: 1000, height: 680 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base, { waitUntil: "load" });
    await page.evaluate(
      ([t, tb]) => {
        document.documentElement.dataset.theme = t;
        document.body.dataset.titlebar = tb;
      },
      [theme, titlebar],
    );
    // 静态预览路径会立刻渲染完；留一点时间给字体与布局稳定
    await page.waitForTimeout(250);
    await page.screenshot({ path: `${outDir}/${theme}-${titlebar}.png` });
    if (errors.length) {
      console.error(`[snapshot] ${theme}-${titlebar} 页面报错: ${errors.join(" | ")}`);
      process.exitCode = 1;
    }
    await page.close();
  }
} finally {
  await browser.close();
}
console.log("[snapshot] 4 组已采集");
