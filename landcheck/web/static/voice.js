/* Голосовой ввод (бесплатный стек, 19.10.2026).
   1) Web Speech API браузера (Chrome, Edge, Safari): распознаёт сам, серверу ничего не стоит.
   2) Запасной путь: запись до 60 с → WAV 16 кГц → POST /asr (Groq Whisper / локальный whisper).
   Кнопка .mic[data-target=<id поля>]; текст попадает в поле для правки перед отправкой формы. */
(function () {
  const MAX_SECONDS = 60;
  const SILENCE_RMS = 0.005; // как на сервере: ниже — микрофон не слышит голос
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  const serverAsr = document.documentElement.dataset.asrServer === '1';

  function setStatus(btn, text, busy) {
    const s = btn.parentElement.querySelector('.mic-status');
    if (s) { s.textContent = text || ''; }
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  function putText(target, text) {
    target.value = target.value.trim() ? target.value.trim() + ' ' + text : text;
    target.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function idle(btn) {
    btn.classList.remove('recording');
    btn.textContent = btn.dataset.labelIdle || '🎙';
    btn.onclick = () => start(btn);
  }

  /* --- путь 1: распознавание в браузере --- */
  function recognizeInBrowser(btn, target) {
    const rec = new SR();
    rec.lang = 'ru-RU'; rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1;
    let finals = '', interim = '', failed = null, stoppedByUser = false;
    const started = Date.now();
    const timer = setInterval(() => {
      const sec = Math.round((Date.now() - started) / 1000);
      setStatus(btn, 'Слушаю ' + sec + ' с из ' + MAX_SECONDS + (interim ? '… ' + interim : '') + ' — нажмите «Стоп», когда закончите', false);
      if (sec >= MAX_SECONDS) { stoppedByUser = true; rec.stop(); }
    }, 500);
    rec.onresult = (e) => {
      interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finals += (finals ? ' ' : '') + t.trim(); else interim += t;
      }
    };
    rec.onerror = (e) => { failed = e.error || 'error'; };
    rec.onend = () => {
      clearInterval(timer);
      idle(btn);
      if (finals) { putText(target, finals); setStatus(btn, 'Готово: проверьте текст и поправьте при необходимости', false); return; }
      if (failed === 'not-allowed' || failed === 'service-not-allowed') { setStatus(btn, 'Нет доступа к микрофону: разрешите его в браузере', false); return; }
      // Сервис браузера недоступен (network, aborted) или не услышал — запасной путь через сервер.
      if (serverAsr && canRecord && !stoppedByUser) { record(btn, target); return; }
      setStatus(btn, failed === 'no-speech' || !failed ? 'Речь не распознана, попробуйте ещё раз' : 'Распознавание недоступно: ' + failed, false);
    };
    try { rec.start(); } catch (e) { clearInterval(timer); idle(btn); if (serverAsr && canRecord) record(btn, target); else setStatus(btn, 'Распознавание недоступно', false); return; }
    btn.classList.add('recording');
    btn.textContent = btn.dataset.labelStop || '■ Стоп';
    btn.onclick = () => { stoppedByUser = true; rec.stop(); };
  }

  /* --- путь 2: запись и сервер --- */
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
    const data = rendered.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    const rms = data.length ? Math.sqrt(sum / data.length) : 0;
    return { blob: encodeWav(data, rate), rms: rms, seconds: data.length / rate };
  }
  async function record(btn, target) {
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
      idle(btn);
      setStatus(btn, 'Распознаю…', true);
      try {
        const raw = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const wav = await toWav16k(raw);
        if (wav.seconds < 0.5 || wav.rms < SILENCE_RMS) {
          setStatus(btn, 'Тишина: микрофон не слышит голос. Проверьте устройство и говорите ближе', false);
          return;
        }
        const fd = new FormData();
        fd.append('audio', wav.blob, 'voice.wav');
        const r = await fetch('/asr', { method: 'POST', body: fd });
        const data = await r.json();
        if (!r.ok || data.error) { setStatus(btn, 'Не распознано: ' + (data.error || r.status), false); return; }
        if (data.silent) { setStatus(btn, 'Тишина: голос не слышен, попробуйте ещё раз', false); return; }
        if (!data.text) { setStatus(btn, 'Речь не распознана, попробуйте ещё раз', false); return; }
        putText(target, data.text);
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

  function start(btn) {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    if (SR && !btn.dataset.forceServer) recognizeInBrowser(btn, target); else record(btn, target);
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('button.mic').forEach(btn => {
      const usable = SR || (serverAsr && canRecord);
      if (!usable) { btn.closest('.mic-wrap') ? btn.closest('.mic-wrap').remove() : btn.remove(); return; }
      btn.dataset.labelIdle = btn.textContent;
      btn.onclick = () => start(btn);
    });
  });
})();
