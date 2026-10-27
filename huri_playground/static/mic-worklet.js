// Microphone -> HuRI's `audio.in`: 16 kHz mono int16 PCM in fixed-size frames
// (the VAD only accepts 10, 20 or 30 ms). Browsers record at 44.1 or 48 kHz,
// so samples are averaged down: each output sample is the mean of the input
// samples it spans, which also keeps the audio above 8 kHz from folding back.
"use strict";

class HuriMic extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { targetRate, frameSamples } = options.processorOptions;
    this.step = sampleRate / targetRate; // input samples per output sample
    this.acc = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(frameSamples);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (const sample of channel) {
      this.sum += sample;
      this.count += 1;
      this.acc += 1;
      if (this.acc < this.step) continue;
      this.acc -= this.step;
      this.push(this.sum / this.count);
      this.sum = 0;
      this.count = 0;
    }
    return true;
  }

  push(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.frame[this.filled++] = Math.round(clamped * 32767);
    if (this.filled < this.frame.length) return;
    const done = this.frame;
    this.frame = new Int16Array(done.length);
    this.filled = 0;
    this.port.postMessage(done.buffer, [done.buffer]);
  }
}

registerProcessor("huri-mic", HuriMic);
