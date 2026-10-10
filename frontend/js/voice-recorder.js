/* CivicMind — 15-second voice recorder that produces 16 kHz mono 16-bit PCM WAV.
   WAV is used because the server can verify its exact duration from the header, and the
   speech-to-text provider accepts it directly. The 15 s cap is enforced here AND on the server.
   Microphone access is requested only when the user presses Record. */
const VoiceRecorder = (() => {
  const MAX_SECONDS = 15, TARGET_RATE = 16000;

  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
    v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return new Blob([buf], { type: 'audio/wav' });
  }

  function downsample(data, from, to) {
    if (from === to) return data;
    const ratio = from / to, out = new Float32Array(Math.floor(data.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio), end = Math.min(data.length, Math.floor((i + 1) * ratio));
      let sum = 0; for (let j = start; j < end; j++) sum += data[j];
      out[i] = sum / Math.max(1, end - start);
    }
    return out;
  }

  /** start({ onTick(seconds), onStop(blob, seconds) }) -> controller with stop(). Rejects if mic is unavailable. */
  async function start({ onTick, onStop }) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('Recording is not supported in this browser.');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const source = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = []; let total = 0, stopped = false;
    const maxSamples = Math.floor(MAX_SECONDS * ctx.sampleRate);
    const t0 = performance.now();
    const timer = setInterval(() => onTick && onTick(Math.min(MAX_SECONDS, (performance.now() - t0) / 1000)), 100);
    proc.onaudioprocess = (e) => {
      if (stopped) return;
      const input = e.inputBuffer.getChannelData(0);
      const take = Math.min(input.length, maxSamples - total);
      chunks.push(new Float32Array(input.subarray(0, take))); total += take;
      if (total >= maxSamples) stop();   // automatic stop at 15 seconds
    };
    source.connect(proc); proc.connect(ctx.destination);
    function stop() {
      if (stopped) return; stopped = true; clearInterval(timer);
      proc.disconnect(); source.disconnect(); stream.getTracks().forEach(t => t.stop());
      const all = new Float32Array(total); let off = 0;
      chunks.forEach(c => { all.set(c, off); off += c.length; });
      const rate = ctx.sampleRate; ctx.close();
      const pcm = downsample(all, rate, TARGET_RATE);
      onStop(encodeWav(pcm, TARGET_RATE), pcm.length / TARGET_RATE);
    }
    return { stop };
  }

  const toDataURL = (blob) => new Promise((resolve, reject) => {
    const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(new Error('Could not read the recording.')); r.readAsDataURL(blob);
  });

  return { start, toDataURL, MAX_SECONDS };
})();
