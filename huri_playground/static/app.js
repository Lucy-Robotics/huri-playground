// HuRI playground: welcome -> conversation -> questionnaires -> thanks.
// Protocol with the gateway is documented in huri_playground/gateway.py.
"use strict";

const $ = (id) => document.getElementById(id);

const ERRORS = {
  busy: "HuRI is talking with other visitors right now. Please try again in a minute.",
  bad_access_code: "That access code is not right.",
  huri_unavailable: "HuRI is offline at the moment. Please try again later.",
  huri_rejected: "HuRI could not start these modules. Try another combination, or later.",
  invalid_pipeline: "These modules cannot run together",
};

const REJECTIONS = {
  rate_limited: "You are sending messages very quickly. Wait a moment and try again.",
  bad_data: "That message is empty or too long.",
  audio: "Some of your microphone audio did not reach HuRI.",
};

const ENDINGS = {
  time_limit: "Time is up. Thank you for talking with HuRI!",
  idle: "Nobody spoke for a while, so the conversation was closed.",
  huri_closed: "HuRI ended the conversation.",
  error: "The conversation stopped unexpectedly.",
};

const state = {
  config: null,
  ws: null,
  sessionId: null,
  outbound: [], // topics HuRI streams this session
  typed: [], // typed questions HuRI is about to echo back on `huri.question`
  lastVoiceAt: null, // last microphone frame loud enough to be speech
  turn: null, // timings of the turn being answered, see showTiming()
  lateEmotion: null, // your bubble HuRI answered before reading its emotion
  modeName: "", // e.g. "parallel", shown on every timing
  activity: 0, // replies, or spoken questions, HuRI got through
  waiting: false, // a question is in flight
  current: null, // <li> HuRI is writing into
  deadline: null, // when the time is up; null until your first words
  maxSeconds: 0,
  timer: null,
  ended: false,
  step: 0, // index of the questionnaire on screen
  answers: {},
};

function show(screen) {
  document.body.dataset.screen = screen;
  for (const id of ["welcome", "chat", "survey", "end"]) {
    $(`screen-${id}`).hidden = id !== screen;
  }
  window.scrollTo(0, 0);
}

function showError(el, text) {
  el.textContent = text;
  el.hidden = !text;
}

// --- Welcome --------------------------------------------------------------

async function init() {
  try {
    const res = await fetch("/api/config");
    state.config = await res.json();
  } catch {
    showError($("start-error"), ERRORS.huri_unavailable);
    $("start-button").disabled = true;
    return;
  }
  const { config } = state;

  $("welcome-minutes").textContent = Math.round(config.max_seconds / 60);
  $("access-field").hidden = !config.access_code_required;

  if (!config.huri_available || config.modules.length === 0) {
    $("modules-field").hidden = true;
    showError($("start-error"), ERRORS.huri_unavailable);
    $("start-button").disabled = true;
    return;
  }
  picker.init(config);

  $("start-form").addEventListener("submit", (e) => {
    e.preventDefault();
    start();
  });
}

function start() {
  showError($("start-error"), "");
  $("start-button").disabled = true;

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  state.ws = ws;

  ws.addEventListener("open", () => {
    ws.send(
      JSON.stringify({
        type: "start",
        modules: picker.chosen(),
        options: picker.chosenOptions(),
        access_code: $("access-code").value,
      }),
    );
  });
  ws.binaryType = "arraybuffer";
  // Browsers only let audio start from a user gesture: this click is it.
  speaker.unlock();
  ws.addEventListener("message", (e) => {
    if (typeof e.data === "string") onMessage(JSON.parse(e.data));
    else onBinary(e.data);
  });
  ws.addEventListener("close", onClose);
}

// --- Module picker --------------------------------------------------------

// What the page can send (`inbound`) and show (`rendered`) comes with the
// module list, from what this HuRI runs. The gateway applies the same rules
// (huri_playground/pipeline.py): every module's input comes from the page or
// from another ticked module, and something it all produces shows up here.
const SENDS = { "huri.question": "type", "huri.audio.in": "speak" };
const GETS = {
  "huri.token": "writes back",
  "huri.audio.out": "speaks",
  "huri.motion": "gestures",
  "huri.transcript": "shows what it hears",
  "huri.question": "shows what it understood",
  "huri.emotion": "shows the emotion it senses",
};

// Labels of the emotion classifier (huri-modules' `emo` module).
const EMOTIONS = { neu: "neutral", hap: "happy", ang: "angry", sad: "sad" };

function emotionText(emotion) {
  const name = EMOTIONS[emotion.label] || emotion.label;
  return `${name} ${Math.round(emotion.confidence * 100)}%`;
}

