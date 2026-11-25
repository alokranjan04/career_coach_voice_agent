// Captures the mic, resamples to 16 kHz mono PCM16 and posts 40 ms chunks
// (640 samples) to the main thread. Linear interpolation is enough for speech;
// the browser's own noise suppression runs before this.
const TARGET_RATE = 16000;
const CHUNK = 640;

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0;        // read position, in buffer coordinates (index 0 = previous sample)
    this.prev = 0;
    this.out = new Int16Array(CHUNK);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || ch.length === 0) return true;
    const buf = new Float32Array(ch.length + 1);
    buf[0] = this.prev;
    buf.set(ch, 1);
    while (this.pos < buf.length - 1) {
      const k = Math.floor(this.pos);
      const f = this.pos - k;
      const s = Math.max(-1, Math.min(1, buf[k] * (1 - f) + buf[k + 1] * f));
      this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === CHUNK) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(CHUNK);
        this.n = 0;
      }
      this.pos += this.step;
    }
    this.pos -= buf.length - 1;
    this.prev = ch[ch.length - 1];
    return true;
  }
}

registerProcessor("mic-capture", MicCapture);
