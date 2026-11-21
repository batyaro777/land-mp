/* Загрузка файлов перетягиванием (Тони, 09.11.2026).
   Каждый <input type="file"> на странице превращается в зону: перетащить файлы (или папку —
   берутся файлы из неё), нажать и выбрать, вставить из буфера (Ctrl+V). Список выбранных файлов
   с размером и кнопкой «убрать»; у поля с одним файлом новый файл заменяет прежний, у поля с
   multiple — добавляется. Фильтр по accept: лишние файлы не берём и говорим почему.
   Сам <input> остаётся в форме (прозрачный поверх зоны), поэтому required и обычная отправка
   формы работают как раньше; без JS зона просто не появляется. Повторная разметка после HTMX
   (htmx:load) — для блоков, которые перерисовываются по частям (ГПЗУ в отчёте). */
(function () {
  const MAX_LIST = 30;

  function fmtSize(n) {
    if (n < 1024) return n + ' Б';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' КБ';
    return (n / 1024 / 1024).toFixed(1) + ' МБ';
  }

  function accepts(input, file) {
    const accept = (input.getAttribute('accept') || '').toLowerCase().trim();
    if (!accept) return true;
    const name = (file.name || '').toLowerCase();
    const type = (file.type || '').toLowerCase();
    return accept.split(',').some(function (a) {
      a = a.trim();
      if (!a) return false;
      if (a.startsWith('.')) return name.endsWith(a);
      if (a.endsWith('/*')) return type.startsWith(a.slice(0, -1));
      return type === a;
    });
  }

  function setFiles(input, files) {
    const dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function render(zone, input, rejected) {
    const list = zone.querySelector('.dz-files');
    const note = zone.querySelector('.dz-note');
    const files = Array.from(input.files || []);
    list.innerHTML = '';
    files.slice(0, MAX_LIST).forEach(function (f, i) {
      const li = document.createElement('li');
      const name = document.createElement('span');
      name.textContent = f.name + ' ';
      const size = document.createElement('small');
      size.className = 'muted';
      size.textContent = fmtSize(f.size);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'dz-rm';
      rm.title = 'Убрать файл';
      rm.setAttribute('aria-label', 'Убрать ' + f.name);
      rm.textContent = '×';
      rm.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        setFiles(input, files.filter(function (_, j) { return j !== i; }));
        render(zone, input, []);
      });
      li.appendChild(name);
      li.appendChild(size);
      li.appendChild(rm);
      list.appendChild(li);
    });
    if (files.length > MAX_LIST) {
      const li = document.createElement('li');
      li.className = 'muted';
      li.textContent = '… и ещё ' + (files.length - MAX_LIST);
      list.appendChild(li);
    }
    zone.classList.toggle('has-files', files.length > 0);
    const hint = zone.querySelector('.dz-hint');
    if (hint) {
      hint.textContent = files.length
        ? (input.multiple ? 'Выбрано файлов: ' + files.length + '. Можно добавить ещё — перетащите или нажмите.' : 'Файл выбран. Чтобы заменить — перетащите или нажмите.')
        : zone.dataset.dzText;
    }
    note.textContent = rejected && rejected.length
      ? 'Не приняты (не тот тип файла): ' + rejected.slice(0, 5).join(', ') + (rejected.length > 5 ? ' и ещё ' + (rejected.length - 5) : '')
      : '';
  }

  function addFiles(zone, input, incoming) {
    const ok = [], rejected = [];
    Array.from(incoming).forEach(function (f) {
      if (!f || (!f.size && !f.name)) return;
      (accepts(input, f) ? ok : rejected).push(f);
    });
    if (!ok.length && !rejected.length) return;
    let files;
    if (input.multiple) {
      const have = Array.from(input.files || []);
      const seen = new Set(have.map(function (f) { return f.name + ':' + f.size; }));
      files = have.concat(ok.filter(function (f) { return !seen.has(f.name + ':' + f.size); }));
    } else {
      files = ok.slice(0, 1).length ? ok.slice(0, 1) : Array.from(input.files || []);
    }
    setFiles(input, files);
    render(zone, input, rejected.map(function (f) { return f.name; }));
  }

  // Файлы из DataTransfer: папка (webkitGetAsEntry) раскрывается на один уровень вложенности.
  function filesFromTransfer(dt, done) {
    const items = dt.items ? Array.from(dt.items) : [];
    const entries = items.map(function (it) { return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; });
    if (!entries.some(function (e) { return e && e.isDirectory; })) {
      done(Array.from(dt.files || []));
      return;
    }
    const out = [];
    let pending = 0;
    function finish() { if (--pending === 0) done(out); }
    function readDir(dir, depth) {
      pending++;
      const reader = dir.createReader();
      const all = [];
      (function more() {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            all.forEach(function (e) {
              if (e.isFile) { pending++; e.file(function (f) { out.push(f); finish(); }, finish); }
              else if (e.isDirectory && depth < 2) readDir(e, depth + 1);
            });
            finish();
            return;
          }
          all.push.apply(all, batch);
          more();
        }, finish);
      })();
    }
    pending++;
    entries.forEach(function (e, i) {
      if (!e) { const f = items[i].getAsFile && items[i].getAsFile(); if (f) out.push(f); }
      else if (e.isFile) { pending++; e.file(function (f) { out.push(f); finish(); }, finish); }
      else if (e.isDirectory) readDir(e, 0);
    });
    finish();
  }

  function enhance(input) {
    if (input.dataset.dz === '1' || input.closest('.dropzone')) return;
    input.dataset.dz = '1';
    const zone = document.createElement('div');
    zone.className = 'dropzone';
    zone.dataset.dzText = input.dataset.dzText || (input.multiple
      ? 'Перетащите файлы сюда (можно несколько или папку), нажмите, чтобы выбрать, или вставьте из буфера'
      : 'Перетащите файл сюда или нажмите, чтобы выбрать');
    zone.innerHTML = '<div class="dz-hint"></div><ul class="dz-files"></ul><div class="dz-note err small"></div>';
    input.parentNode.insertBefore(zone, input);
    zone.insertBefore(input, zone.firstChild);
    input.classList.add('dz-input');
    input.title = zone.dataset.dzText;

    input.addEventListener('change', function (e) {
      if (e.isTrusted) render(zone, input, []); // выбор через диалог; программный change уже отрисован
    });
    ['dragenter', 'dragover'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.add('over'); });
    });
    ['dragleave', 'dragend'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.remove('over'); });
    });
    zone.addEventListener('drop', function (e) {
      e.preventDefault();
      e.stopPropagation();
      zone.classList.remove('over');
      if (!e.dataTransfer) return;
      filesFromTransfer(e.dataTransfer, function (files) { addFiles(zone, input, files); });
    });
    render(zone, input, []);
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll('input[type=file]').forEach(enhance);
  }

  // Файлы, брошенные мимо зоны: браузер не должен открывать их вместо страницы; если на странице
  // одна зона — файлы идут в неё.
  document.addEventListener('dragover', function (e) {
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') !== -1) e.preventDefault();
  });
  document.addEventListener('drop', function (e) {
    if (!e.dataTransfer || Array.from(e.dataTransfer.types || []).indexOf('Files') === -1) return;
    e.preventDefault();
    const zones = Array.from(document.querySelectorAll('.dropzone'));
    if (zones.length === 1) {
      const zone = zones[0];
      filesFromTransfer(e.dataTransfer, function (files) { addFiles(zone, zone.querySelector('input[type=file]'), files); });
    }
  });
  // Вставка из буфера (скриншот, скопированный файл): в зону под курсором/в фокусе, иначе в единственную.
  document.addEventListener('paste', function (e) {
    const cd = e.clipboardData;
    if (!cd || !cd.files || !cd.files.length) return;
    const active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || (active.tagName === 'INPUT' && active.type !== 'file'))) return;
    let zone = active && active.closest ? active.closest('.dropzone') : null;
    const zones = Array.from(document.querySelectorAll('.dropzone'));
    if (!zone && zones.length === 1) zone = zones[0];
    if (!zone) return;
    e.preventDefault();
    addFiles(zone, zone.querySelector('input[type=file]'), cd.files);
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { enhanceAll(); });
  else enhanceAll();
  document.addEventListener('htmx:load', function (e) { enhanceAll(e.target || document); });
})();