const picker = {
  modules: [],
  inbound: [],
  rendered: [],
  selected: new Set(),
  rows: new Map(), // name -> { row, input, needs, options }
  values: {}, // module -> option -> value picked

  init(config) {
    this.modules = config.modules;
    this.inbound = config.inbound;
    this.rendered = config.rendered;
    for (const name of config.selected) this.add(name);

    // One column per group (modules.yaml), side by side as far as the width
    // allows; module settings in one full-width row under them all.
    const settings = document.createElement("div");
    settings.className = "settings-area";
    $("module-options").after(settings);
    const grids = new Map();
    for (const group of config.groups || []) {
      const section = document.createElement("section");
      section.className = "module-group";
      const title = document.createElement("h3");
      title.textContent = group.label;
      const grid = document.createElement("div");
      grid.className = "module-grid";
      section.append(title, grid);
      $("module-options").append(section);
      grids.set(group.id, { section, grid });
    }
    const other = () => grids.values().next().value;

    for (const module of this.modules) {
      const { grid } = grids.get(module.group) || other();
      const row = document.createElement("label");
      row.className = "module";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = module.name;
      input.addEventListener("change", () => {
        if (input.checked) this.add(module.name);
        else this.remove(module.name);
        this.refresh();
      });
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = module.label;
      const topics = document.createElement("span");
      topics.className = "topics";
      topics.textContent = `${module.input} → ${module.output || "—"}`;
      name.append(topics);
      const description = document.createElement("span");
      description.className = "muted small";
      description.textContent = module.description;
      const needs = document.createElement("span");
      needs.className = "needs";
      row.append(input, name, description, needs);
      // Kept short on the tile; the rest on hover.
      row.title = `${module.description}\n${module.input} → ${module.output || "—"}`;
      grid.append(row);
      const options = this.optionsBlock(module);
      if (options) settings.append(options);
      this.rows.set(module.name, { row, input, needs, options });
    }
    this.refresh();
  },

  // Choices for a module's settings, from modules.yaml (e.g. qag's mode).
  optionsBlock(module) {
    const entries = Object.entries(module.options || {});
    if (entries.length === 0) return null;
    const block = document.createElement("div");
    block.className = "module-settings";
    this.values[module.name] = {};
    for (const [option, spec] of entries) {
      this.values[module.name][option] = spec.default;
      const field = document.createElement("fieldset");
      field.dataset.option = option;
      const legend = document.createElement("legend");
      legend.textContent = spec.label;
      field.append(legend);
      for (const { value, label } of spec.values) {
        const choice = document.createElement("label");
        choice.className = "check";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `${module.name}.${option}`;
        radio.value = value;
        radio.checked = value === spec.default;
        // "click", not "change": the default is already selected, and picking
        // it must still tick what it needs.
        radio.addEventListener("click", () => {
          this.values[module.name][option] = value;
          // Picking a value means wanting it to apply: tick what it needs.
          this.add(module.name);
          for (const name of [spec.needs, ...(spec.ticks || [])]) if (name) this.add(name);
          this.refresh();
        });
        const text = document.createElement("span");
        text.textContent = label;
        choice.append(radio, text);
        field.append(choice);
      }
      const hint = document.createElement("p");
      hint.className = "option-hint";
      const ticks = [module.name, spec.needs, ...(spec.ticks || [])].filter(Boolean);
      const labels = [...new Set(ticks)].map((n) => (this.byName(n) || { label: n }).label);
      hint.textContent = `Picking one also ticks ${listOf(labels, "and")}.`;
      field.append(hint);
      block.append(field);
    }
    return block;
  },

  // Whether a module's option takes effect with what is ticked now.
  applies(module, option) {
    const spec = module.options[option];
    return this.selected.has(module.name) && !(spec.needs && !this.selected.has(spec.needs));
  },

  // Option values of ticked modules, for those that apply to this pipeline.
  chosenOptions() {
    const result = {};
    for (const name of this.chosen()) {
      const module = this.byName(name);
      for (const option of Object.keys(module.options || {})) {
        if (!this.applies(module, option)) continue;
        result[name] = result[name] || {};
        result[name][option] = this.values[name][option];
      }
    }
    return result;
  },

  // e.g. "In parallel, HuRI answers while it reads your emotion", if it applies.
  optionsSummary() {
    const parts = [];
    for (const [name, values] of Object.entries(this.chosenOptions())) {
      for (const [option, value] of Object.entries(values)) {
        const spec = this.byName(name).options[option];
        const choice = spec.values.find((v) => v.value === value);
        parts.push(choice ? choice.label : `${option}: ${value}`);
      }
    }
    return parts.map((p) => `${p}.`).join(" ");
  },

  byName(name) {
    return this.modules.find((m) => m.name === name);
  },

  // Ticked names, in the order HuRI listed them.
  chosen() {
    return this.modules.map((m) => m.name).filter((n) => this.selected.has(n));
  },

  produced() {
    return new Set(this.chosen().map((n) => this.byName(n).output).filter(Boolean));
  },

  fed(module, produced = this.produced()) {
    return this.inbound.includes(module.input) || produced.has(module.input);
  },

  producers(topic) {
    return this.modules.filter((m) => m.output === topic);
  },

  // Ticking a module also ticks what feeds it, when only one module can.
  add(name) {
    const module = this.byName(name);
    if (!module || this.selected.has(name)) return;
    this.selected.add(name);
    if (this.fed(module)) return;
    const producers = this.producers(module.input);
    if (producers.length === 1) this.add(producers[0].name);
  },

  // Unticking one also unticks what only it was feeding.
  remove(name) {
    const produced = this.produced();
    const wasFed = this.chosen().filter((n) => this.fed(this.byName(n), produced));
    this.selected.delete(name);
    let changed = true;
    while (changed) {
      changed = false;
      const now = this.produced();
      for (const n of wasFed) {
        if (this.selected.has(n) && !this.fed(this.byName(n), now)) {
          this.selected.delete(n);
          changed = true;
        }
      }
    }
  },

  needs(module) {
    const producers = this.producers(module.input);
    if (producers.length === 0) return `needs "${module.input}", which no module here produces`;
    return `needs ${producers.map((m) => m.label).join(" or ")}`;
  },

  // { problem } or { sends, gets }: the topics this pipeline takes and gives.
  check() {
    const chosen = this.chosen().map((n) => this.byName(n));
    if (chosen.length === 0) return { problem: "Pick at least one module." };
    const produced = this.produced();
    const unfed = chosen.find((m) => !this.fed(m, produced));
    if (unfed) return { problem: `${unfed.label} ${this.needs(unfed)}.` };
    const sends = this.inbound.filter((t) => chosen.some((m) => m.input === t));
    if (sends.length === 0) {
      return { problem: "Nothing you can type or say reaches these modules." };
    }
    const gets = this.rendered.filter((t) => produced.has(t));
    if (gets.length === 0) {
      return { problem: "These modules produce nothing this page can show." };
    }
    // Valid, but e.g. emotion with no question builder goes nowhere visible.
    const silent = sends.filter((t) => !this.shows(t, chosen));
    return { sends: sends.filter((t) => !silent.includes(t)), gets, silent };
  },

  // Whether what the page sends on `topic` leads to something it shows.
  shows(topic, chosen, seen = new Set()) {
    if (seen.has(topic)) return false;
    seen.add(topic);
    return chosen
      .filter((m) => m.input === topic && m.output)
      .some((m) => this.rendered.includes(m.output) || this.shows(m.output, chosen, seen));
  },

  refresh() {
    const produced = this.produced();
    for (const [name, row] of this.rows) {
      const module = this.byName(name);
      row.input.checked = this.selected.has(name);
      row.row.classList.toggle("on", row.input.checked);
      const unfed = row.input.checked && !this.fed(module, produced);
      row.needs.textContent = unfed ? `Also tick: ${this.needs(module).replace(/^needs /, "")}` : "";
      row.needs.hidden = !unfed;
      if (row.options) {
        // Always on screen, so the choice can be found; greyed out, with what
        // picking one ticks, until it applies to the pipeline.
        for (const field of row.options.querySelectorAll("fieldset")) {
          const applies = this.applies(module, field.dataset.option);
          field.classList.toggle("inactive", !applies);
          field.querySelector(".option-hint").hidden = applies;
        }
      }
    }

    const result = this.check();
    const summary = $("pipeline-summary");
    summary.classList.toggle("invalid", Boolean(result.problem));
    summary.textContent = result.problem
      ? result.problem
      : [describe(result), this.optionsSummary()].filter(Boolean).join(" ");
    $("start-button").disabled = Boolean(result.problem);
  },
};

