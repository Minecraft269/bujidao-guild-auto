// 图标单一真源（规范 §4.4）：全部内联 SVG，24px 画布 / 16px 视觉、1.5px 描边、currentColor。
// 禁止 emoji 与字符字形（⤡ 📌 ─ ✕ 一律不用）——它们随字体变化，也不跟主题色。
// vector-effect="non-scaling-stroke" 保证缩到 16px 时描边仍是 1.5px 而不是变细。

const ATTRS =
  'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';

const svg = (body) => `<svg class="icon" ${ATTRS}>${body}</svg>`;

export const ICONS = {
  // 小窗：双矩形 + 右下箭头
  compact: svg(
    '<rect x="3" y="3" width="12" height="9" rx="1.5"/><rect x="9" y="12" width="12" height="9" rx="1.5" vector-effect="non-scaling-stroke"/><path d="M15 12l3 3M18 15v-3M18 15h-3" vector-effect="non-scaling-stroke"/>',
  ),
  // 大窗：双矩形 + 左上箭头
  expand: svg(
    '<rect x="9" y="3" width="12" height="9" rx="1.5"/><rect x="3" y="12" width="12" height="9" rx="1.5" vector-effect="non-scaling-stroke"/><path d="M9 12l-3-3M6 9v3M6 9h3" vector-effect="non-scaling-stroke"/>',
  ),
  // 置顶（已置顶）：实心图钉
  pin: svg(
    '<path d="M9 3h6l-1 5 3 3v2h-4v6l-1 2-1-2v-6H6v-2l3-3-1-5z" fill="currentColor" vector-effect="non-scaling-stroke"/>',
  ),
  // 未置顶：描边图钉
  pinOff: svg(
    '<path d="M9 3h6l-1 5 3 3v2h-4v6l-1 2-1-2v-6H6v-2l3-3-1-5z" vector-effect="non-scaling-stroke"/>',
  ),
  minimize: svg('<path d="M5 12h14" vector-effect="non-scaling-stroke"/>'),
  close: svg('<path d="M6 6l12 12M18 6L6 18" vector-effect="non-scaling-stroke"/>'),
};
