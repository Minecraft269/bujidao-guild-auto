// 图标单一真源（规范 §4.4）：全部内联 SVG，24px 画布 / 16px 视觉、1.5px 描边、currentColor。
// 禁止 emoji 与字符字形（⤡ 📌 ─ ✕ 一律不用）——它们随字体变化，也不跟主题色。
// vector-effect="non-scaling-stroke" 保证缩到 16px 时描边仍是 1.5px 而不是变细。
//
// 形制铁律（§4.4）：小窗/大窗 = **单外框 + 框内箭头**；叠框是「还原」的语义，
// 把箭头塞进叠框会让「小窗」与「还原」一眼分不清（上一版就是这个问题）。

const ATTRS =
  'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';

const svg = (body) => `<svg class="icon" ${ATTRS}>${body}</svg>`;

// 参考图结构（已按 10× 放大版核对）：大圆角外框（占左上约 3/4）
// + 右下角独立小方框 + 框内对角箭头。小窗/大窗只差箭头方向。
const FRAME = '<rect x="3" y="4" width="14" height="11" rx="2.5"/>';
const CORNER_BOX =
  '<rect x="15" y="15" width="6" height="6" rx="1.5" vector-effect="non-scaling-stroke"/>';

export const ICONS = {
  // 小窗：外框 + 右下小方框 + 框内指向右下的箭头
  compact: svg(
    `${FRAME}${CORNER_BOX}<path d="M10 9l3.5 3.5" vector-effect="non-scaling-stroke"/><path d="M13.5 12.5V9.5" vector-effect="non-scaling-stroke"/><path d="M13.5 12.5H10.5" vector-effect="non-scaling-stroke"/>`,
  ),
  // 大窗（小窗→正常）：同结构，仅箭头指向左上
  expand: svg(
    `${FRAME}${CORNER_BOX}<path d="M13.5 12.5L10 9" vector-effect="non-scaling-stroke"/><path d="M10 9v3" vector-effect="non-scaling-stroke"/><path d="M10 9h3" vector-effect="non-scaling-stroke"/>`,
  ),
  // 最大化：单个空心方框（无箭头）
  maximize: svg(
    '<rect x="4" y="5" width="16" height="14" rx="2" vector-effect="non-scaling-stroke"/>',
  ),
  // 还原（已最大化）：两个方框，后框右上、前框左下
  restore: svg(
    '<rect x="9" y="3" width="12" height="10" rx="2" vector-effect="non-scaling-stroke"/><rect x="3" y="11" width="12" height="10" rx="2" vector-effect="non-scaling-stroke"/>',
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