function describe({ sends, gets, silent }) {
  const you = sends.length ? `You ${listOf(sends.map((t) => SENDS[t] || t), "or")}; ` : "";
  const huri = `HuRI ${listOf(gets.map((t) => GETS[t] || t), "and")}.`;
  const lost = silent.length
    ? ` What you ${listOf(silent.map((t) => SENDS[t] || t), "or")} reaches nothing shown here yet.`
    : "";
  return you + huri + lost;
}

function listOf(words, joiner) {
  if (words.length < 2) return words.join("");
  return `${words.slice(0, -1).join(", ")} ${joiner} ${words[words.length - 1]}`;
}

// --- Voice ----------------------------------------------------------------

// Plays HuRI's `audio.out` chunks back to back, in the order they arrive.
// avatar.js reads level() for Mouse-Man's talking cues, and contextTime() to
// play `motion` frames in step with the voice.
const speaker = {
  ctx: null,
  analyser: null,
  samples: null,
  next: 0, // AudioContext time at which the next chunk starts
  // [pts, AudioContext time] of each chunk of the current utterance. Kept
  // after the end marker: the last gesture chunk arrives after it.
  anchors: [],
  fresh: true, // the next chunk opens a new utterance

  unlock() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.connect(this.ctx.destination);
      this.samples = new Float32Array(this.analyser.fftSize);
    }
    this.ctx.resume();
  },

  // Loudness (RMS) of what is playing right now; about 0.1 for speech.
  level() {
    if (!this.analyser) return 0;
    this.analyser.getFloatTimeDomainData(this.samples);
    let sum = 0;
    for (const s of this.samples) sum += s * s;
    return Math.sqrt(sum / this.samples.length);
  },

  play(sampleRate, samples, pts, end) {
    if (this.ctx && samples.length > 0) {
      const buffer = this.ctx.createBuffer(1, samples.length, sampleRate);
      buffer.copyToChannel(samples, 0);
      const source = this.ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(this.analyser);
      this.next = Math.max(this.next, this.ctx.currentTime + 0.05);
      source.start(this.next);

      if (this.fresh) this.anchors = [];
      this.fresh = false;
      this.anchors.push([pts, this.next]);
      const turn = state.turn;
      // Not before the first word: audio before it is HuRI thinking aloud
      // (huri-module-think's "hmm"), not its answer.
      if (turn && turn.voiceAt === null && turn.firstWordAt !== null) {
        // When this first chunk is heard, on the page's clock.
        turn.voiceAt = performance.now() + (this.next - this.ctx.currentTime) * 1000;
        showTiming();
      }
      this.next += buffer.duration;
    }
    if (end) this.fresh = true;
  },

  // True while HuRI's voice is playing, plus a short tail for the room's echo.
  speaking() {
    return Boolean(this.ctx) && this.next + 0.3 > this.ctx.currentTime;
  },

  // AudioContext time at which utterance time `pts` is (or was) heard.
  contextTime(pts) {
    let anchor = null;
    for (const a of this.anchors) if (a[0] <= pts) anchor = a;
    return anchor && anchor[1] + (pts - anchor[0]);
  },
};
window.huriSpeaker = speaker;
// avatar.js: when the visitor last spoke (performance.now() ms), so Mouse-Man
// listens while they talk.
window.huriListener = { heardAt: () => state.lastVoiceAt };

