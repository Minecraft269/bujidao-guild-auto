import { defineConfig } from "vitest/config";

// T7 前端门禁：只对纯逻辑（src/lib）计覆盖率，DOM 装配段不计入。
// 调用方：package.json 的 `npm test` / `npm run coverage`（vitest 自动读取本文件）。
export default defineConfig({
  test: {
    include: ["tests/**/*.test.js"],
    environment: "node",
    coverage: {
      provider: "v8",
      include: ["src/lib/**/*.js"],
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
});
