/*! Atelier widget. One script tag puts the assistant on any website:
 *
 *   <script src="https://YOUR-HOST/widget.js" data-brand="casa-atelier" async></script>
 *
 * Optional attributes:  data-position="left"   (default: right)
 * From your own button: AtelierWidget.open()  /  .close()  /  .toggle()
 */
(function () {
  'use strict';
  if (window.AtelierWidget) return;

  var script = document.currentScript || (function () { var s = document.getElementsByTagName('script'); return s[s.length - 1]; })();
  var base;
  try { base = new URL(script.src, location.href).origin; } catch (e) { return; }
  var brand = (script.getAttribute('data-brand') || '').toLowerCase().replace(/[^a-z0-9-]/g, '');
  if (!brand) { console.warn('Atelier widget: add data-brand="your-brand" to the script tag.'); return; }
  var side = script.getAttribute('data-position') === 'left' ? 'left' : 'right';

  var accent = '#b4501f', accentInk = '#fff6ec', label = 'Chat with us', teaser = '';
  var isOpen = false, frame = null, teaseTimer = null, tease, btn, panel, wrap, loader;

  function lum(hex) {
    hex = String(hex).replace('#', '');
    if (hex.length === 3) hex = hex.replace(/./g, function (c) { return c + c; });
    var n = parseInt(hex.slice(0, 6), 16);
    if (isNaN(n)) return 0.3;
    var f = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(n >> 16 & 255) + 0.7152 * f(n >> 8 & 255) + 0.0722 * f(n & 255);
  }

  var CSS = [
    ':host{all:initial}',
    '*{box-sizing:border-box}',
    '.root{--a:' + accent + ';--ai:' + accentInk + ';font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}',
    '.btn{position:fixed;bottom:20px;' + side + ':20px;width:60px;height:60px;border-radius:50%;border:0;padding:0;cursor:pointer;background:var(--a);color:var(--ai);display:grid;place-items:center;z-index:2147483000;',
    'box-shadow:0 14px 34px -10px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.2);transition:transform .25s cubic-bezier(.2,.9,.3,1.3),box-shadow .25s,opacity .2s}',
    '.btn:hover{transform:scale(1.07)}',
    '.btn:focus-visible{outline:3px solid var(--a);outline-offset:3px}',
    '.btn svg{grid-area:1/1;transition:transform .35s cubic-bezier(.2,.9,.3,1),opacity .2s}',
    '.btn .x{opacity:0;transform:rotate(-80deg) scale(.5)}',
    '.on .btn .chat{opacity:0;transform:rotate(80deg) scale(.5)}',
    '.on .btn .x{opacity:1;transform:none}',
    '.panel{position:fixed;bottom:92px;' + side + ':20px;width:392px;height:min(680px,calc(100vh - 116px));border-radius:22px;overflow:hidden;background:#f3ede3;z-index:2147483000;',
    'box-shadow:0 40px 80px -24px rgba(0,0,0,.5),0 0 0 1px rgba(0,0,0,.08);opacity:0;transform:translateY(16px) scale(.97);transform-origin:bottom ' + side + ';',
    'visibility:hidden;pointer-events:none;transition:opacity .25s,transform .3s cubic-bezier(.2,.9,.3,1),visibility 0s linear .3s}',
    '.on .panel{opacity:1;transform:none;visibility:visible;pointer-events:auto;transition-delay:0s}',
    'iframe{display:block;width:100%;height:100%;border:0;background:transparent}',
    '.load{position:absolute;inset:0;display:grid;place-items:center;background:#f3ede3;color:#7a6d5f;transition:opacity .3s}',
    '.load.gone{opacity:0;pointer-events:none}',
    '@media (prefers-color-scheme:dark){.panel,.load{background:#13100e}.load{color:#9a8c7b}}',
    '.tease{position:fixed;bottom:30px;' + side + ':92px;max-width:260px;padding:13px 40px 13px 16px;border-radius:18px;background:#fff;color:#241d17;z-index:2147483000;',
    'box-shadow:0 18px 40px -16px rgba(0,0,0,.45),0 0 0 1px rgba(0,0,0,.06);opacity:0;transform:translateY(8px);pointer-events:none;transition:opacity .3s,transform .3s}',
    '.tease.show{opacity:1;transform:none;pointer-events:auto}',
    '.tease .t{all:unset;cursor:pointer;display:block}',
    '.tease .c{all:unset;position:absolute;top:7px;right:9px;width:24px;height:24px;display:grid;place-items:center;border-radius:50%;cursor:pointer;color:#7a6d5f;font-size:18px;line-height:1}',
    '.tease .c:hover{background:rgba(0,0,0,.07)}',
    '.on .tease{display:none}',
    '@media (max-width:520px){.panel{inset:0;width:auto;height:auto;border-radius:0;transform:translateY(24px)}.on .btn{opacity:0;pointer-events:none}.tease{max-width:calc(100vw - 112px)}}',
    '@media (prefers-reduced-motion:reduce){.btn,.btn svg,.panel,.tease,.load{transition:none}}'
  ].join('');

  var ICON_CHAT = '<svg class="chat" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.5-4.6A8 8 0 1 1 21 12z"/></svg>';
  var ICON_X = '<svg class="x" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  function paint() {
    if (!btn) return;
    wrap.style.setProperty('--a', accent);
    wrap.style.setProperty('--ai', accentInk);
    btn.setAttribute('aria-label', label);
    panel.setAttribute('aria-label', label);
  }

  function setOpen(v) {
    if (v === isOpen) return;
    isOpen = v;
    wrap.classList.toggle('on', v);
    btn.setAttribute('aria-expanded', v ? 'true' : 'false');
    clearTimeout(teaseTimer); tease.classList.remove('show');
    if (v) {
      try { sessionStorage.setItem('atelier.teaser.' + brand, '1'); } catch (e) {}
      if (!frame) {
        frame = document.createElement('iframe');
        frame.src = base + '/c/' + brand + '?embed=1';
        frame.title = label;
        frame.setAttribute('allow', 'microphone; clipboard-write');
        frame.addEventListener('load', function () { loader.classList.add('gone'); });
        panel.insertBefore(frame, loader);
      }
      setTimeout(function () { try { frame.focus(); } catch (e) {} }, 320);
    } else {
      btn.focus();
    }
  }

  function mount() {
    var host = document.createElement('div');
    host.id = 'atelier-widget';
    var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;
    var style = document.createElement('style'); style.textContent = CSS; root.appendChild(style);

    wrap = document.createElement('div'); wrap.className = 'root';
    btn = document.createElement('button'); btn.type = 'button'; btn.className = 'btn';
    btn.setAttribute('aria-expanded', 'false'); btn.innerHTML = ICON_CHAT + ICON_X;
    panel = document.createElement('div'); panel.className = 'panel'; panel.setAttribute('role', 'dialog');
    loader = document.createElement('div'); loader.className = 'load'; loader.textContent = 'Loading…'; panel.appendChild(loader);
    tease = document.createElement('div'); tease.className = 'tease';
    var tt = document.createElement('button'); tt.type = 'button'; tt.className = 't';
    var tc = document.createElement('button'); tc.type = 'button'; tc.className = 'c'; tc.setAttribute('aria-label', 'Dismiss'); tc.textContent = '×';
    tease.append(tt, tc);
    wrap.append(panel, tease, btn); root.appendChild(wrap);
    document.body.appendChild(host);
    paint();

    btn.addEventListener('click', function () { setOpen(!isOpen); });
    tt.addEventListener('click', function () { setOpen(true); });
    tc.addEventListener('click', function () {
      tease.classList.remove('show');
      try { sessionStorage.setItem('atelier.teaser.' + brand, '1'); } catch (e) {}
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && isOpen) setOpen(false); });
    window.addEventListener('message', function (e) {
      if (e.origin !== base || !frame || e.source !== frame.contentWindow) return;
      if (e.data && e.data.type === 'atelier:close') setOpen(false);
    });

    fetch(base + '/api/brand?b=' + encodeURIComponent(brand), { mode: 'cors' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (b) {
        if (!b) return;
        var a = b.theme && b.theme.light && b.theme.light.accent;
        if (a && /^#[0-9a-fA-F]{3,8}$/.test(a)) { accent = a; accentInk = lum(a) > 0.5 ? '#1a110a' : '#fff6ec'; }
        if (b.name) label = 'Chat with ' + b.name;
        paint();
        if (b.teaser) {
          tt.textContent = b.teaser;
          var seen = false; try { seen = !!sessionStorage.getItem('atelier.teaser.' + brand); } catch (e) {}
          if (!seen) teaseTimer = setTimeout(function () { if (!isOpen) tease.classList.add('show'); }, 3500);
        }
      })
      .catch(function () { /* the plain launcher still works */ });
  }

  window.AtelierWidget = {
    open: function () { setOpen(true); },
    close: function () { setOpen(false); },
    toggle: function () { setOpen(!isOpen); }
  };

  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