// A 30 ms frame at least this loud (RMS, full scale = 1) counts as speech, to
// time when you stopped talking. Speech is around 0.03-0.3, a quiet room 0.005.
const SPEECH_RMS = 0.015;

function rms(pcm) {
  const samples = new Int16Array(pcm);
  let sum = 0;
  for (const s of samples) sum += s * s;
  return Math.sqrt(sum / samples.length) / 32768;
}

// Microphone -> `huri.audio.in`: 30 ms frames of 16 kHz int16 PCM, cut by
// mic-worklet.js, which is what HuRI's `vad` module was given (modules.yaml).
const AUDIO_IN = new TextEncoder().encode("huri.audio.in");

const mic = {
  ctx: null,
  stream: null,
  on: false,

  async toggle() {
    if (this.on) this.stop();
    else await this.start();
  },

  async start() {
    if (!navigator.mediaDevices) {
      return notice("The microphone only works on a secure (https) page.");
    }
    $("mic").disabled = true;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      this.ctx = new AudioContext();
      await this.ctx.audioWorklet.addModule("/static/mic-worklet.js");
      const source = this.ctx.createMediaStreamSource(this.stream);
      const node = new AudioWorkletNode(this.ctx, "huri-mic", {
        processorOptions: { targetRate: 16000, frameSamples: 480 },
      });
      node.port.onmessage = (e) => this.send(e.data);
      source.connect(node);
      // Only so the graph runs: the node itself outputs silence.
      node.connect(this.ctx.destination);
      this.on = true;
    } catch {
      this.stop();
      notice("HuRI cannot hear you: the microphone is blocked or missing.");
    }
    $("mic").disabled = false;
    this.render();
  },

  stop() {
    if (this.stream) for (const track of this.stream.getTracks()) track.stop();
    if (this.ctx) this.ctx.close();
    this.stream = null;
    this.ctx = null;
    this.on = false;
    this.render();
  },

  send(pcm) {
    const ws = state.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN || state.ended) return;
    // While HuRI talks, silence is sent instead: the speakers would otherwise
    // feed its own voice back to it as a new question.
    const muted = speaker.speaking();
    if (!muted && rms(pcm) > SPEECH_RMS) state.lastVoiceAt = performance.now();
    const payload = new Uint8Array(muted ? pcm.byteLength : pcm);
    const frame = new Uint8Array(2 + AUDIO_IN.length + payload.length);
    new DataView(frame.buffer).setUint16(0, AUDIO_IN.length);
    frame.set(AUDIO_IN, 2);
    frame.set(payload, 2 + AUDIO_IN.length);
    ws.send(frame.buffer);
  },

  render() {
    $("mic").setAttribute("aria-pressed", String(this.on));
    $("mic").textContent = this.on ? "Turn the microphone off" : "Turn the microphone on";
    $("mic-status").textContent = this.on ? "Listening: just talk." : "";
  },
};

