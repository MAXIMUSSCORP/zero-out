// Posts each block of microphone samples (channel 0) to the page as a Float32Array.
class RecorderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(new Float32Array(ch));
    return true;
  }
}
registerProcessor('recorder', RecorderProcessor);
