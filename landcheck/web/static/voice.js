/* Голосовой ввод: кнопка .mic[data-target=<id поля>] пишет до 60 с, переводит запись в WAV
   16 кГц моно и отправляет на /asr; текст попадает в поле для правки перед отправкой формы. */
(function () {
  const MAX_SECONDS = 60;
  const supported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, 'data'); v.setUint32(40, samples.length * 2, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: 'audio/wav' });
  }

  async function toWav16k(blob) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const rate = 16000;
    const length = Math.ceil(decoded.duration * rate);
    const off = new OfflineAudioContext(1, length, rate);
    const src = off.createBufferSource();
    src.buffer = decoded; src.connect(off.destination); src.start();
    const rendered = await off.startRendering();
    ctx.close && ctx.close();
    return encodeWav(rendered.getChannelData(0), rate);
  }

  function setStatus(btn, text, busy) {
    const s = btn.parentElement.querySelector('.mic-status');
    if (s) { s.textContent = text || ''; }
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  async function record(btn) {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      setStatus(btn, 'Нет доступа к микрофону: разрешите его в браузере', false);
      return;
    }
    const rec = new MediaRecorder(stream);
    const chunks = [];
    rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    const started = Date.now();
    let timer = null;
    const stop = () => { if (rec.state !== 'inactive') rec.stop(); };
    rec.onstop = async () => {
      clearInterval(timer);
      stream.getTracks().forEach(t => t.stop());
      btn.classList.remove('recording');
      btn.textContent = btn.dataset.labelIdle || '🎙';
      btn.onclick = () => record(btn);
      setStatus(btn, 'Распознаю…', true);
      try {
        const raw = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const wav = await toWav16k(raw);
        const fd = new FormData();
        fd.append('audio', wav, 'voice.wav');
        const r = await fetch('/asr', { method: 'POST', body: fd });
        const data = await r.json();
        if (!r.ok || data.error) {
          setStatus(btn, 'Не распознано: ' + (data.error || r.status), false);
          return;
        }
        if (!data.text) { setStatus(btn, 'Речь не распознана, попробуйте ещё раз', false); return; }
        target.value = target.value.trim() ? target.value.trim() + ' ' + data.text : data.text;
        target.dispatchEvent(new Event('input', { bubbles: true }));
        setStatus(btn, 'Готово: проверьте текст и поправьте при необходимости', false);
      } catch (e) {
        setStatus(btn, 'Ошибка: ' + e.message, false);
      }
    };
    rec.start();
    btn.classList.add('recording');
    btn.textContent = btn.dataset.labelStop || '■ Стоп';
    btn.onclick = stop;
    timer = setInterval(() => {
      const sec = Math.round((Date.now() - started) / 1000);
      // aria-busy здесь нельзя: Pico отключает клики у занятых кнопок, а «Стоп» должен нажиматься.
      setStatus(btn, 'Запись ' + sec + ' с из ' + MAX_SECONDS + '… нажмите «Стоп», когда закончите', false);
      if (sec >= MAX_SECONDS) stop();
    }, 500);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('button.mic').forEach(btn => {
      if (!supported) { btn.closest('.mic-wrap') ? btn.closest('.mic-wrap').remove() : btn.remove(); return; }
      btn.dataset.labelIdle = btn.textContent;
      btn.onclick = () => record(btn);
    });
  });
})();