// Binary frame: [u16 BE topic_len][topic][payload].
function onBinary(data) {
  const view = new DataView(data);
  const topicLen = view.getUint16(0);
  const topic = new TextDecoder().decode(new Uint8Array(data, 2, topicLen));
  const start = 2 + topicLen;
  if (state.ended) return;

  if (topic === "huri.audio.out") {
    // [u32 BE sample_rate][u8 end][f64 BE pts][float32 samples]
    const sampleRate = view.getUint32(start);
    const end = view.getUint8(start + 4) === 1;
    const pts = view.getFloat64(start + 5);
    // slice() copies into a fresh, 4-byte aligned buffer for Float32Array.
    const samples = new Float32Array(data.slice(start + 13));
    speaker.play(sampleRate, samples, pts, end);
  } else if (topic === "huri.motion") {
    // Parsed by avatar.js: [f64 BE pts][u32 BE fps][u32 BE frames][float32...]
    window.dispatchEvent(
      new CustomEvent("huri-motion", { detail: data.slice(start) }),
    );
  }
}

// --- Conversation ---------------------------------------------------------

function onMessage(msg) {
  if (msg.type === "ready") return onReady(msg);
  if (msg.type === "error") return onError(msg.reason, msg.detail);
  if (msg.type === "rejected") return onRejected(msg);
  if (msg.type === "ended") return onEnded(msg.reason);
  if (msg.type === "clock") return startClock(msg.max_seconds);
  if (msg.topic === "huri.token") return onToken(msg.data);
  if (msg.topic === "huri.question") return onQuestion(msg.data);
  if (msg.topic === "huri.transcript") return onTranscript(msg.data);
  if (msg.topic === "huri.emotion") return onEmotion(msg.data);
}

function onReady(msg) {
  state.sessionId = msg.session_id;
  state.outbound = msg.outbound || [];
  // Tells avatar.js whether HuRI will send gestures this session.
  window.dispatchEvent(
    new CustomEvent("huri-session", { detail: { outbound: msg.outbound || [] } }),
  );
  const canType = "huri.question" in msg.inbound;
  const canSpeak = "huri.audio.in" in msg.inbound;
  $("composer").hidden = !canType;
  $("mic-bar").hidden = !canSpeak;
  mic.render();
  const maxChars = canType && msg.inbound["huri.question"].max_chars;
  if (maxChars) $("question").maxLength = maxChars;
  showMode(msg.options || {}, msg.modules || []);

  // The time counts from your first words (see startClock).
  state.deadline = null;
  state.maxSeconds = msg.max_seconds;
  tick();

  show("chat");
  if (canType) $("question").focus();
  else $("mic").focus();
}

// The settings this session runs with, e.g. "Emotion and answer: in parallel".
// The settings this session runs with, spelled out in the header, and the
// short name every timing tag starts with (state.modeName).
function showMode(options, modules) {
  const parts = [];
  const names = [];
  for (const [name, values] of Object.entries(options)) {
    const module = picker.byName(name);
    for (const [option, value] of Object.entries(values)) {
      const spec = module && module.options[option];
      const choice = spec && spec.values.find((v) => v.value === value);
      parts.push(choice ? choice.label : `${name}.${option}: ${value}`);
      names.push(value);
    }
  }
  // qag without eag: nothing to wait for, so no mode either.
  if (parts.length === 0 && modules.includes("qag")) {
    parts.push("No emotion analysis: HuRI answers as soon as it has your sentence");
    names.push("no emotion");
  }
  state.modeName = names.join(", ");
  $("mode").textContent = parts.join(" · ");
  $("mode").title = parts.join(" · "); // in full, when the badge is cut short
  $("mode").hidden = parts.length === 0;
}

function seconds(ms) {
  return `${(ms / 1000).toFixed(1)} s`;
}

function newTurn(stoppedAt, typed) {
  const now = performance.now();
  // No loud frame in the last 10 s: the speech end is unknown, not ages ago.
  const known = stoppedAt !== null && now - stoppedAt < 10000;
  state.turn = {
    typed,
    stoppedAt: known ? stoppedAt : null,
    heardAt: now, // final transcript in (or question typed)
    sentAt: null, // question out of qag
    firstWordAt: null,
    voiceAt: null, // HuRI's voice starts playing
    li: null, // HuRI's answer
    textDone: false,
    noVoice: false, // Voice is on, but none came back
  };
}

