/**
 * Marquee selection - drag rectangle row selection (C6).
 *
 * One implementation serves every table (files, netdisk, groups) and is
 * parameterized by row key attribute + selection callbacks. Default drag
 * replaces the selection, Shift-drag appends; a live rectangle highlights
 * rows, the viewport edge auto-scrolls, and the click that ends a drag is
 * suppressed so rows are never toggled twice.
 *
 * @module features/marquee-select
 */

import {
  MARQUEE_THRESHOLD_PX,
  MARQUEE_EDGE_PX,
  MARQUEE_SCROLL_STEP,
  MARQUEE_CLICK_SUPPRESS_MS,
} from '../constants.js';

/**
 * Attach marquee selection to a table host.
 *
 * @param {HTMLElement} host - interaction host (.table-grid)
 * @param {Object} cfg
 * @param {string} [cfg.rowKeyAttr='key'] - dataset attribute holding row keys
 * @param {function} cfg.getSelection - () => string[] current keys
 * @param {function} cfg.setSelection - (keys: string[]) => void commit
 * @param {function} [cfg.canStart] - extra guard (event) => boolean
 * @returns {function} detach
 */
export function attachMarquee(host, cfg) {
  const rowKeyAttr = cfg.rowKeyAttr || 'key';
  if (!host || host.dataset.marqueeBound) return () => {};
  host.dataset.marqueeBound = '1';

  const overlay = document.createElement('div');
  overlay.className = 'marquee-box';
  overlay.hidden = true;
  document.body.appendChild(overlay);

  let drag = null;
  let suppressUntil = 0;

  const rowsOf = () => Array.from(host.querySelectorAll('tbody tr'))
    .filter((r) => r.dataset[rowKeyAttr]);

  function highlight(want) {
    for (const r of rowsOf()) {
      const on = want.has(r.dataset[rowKeyAttr]);
      const chk = r.querySelector('input[type="checkbox"]');
      if (chk) chk.checked = on;
      r.classList.toggle('selected', on);
    }
  }

  function onMouseDown(ev) {
    if (ev.button !== 0) return;
    if (ev.target.closest('input,button,a,select,textarea,thead,label')) return;
    if (cfg.canStart && !cfg.canStart(ev)) return;
    drag = {
      sx: ev.clientX,
      sy: ev.clientY,
      base: new Set(ev.shiftKey ? cfg.getSelection() : []),
      active: false,
      want: null,
    };
  }

  function onMouseMove(ev) {
    if (!drag) return;
    const dx = ev.clientX - drag.sx;
    const dy = ev.clientY - drag.sy;
    if (!drag.active) {
      if (Math.hypot(dx, dy) < MARQUEE_THRESHOLD_PX) return;
      drag.active = true;
      document.body.classList.add('marquee-active');
    }
    ev.preventDefault();

    const x0 = Math.min(drag.sx, ev.clientX);
    const x1 = Math.max(drag.sx, ev.clientX);
    const y0 = Math.min(drag.sy, ev.clientY);
    const y1 = Math.max(drag.sy, ev.clientY);
    overlay.hidden = false;
    overlay.style.left = `${x0}px`;
    overlay.style.top = `${y0}px`;
    overlay.style.width = `${x1 - x0}px`;
    overlay.style.height = `${y1 - y0}px`;

    const want = new Set(drag.base);
    for (const r of rowsOf()) {
      const rr = r.getBoundingClientRect();
      if (x0 < rr.right && x1 > rr.left && y0 < rr.bottom && y1 > rr.top) {
        want.add(r.dataset[rowKeyAttr]);
      }
    }
    drag.want = want;
    highlight(want);

    // Viewport-edge auto scroll keeps the drag usable on long pages.
    const vh = window.innerHeight;
    if (ev.clientY < MARQUEE_EDGE_PX) window.scrollBy(0, -MARQUEE_SCROLL_STEP);
    else if (ev.clientY > vh - MARQUEE_EDGE_PX) window.scrollBy(0, MARQUEE_SCROLL_STEP);
  }

  function onMouseUp() {
    if (!drag) return;
    if (drag.active) {
      suppressUntil = Date.now() + MARQUEE_CLICK_SUPPRESS_MS;
      cfg.setSelection(Array.from(drag.want || drag.base));
    }
    overlay.hidden = true;
    document.body.classList.remove('marquee-active');
    drag = null;
  }

  function onClickCapture(ev) {
    if (Date.now() < suppressUntil) {
      ev.stopPropagation();
      ev.preventDefault();
    }
  }

  host.addEventListener('mousedown', onMouseDown);
  window.addEventListener('mousemove', onMouseMove);
  window.addEventListener('mouseup', onMouseUp);
  host.addEventListener('click', onClickCapture, true);

  return function detach() {
    host.removeEventListener('mousedown', onMouseDown);
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup', onMouseUp);
    host.removeEventListener('click', onClickCapture, true);
    overlay.remove();
    delete host.dataset.marqueeBound;
  };
}

/**
 * Pure geometry helper (unit-testable): rectangle/row intersection.
 * @param {{left:number,right:number,top:number,bottom:number}} row
 * @param {{x0:number,x1:number,y0:number,y1:number}} rect
 * @returns {boolean}
 */
export function rectHitsRow(row, rect) {
  return rect.x0 < row.right && rect.x1 > row.left &&
    rect.y0 < row.bottom && rect.y1 > row.top;
}