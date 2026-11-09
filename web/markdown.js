/* ════════════════════════════════════════════════════════════════
   markdown.js · 零依赖 markdown → HTML
   - 先转义、再格式化：任何用户文本都不会以原始 HTML 进入 DOM
   - 流式容错：未闭合的 ``` 按「进行中的代码块」渲染，不丢内容
   - 按块 memo：同一段落重复渲染（流式每帧）不重建 HTML 字符串
   导出：renderMarkdown(text) -> string
         renderInto(el, text, {streaming}) -> void  ← 复用已渲染块的 DOM
   ════════════════════════════════════════════════════════════════ */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** HTML 实体转义（唯一入口，任何用户文本都必须先过这里） */
export function escapeHtml(input) {
  return String(input == null ? '' : input).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

const SAFE_SCHEME = /^(https?:|mailto:|\/|\.{0,2}\/|#)/i;

/** 只放行 http(s)/mailto/相对路径，挡掉 javascript: 与 data: */
function safeUrl(raw) {
  const url = String(raw).replace(/&amp;/g, '&').trim();
  if (!url) return '';
  if (/^[a-z][a-z0-9+.-]*:/i.test(url) && !SAFE_SCHEME.test(url)) return '';
  if (/[\u0000-\u001f"']/.test(url)) return '';
  return escapeHtml(url);
}

/* ─────────────────────────── 行内 ─────────────────────────── */

function inline(src) {
  const stash = [];
  const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
  let t = escapeHtml(src);

  // 行内代码先抽出来，避免内部被后续规则改写
  t = t.replace(/`([^`\n]+)`/g, (_m, code) => keep(`<code>${code}</code>`));

  // 链接 [文本](url)
  t = t.replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, (m, label, url) => {
    const href = safeUrl(url);
    return href ? keep(`<a href="${href}" target="_blank" rel="noreferrer noopener">${label}</a>`) : m;
  });

  t = t.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');
  // 斜体：左右需是边界，避免把 a*b*c 里的标识符拆开
  t = t.replace(/(^|[\s(（[，。、:；!?])\*(?![\s*])([^*\n]+?)\*(?=$|[\s)）\]，。、:；!?.])/g, '$1<em>$2</em>');
  // 裸 URL 自动链接
  t = t.replace(/(^|[\s(（])(https?:\/\/[^\s<>\u0000]+[^\s<>\u0000.,;:!?)\]])/g,
    (_m, pre, url) => `${pre}${keep(`<a href="${safeUrl(url)}" target="_blank" rel="noreferrer noopener">${url}</a>`)}`);

  return t.replace(/\u0000(\d+)\u0000/g, (_m, i) => stash[Number(i)]);
}

/* ─────────────────────────── 块级 ─────────────────────────── */

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})(.*)$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*)$/;
const HR_RE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE_RE = /^\s{0,3}>\s?/;
const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

function renderCodeBlock(info, body, open) {
  const lang = escapeHtml((info || '').split(/\s+/)[0] || 'text');
  return `<div class="codeblock${open ? ' is-open-stream' : ''}">` +
    `<div class="codeblock__head"><span class="codeblock__lang">${lang}</span>` +
    `<button class="codeblock__copy" type="button" data-copy-code title="复制代码" aria-label="复制代码">复制</button></div>` +
    `<pre><code>${escapeHtml(body)}</code></pre></div>`;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function renderTable(head, sep, rows) {
  const aligns = splitRow(sep).map((c) => {
    const left = c.startsWith(':');
    const right = c.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return '';
  });
  const th = splitRow(head).map((c, i) => {
    const a = aligns[i] ? ` style="text-align:${aligns[i]}"` : '';
    return `<th${a}>${inline(c)}</th>`;
  }).join('');
  const tb = rows.map((r) => {
    const cells = splitRow(r);
    return '<tr>' + cells.map((c, i) => {
      const a = aligns[i] ? ` style="text-align:${aligns[i]}"` : '';
      return `<td${a}>${inline(c)}</td>`;
    }).join('') + '</tr>';
  }).join('');
  return `<div class="tblwrap"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`;
}

/** 缩进栈构建嵌套列表 */
function renderList(items) {
  let html = '';
  const stack = [];
  const item = (text) => inline(text).replace(/\n/g, '<br>');
  for (const it of items) {
    const tag = it.ordered ? 'ol' : 'ul';
    // 1) 退到正确层级：弹出所有缩进更深的列表
    while (stack.length && stack[stack.length - 1].indent > it.indent) {
      html += `</li></${stack.pop().tag}>`;
    }
    const top = stack[stack.length - 1];
    if (top && top.indent === it.indent && top.tag !== tag) {
      // 2) 同级换类型（- 切到 1.）：关闭当前列表，另起一个
      html += `</li></${stack.pop().tag}>`;
      stack.push({ indent: it.indent, tag });
      html += `<${tag}><li>${item(it.text)}`;
    } else if (!top || it.indent > top.indent) {
      // 3) 进入更深层
      stack.push({ indent: it.indent, tag });
      html += `<${tag}><li>${item(it.text)}`;
    } else {
      // 4) 同级同类型：兄弟项
      html += `</li><li>${item(it.text)}`;
    }
  }
  while (stack.length) html += `</li></${stack.pop().tag}>`;
  return html;
}

/**
 * 块解析：返回 [{src, html}]，src 用于 memo 与 DOM 复用
 * 未闭合围栏 / 未闭合表格视为「进行中」，照样渲染。
 */
export function parseBlocks(text) {
  const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
  const lines = src.split('\n');
  const out = [];
  let i = 0;

  const push = (raw, html) => out.push({ src: raw, html });

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    /* 围栏代码块（含未闭合） */
    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1][0];
      const info = fence[2].trim();
      const body = [];
      const startLine = i;
      i++;
      let closed = false;
      while (i < lines.length) {
        const l = lines[i];
        if (new RegExp('^\\s{0,3}' + marker + '{3,}\\s*$').test(l)) { closed = true; i++; break; }
        body.push(l);
        i++;
      }
      push(lines.slice(startLine, i).join('\n'), renderCodeBlock(info, body.join('\n'), !closed));
      continue;
    }

    /* 标题 #1-4（5/6 级就近降为 h4） */
    const head = line.match(HEADING_RE);
    if (head) {
      const level = Math.min(head[1].length, 4);
      push(line, `<h${level}>${inline(head[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    /* 分割线 */
    if (HR_RE.test(line)) { push(line, '<hr>'); i++; continue; }

    /* 引用 */
    if (QUOTE_RE.test(line)) {
      const start = i;
      const inner = [];
      const startsBlock = (l) => FENCE_RE.test(l) || HEADING_RE.test(l) || HR_RE.test(l) || LIST_RE.test(l);
      while (i < lines.length) {
        const l = lines[i];
        if (QUOTE_RE.test(l)) { inner.push(l.replace(QUOTE_RE, '')); i++; continue; }
        // 惰性续行：非空、非块起点才并入引用
        if (l.trim() && inner.length && !startsBlock(l)) { inner.push(l); i++; continue; }
        break;
      }
      const paras = inner.join('\n').split(/\n{2,}/)
        .map((p) => p.trim()).filter(Boolean)
        .map((p) => `<p>${inline(p).replace(/\n/g, '<br>')}</p>`).join('');
      push(lines.slice(start, i).join('\n'), `<blockquote>${paras}</blockquote>`);
      continue;
    }

    /* 表格：本行 + 分隔行 */
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const start = i;
      const headRow = line;
      const sep = lines[i + 1];
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(lines[i]); i++; }
      push(lines.slice(start, i).join('\n'), renderTable(headRow, sep, rows));
      continue;
    }

    /* 列表 */
    if (LIST_RE.test(line)) {
      const start = i;
      const items = [];
      while (i < lines.length) {
        const l = lines[i];
        const m = l.match(LIST_RE);
        if (m) {
          items.push({
            indent: m[1].replace(/\t/g, '    ').length,
            ordered: /\d/.test(m[2]),
            text: m[3],
          });
          i++;
          continue;
        }
        if (!l.trim()) {
          // 空行后若仍是列表项则继续，否则结束
          const nxt = lines[i + 1];
          if (nxt && LIST_RE.test(nxt)) { i++; continue; }
          break;
        }
        if (/^\s{2,}\S/.test(l) && items.length) {
          items[items.length - 1].text += '\n' + l.trim();
          i++;
          continue;
        }
        break;
      }
      push(lines.slice(start, i).join('\n'), renderList(items));
      continue;
    }

    /* 段落：吃到空行或下一个块起点 */
    const start = i;
    const para = [];
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) break;
      if (para.length && (FENCE_RE.test(l) || HEADING_RE.test(l) || HR_RE.test(l) || QUOTE_RE.test(l) || LIST_RE.test(l))) break;
      if (para.length && l.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) break;
      para.push(l);
      i++;
    }
    push(lines.slice(start, i).join('\n'), `<p>${inline(para.join('\n')).replace(/\n/g, '<br>')}</p>`);
  }

  return out;
}

/* ─────────────────────────── memo + 渲染 ─────────────────────────── */

const HTML_CACHE = new Map();
const CACHE_MAX = 800;

function htmlForBlock(block) {
  const hit = HTML_CACHE.get(block.src);
  if (hit !== undefined) return hit;
  if (HTML_CACHE.size > CACHE_MAX) HTML_CACHE.clear();
  HTML_CACHE.set(block.src, block.html);
  return block.html;
}

/** 纯字符串接口：text -> html（块级结果走 memo，不重复拼串） */
export function renderMarkdown(text) {
  return parseBlocks(text).map(htmlForBlock).join('');
}

/**
 * 增量渲染到容器：已渲染且 src 未变的块保留原 DOM（流式每帧调用也不重建）。
 * 安全说明：html 全部来自本模块的转义产物，才允许写入 innerHTML。
 */
export function renderInto(el, text, opts = {}) {
  const blocks = parseBlocks(text);
  const children = el.children;
  let keep = 0;
  while (keep < children.length && keep < blocks.length) {
    const node = children[keep];
    if (node.__mdSrc === blocks[keep].src) keep++;
    else break;
  }
  while (el.childNodes.length > keep) el.removeChild(el.lastChild);

  for (let i = keep; i < blocks.length; i++) {
    const tpl = document.createElement('template');
    tpl.innerHTML = htmlForBlock(blocks[i]);
    const node = tpl.content.firstElementChild;
    if (!node) continue;
    node.__mdSrc = blocks[i].src;
    el.appendChild(node);
  }

  if (!blocks.length && opts.streaming) {
    const blank = document.createElement('p');
    blank.className = 'md-blank';
    blank.__mdSrc = '\u0000blank';
    el.appendChild(blank);
  }

  el.classList.toggle('is-streaming', !!opts.streaming);
  return blocks.length;
}

/** 用于「复制纯文本」等场景：去掉标记后的大致原文（不做完整 AST） */
export function toPlainText(text) {
  return String(text == null ? '' : text).replace(/[*_~`>#]/g, '');
}