// What a person waits for: from the end of their sentence to HuRI's voice
// (or its first written word, without Voice), with where the time went.
function showTiming() {
  const t = state.turn;
  if (!t || !t.textDone || !t.li) return;
  const spoken = state.outbound.includes("huri.audio.out") && !t.noVoice;
  if (spoken && t.voiceAt === null) return; // the voice is still coming
  const end = spoken ? t.voiceAt : t.firstWordAt;
  if (end === null) return;
  state.turn = null;

  const verb = spoken ? "spoke" : "wrote back";
  const mode = state.modeName ? `${capitalize(state.modeName)}: ` : "";
  if (t.typed) {
    addTag(t.li, `${mode}HuRI ${verb} ${seconds(end - t.heardAt)} after you sent it`);
    return;
  }
  const start = t.stoppedAt === null ? t.heardAt : t.stoppedAt;
  const parts = [];
  if (t.stoppedAt !== null) parts.push(`hearing you were done ${seconds(t.heardAt - t.stoppedAt)}`);
  if (t.sentAt !== null) {
    parts.push(`emotion wait ${seconds(t.sentAt - t.heardAt)}`);
    parts.push(`answer ${seconds(end - t.sentAt)}`);
  }
  const after = t.stoppedAt === null ? "after you were heard" : "after you stopped talking";
  addTag(t.li, `${mode}HuRI ${verb} ${seconds(end - start)} ${after} (${parts.join(" · ")})`);
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function addTag(li, text) {
  const tag = document.createElement("span");
  tag.className = "emotion";
  tag.textContent = text;
  li.append(tag);
  return tag;
}

function onError(reason, detail) {
  state.ended = true;
  const text = ERRORS[reason] || "Something went wrong. Please try again.";
  showError($("start-error"), detail ? `${text}: ${detail}` : text);
  $("start-button").disabled = false;
  show("welcome");
}

function notice(text) {
  $("chat-notice").textContent = text;
  $("chat-notice").hidden = false;
}

function onRejected(msg) {
  // A dropped microphone frame is not a dropped question: keep the reply.
  if (msg.topic === "huri.audio.in") return notice(REJECTIONS.audio);
  notice(REJECTIONS[msg.reason] || "That message was not sent.");
  // The question never reached HuRI: drop its pending reply and let the
  // visitor try again.
  if (state.current) state.current.remove();
  state.current = null;
  setWaiting(false);
}

function onToken(token) {
  if (!state.current) state.current = addMessage("huri", "");
  const turn = state.turn;
  if (turn && token.text && turn.firstWordAt === null) turn.firstWordAt = performance.now();
  if (token.end) {
    state.current.classList.remove("pending");
    if (turn) {
      // Text is complete, so a tag can go on the bubble without being
      // overwritten by the next token.
      turn.li = state.current;
      turn.textDone = true;
      showTiming();
      // No voice after all (TTS failed): fall back to the first written word.
      setTimeout(() => {
        if (state.turn === turn) {
          turn.noVoice = true;
          showTiming();
        }
      }, 15000);
    }
    state.current = null;
    gotThrough();
    setWaiting(false);
    return;
  }
  state.current.textContent += token.text;
  scrollMessages();
}

// What HuRI understood from the microphone, as a RAGQuestion. With `qag` in
// the pipeline a typed question comes back on this topic too: skip that echo.
function onQuestion(question) {
  const text = question.transcript && question.transcript.text;
  if (!text) return;
  const echo = state.typed.indexOf(text);
  if (echo !== -1) {
    state.typed.splice(echo, 1);
    return;
  }
  $("hearing").hidden = true;
  const li = addMessage("you", text);
  const turn = state.turn;
  if (turn && turn.sentAt === null) turn.sentAt = performance.now();
  const waited = turn ? ` (after ${seconds(turn.sentAt - turn.heardAt)})` : "";
  // How the sentence sounded, summed up by eag: what HuRI answers to. In
  // parallel mode an emotion read after its question left goes with the next
  // one, flagged `emotion_earlier`.
  if (question.emotion && question.emotion_earlier) {
    addTag(
      li,
      `HuRI used the emotion of your previous message: ${emotionText(question.emotion)}${waited}`,
    );
  } else if (question.emotion) {
    addTag(li, `HuRI sensed: ${emotionText(question.emotion)}${waited}`);
  } else if (state.outbound.includes("huri.emotion")) {
    // Parallel: the question left before its emotion was read.
    state.lateEmotion = addTag(li, `Sent to HuRI before your emotion was read${waited}`);
  }
  gotThrough();
  if (state.outbound.includes("huri.token") && !state.current) {
    state.current = addMessage("huri", "");
    state.current.classList.add("pending");
    setWaiting(true);
  }
}

// Speech to text, window by window, as HuRI hears it.
function onTranscript(transcript) {
  // The final pass over your whole sentence: from here on, HuRI is the one
  // making you wait.
  if (transcript.end && transcript.text) newTurn(state.lastVoiceAt, false);
  if (!transcript.text) return;
  $("hearing").textContent = `Hearing: ${transcript.text.trim()}`;
  $("hearing").hidden = false;
  gotThrough();
}

// Emotion read from your voice, window by window, while you speak.
function onEmotion(emotion) {
  const sensing = $("sensing");
  sensing.replaceChildren();
  const head = document.createElement("strong");
  head.textContent = `HuRI senses: ${emotionText(emotion)}`;
  sensing.append(head);
  const scores = Object.entries(emotion.scores).sort((a, b) => b[1] - a[1]);
  for (const [label, score] of scores) {
    const bar = document.createElement("span");
    bar.className = "score";
    bar.style.setProperty("--score", score);
    bar.textContent = `${EMOTIONS[label] || label} ${Math.round(score * 100)}%`;
    sensing.append(bar);
  }
  sensing.hidden = false;
  if (emotion.end && state.lateEmotion) {
    state.lateEmotion.textContent += `; read afterwards: ${emotionText(emotion)}, kept for your next message`;
    state.lateEmotion = null;
  }
}

function gotThrough() {
  state.activity += 1;
  $("finish").disabled = false;
}

function onEnded(reason) {
  state.ended = true;
  stopTimer();
  if (reason === "time_limit") {
    $("timer").textContent = "Time is up";
    $("timer").classList.remove("low");
  }
  if (reason === "finished") return startSurvey();
  endConversation(ENDINGS[reason] || ENDINGS.error);
}

function onClose() {
  if (state.ended) return;
  state.ended = true;
  stopTimer();
  if (!state.sessionId) return onError("huri_unavailable");
  endConversation(ENDINGS.error);
}

// The conversation is over without the visitor pressing "finish": it stays
// on screen to scroll back through, and they go rate it when they want, if
// HuRI got to answer at least once; otherwise there is nothing to rate.
function endConversation(text) {
  setWaiting(true);
  $("chat-status").textContent = "Conversation over";
  $("question").disabled = true;
  $("mic-bar").hidden = true;
  if (state.activity > 0) {
    $("chat-notice").textContent = `${text} You can scroll back through the conversation, then rate HuRI.`;
    $("chat-notice").hidden = false;
    $("finish").textContent = "Rate HuRI";
    $("finish-hint").textContent = "Take your time to read it back.";
    $("finish").disabled = false;
  } else {
    showEnd("The conversation ended", `${text} Nothing to rate this time.`, true);
  }
}

function sendQuestion(e) {
  e.preventDefault();
  const text = $("question").value.trim();
  if (!text || state.waiting || state.ended) return;

  $("chat-notice").hidden = true;
  addMessage("you", text);
  state.current = addMessage("huri", "");
  state.current.classList.add("pending");
  $("question").value = "";
  setWaiting(true);
  newTurn(null, true);
  if (state.outbound.includes("huri.question")) state.typed.push(text);

  state.ws.send(JSON.stringify({ topic: "huri.question", data: { text } }));
}

function finish() {
  $("finish").disabled = true;
  if (state.ended) {
    startSurvey(); // the conversation already ended: on to the questions
  } else if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "finish" }));
  } else {
    startSurvey();
  }
}

