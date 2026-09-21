/* Speech recognition in the browser: Whisper-tiny on the ONNX runtime, through
 * transformers.js. WebGPU where the browser has it (many times faster than real
 * time on an ordinary laptop), WebAssembly threads where it does not.
 *
 * The transcript only has to be good enough to line up against a book we
 * already have the words of, which is why the smallest model will do.
 */
import { pipeline, env } from '/vendor/transformers/transformers.min.js';

// Everything from our own origin except the model weights themselves.
env.backends.onnx.wasm.wasmPaths = new URL('/vendor/transformers/', import.meta.url).href;
env.allowLocalModels = false;

const MODEL = 'onnx-community/whisper-tiny';

export async function hasWebGpu() {
  try { return !!(navigator.gpu && await navigator.gpu.requestAdapter()); } catch { return false; }
}

export class Recogniser {
  #pipe;
  device;

  /** @param onProgress ({file, loaded, total}) while the ~40 MB of weights download (cached after). */
  static async open(onProgress) {
    const r = new Recogniser();
    r.device = (await hasWebGpu()) ? 'webgpu' : 'wasm';
    const options = (device) => ({
      device,
      // Full-precision encoder: quantising it is what ruins accuracy. The
      // decoder tolerates 4-bit well and is where the time goes.
      dtype: device === 'webgpu'
        ? { encoder_model: 'fp32', decoder_model_merged: 'q4' }
        : { encoder_model: 'fp32', decoder_model_merged: 'q8' },
      progress_callback: (p) => { if (p.status === 'progress') onProgress?.(p); },
    });
    try {
      r.#pipe = await pipeline('automatic-speech-recognition', MODEL, options(r.device));
    } catch (e) {
      if (r.device !== 'webgpu') throw e;
      // A GPU that is listed but cannot run the model: fall back rather than fail.
      r.device = 'wasm';
      r.#pipe = await pipeline('automatic-speech-recognition', MODEL, options('wasm'));
    }
    return r;
  }

  /**
   * @param pcm 16 kHz mono Float32Array
   * @param language a Whisper code ("ja", "ru", ...), or null to let it decide
   * @returns [{text, start, end}] with times offset by `offset` seconds
   */
  async transcribe(pcm, language, offset = 0) {
    const out = await this.#pipe(pcm, {
      return_timestamps: true,
      chunk_length_s: 30,
      stride_length_s: 5,
      task: 'transcribe',
      ...(language ? { language } : {}),
    });
    const segments = [];
    for (const c of out.chunks ?? []) {
      const text = (c.text ?? '').trim();
      const [s, e] = c.timestamp ?? [];
      if (!text || s == null) continue;
      // The last segment of a buffer can come back without an end.
      segments.push({ text, start: offset + s, end: offset + (e ?? pcm.length / 16000) });
    }
    return segments;
  }
}
