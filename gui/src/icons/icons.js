// 图标单一真源（规范 §4.4）：全部内联 SVG，24px 画布 / 16px 视觉、1.5px 描边、currentColor。
// 禁止 emoji 与字符字形（⤡ 📌 ─ ✕ 一律不用）——它们随字体变化，也不跟主题色。
// vector-effect="non-scaling-stroke" 保证缩到 16px 时描边仍是 1.5px 而不是变细。
//
// 形制铁律（§4.4）：小窗/大窗 = **单外框 + 框内箭头**；叠框是「还原」的语义，
// 把箭头塞进叠框会让「小窗」与「还原」一眼分不清（上一版就是这个问题）。

const ATTRS =
  'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';

const svg = (body) => `<svg class="icon" ${ATTRS}>${body}</svg>`;

// 小窗/大窗共用的单外框
const FRAME = '<rect x="3" y="4" width="18" height="16" rx="2"/>';

export const ICONS = {
  // 小窗：单外框 + 框内指向右下的箭头
  compact: svg(
    `${FRAME}<path d="M11.5 11.5l5 5" vector-effect="non-scaling-stroke"/><path d="M16.5 12v4.5H12" vector-effect="non-scaling-stroke"/>`,
  ),
  // 大窗（小窗→正常）：单外框 + 框内指向左上的箭头
  expand: svg(
    `${FRAME}<path d="M17 17l-5-5" vector-effect="non-scaling-stroke"/><path d="M12 16.5V12h4.5" vector-effect="non-scaling-stroke"/>`,
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