function setWaiting(waiting) {
  state.waiting = waiting;
  $("send").disabled = waiting;
  $("chat-status").textContent = waiting ? "HuRI is answering…" : "Your turn";
}

function addMessage(who, text) {
  const li = document.createElement("li");
  li.className = who;
  li.textContent = text;
  $("messages").append(li);
  scrollMessages();
  return li;
}

function scrollMessages() {
  const list = $("messages");
  list.scrollTop = list.scrollHeight;
}

// The gateway starts the session's time when it hears your first words, a
// typed question or anything HuRI says back to your voice.
function startClock(maxSeconds) {
  if (state.deadline !== null || state.ended) return;
  state.deadline = Date.now() + maxSeconds * 1000;
  tick();
  state.timer = setInterval(tick, 1000);
}

function tick() {
  const waiting = state.deadline === null;
  const total = waiting ? state.maxSeconds : (state.deadline - Date.now()) / 1000;
  const left = Math.max(0, Math.round(total));
  const min = Math.floor(left / 60);
  const sec = String(left % 60).padStart(2, "0");
  $("timer").textContent = waiting
    ? `${min}:${sec}, from your first words`
    : `${min}:${sec} left`;
  $("timer").classList.toggle("low", !waiting && left <= 30);
}

function stopTimer() {
  clearInterval(state.timer);
  mic.stop();
}

// --- Questionnaires -------------------------------------------------------

function startSurvey() {
  if (!$("screen-survey").hidden) return;
  if (state.ws) state.ws.close();
  state.step = 0;
  renderSurvey();
  show("survey");
}

function renderSurvey() {
  const qs = state.config.questionnaires;
  const q = qs[state.step];
  const last = state.step === qs.length - 1;

  $("survey-step").textContent = `Part ${state.step + 1} of ${qs.length}`;
  $("survey-title").textContent = q.title;
  $("comment-field").hidden = !last;
  $("survey-next").textContent = last ? "Send my answers" : "Next";
  showError($("survey-error"), "");

  const body = $("survey-body");
  body.replaceChildren();
  const differential = q.sections.some((s) => s.items.some((i) => i.low));
  body.className = differential ? "differential" : "likert";

  for (const section of q.sections) {
    const prompt = document.createElement("p");
    prompt.className = "section-prompt";
    prompt.textContent = section.prompt;
    body.append(prompt, scaleHead(q, differential));
    for (const item of section.items) body.append(itemRow(q, item, differential));
  }
}

function scaleHead(q, differential) {
  const head = document.createElement("div");
  head.className = "scale-head";
  head.setAttribute("aria-hidden", "true");
  const ends = document.createElement("span");
  ends.className = "ends";
  const low = document.createElement("span");
  const high = document.createElement("span");
  low.textContent = differential ? "" : q.scale.low || "";
  high.textContent = differential ? "" : q.scale.high || "";
  ends.append(low, high);
  if (differential) {
    head.append(document.createElement("span"), ends, document.createElement("span"));
  } else {
    head.append(document.createElement("span"), ends);
  }
  return head;
}

function itemRow(q, item, differential) {
  const row = document.createElement("fieldset");
  row.className = "item";
  row.dataset.item = item.id;

  const legend = document.createElement("legend");
  legend.className = "visually-hidden";
  legend.textContent = differential
    ? `From ${item.low} (1) to ${item.high} (${q.scale.points})`
    : `${item.label}, from ${q.scale.low} (1) to ${q.scale.high} (${q.scale.points})`;
  row.append(legend);

  const points = document.createElement("div");
  points.className = "points";
  const previous = (state.answers[q.id] || {})[item.id];
  for (let value = 1; value <= q.scale.points; value++) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `${q.id}.${item.id}`;
    input.value = String(value);
    input.checked = previous === value;
    input.addEventListener("change", () => row.classList.remove("missing"));
    const n = document.createElement("span");
    n.textContent = String(value);
    label.append(input, n);
    points.append(label);
  }

  if (differential) {
    const low = document.createElement("span");
    low.className = "low";
    low.textContent = item.low;
    const high = document.createElement("span");
    high.className = "high";
    high.textContent = item.high;
    row.append(low, points, high);
  } else {
    const name = document.createElement("span");
    name.textContent = item.label;
    row.append(name, points);
  }
  return row;
}

async function nextSurveyStep(e) {
  e.preventDefault();
  const qs = state.config.questionnaires;
  const q = qs[state.step];

  const answers = {};
  let firstMissing = null;
  for (const row of document.querySelectorAll("#survey-body .item")) {
    const checked = row.querySelector("input:checked");
    if (checked) {
      answers[row.dataset.item] = Number(checked.value);
    } else {
      row.classList.add("missing");
      firstMissing = firstMissing || row;
    }
  }
  if (firstMissing) {
    showError($("survey-error"), "Please answer every line before going on.");
    firstMissing.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  state.answers[q.id] = answers;

  if (state.step < qs.length - 1) {
    state.step += 1;
    renderSurvey();
    window.scrollTo(0, 0);
    return;
  }
  await submit();
}

async function submit() {
  $("survey-next").disabled = true;
  showError($("survey-error"), "");
  try {
    const res = await fetch("/api/responses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.sessionId,
        answers: state.answers,
        comment: $("comment").value,
      }),
    });
    if (res.status === 201 || res.status === 409) {
      showEnd("Thank you!", "Your answers were saved. You can close this page.", false);
      return;
    }
    const body = await res.json().catch(() => ({}));
    showError($("survey-error"), body.error || "Your answers could not be saved.");
  } catch {
    showError($("survey-error"), "Your answers could not be sent. Check your connection and try again.");
  }
  $("survey-next").disabled = false;
}

function showEnd(title, text, canRestart) {
  $("end-title").textContent = title;
  $("end-text").textContent = text;
  $("restart").hidden = !canRestart;
  show("end");
}

// --- Wiring ---------------------------------------------------------------

$("composer").addEventListener("submit", sendQuestion);
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) sendQuestion(e);
});
$("finish").addEventListener("click", finish);
$("mic").addEventListener("click", () => mic.toggle());
$("survey-form").addEventListener("submit", nextSurveyStep);
$("restart").addEventListener("click", () => location.reload());

init();
