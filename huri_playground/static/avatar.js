// Mouse-Man on the welcome and conversation screens: the character from
// HuRI_website_demo.
//
// Two ways he moves, picked per session by what HuRI can run:
//   * gestures: HuRI's `mov` module (EMAGE) streams SMPL-X motion, retargeted
//     onto the Mixamo rig in step with the voice (see HuRI_website_demo's
//     Character.jsx, which this follows);
//   * idle: between and without gestures, alive like a person standing
//     still (breathing, glances, weight shifts, listening), see idleLife().
//
// The model is a 30 MB FBX kept out of git. Without it at MODEL_URL the page
// stays text only. It (Mixamo "Ch14") has no jaw bone and no blend shapes, so
// its mouth cannot open; a model with ARKit-style shapes (jawOpen,
// eyeBlinkLeft/Right) gets a talking mouth and blinks.
import * as THREE from "three";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";

const MODEL_URL = "/static/avatar/model.fbx";

// Voice loudness (RMS) -> talking cues (0..1).
const MOUTH_GAIN = 7;
const MOUTH_FLOOR = 0.01; // below this it is silence or breath
const MOUTH_SMOOTH_S = 0.05;
// The idle head follows the voice's loudness this slowly: following each
// syllable shakes it.
const TALK_SMOOTH_S = 0.35;

// Bones ease toward their target pose: hides seams between gesture chunks
// (EMAGE windows are separate inferences) and the switch to and from idle.
const GESTURE_SMOOTH_S = 0.09;
const IDLE_SMOOTH_S = 0.3;
// Gestures are sampled this far ahead so, once eased, they land on time.
const GESTURE_LEAD_S = GESTURE_SMOOTH_S;
// Past the last gesture frame by this much, he goes back to idling; longer
// while HuRI is still speaking: the next gestures are on their way, and
// dropping to idle in between, then back, reads as twitching.
const GESTURE_HOLD_S = 0.3;
const SPEAKING_HOLD_S = 2.0;
// Out of idle, bones ease in this slowly for ENTER_S: a gesture starting
// mid-swing glides there instead of snapping.
const ENTER_SMOOTH_S = 0.2;
const ENTER_S = 0.5;
// Gesture frames that arrive after their moment in the voice are played this
// late rather than skipped (a jump to mid-gesture), up to MAX_LAG_S.
const LATE_MARGIN_S = 0.05;
const MAX_LAG_S = 3.0;
// Once gestures come in time again, the delay melts away by playing them up
// to this much faster.
const CATCH_UP = 0.25;

// EMAGE moves the whole body like a person shifting their weight; on a
// cartoon standing still that reads as wobbling on the spot. The legs stay
// planted and the pelvis keeps a little of its rotation.
const PLANTED = new Set([
  "left_hip", "right_hip", "left_knee", "right_knee",
  "left_ankle", "right_ankle", "left_foot", "right_foot",
]);
const PELVIS_GAIN = 0.3;
// Human gestures in front of the belly go through Mouse-Man's, much rounder:
// an arm whose hand or forearm would enter the torso swings out of it.
const TORSO_SLICES = 12;
const MAX_PUSH_RAD = 1.2;
const PUSH_IN_RAD_S = 6; // how fast the swing out builds up
const PUSH_OUT_RAD_S = 1.5; // and lets go
const AXIS_SMOOTH_S = 0.15; // how fast the swing's direction may turn
// EMAGE has quick flicks (a forearm turning 40 degrees in a frame); a bone
// turns at most this fast, so they stay quick but continuous.
const MAX_BONE_RAD_S = 6;
// EMAGE nods the head on nearly every syllable, small and quick: on a cartoon
// head that reads as shaking. Neck and head ease in slower.
const SLOW_JOINTS = new Set(["neck", "head"]);
const HEAD_SMOOTH_S = 0.2;

// SMPL-X joint order: poses[:, i*3 : i*3+3] is joint i, axis-angle.
const SMPLX_JOINTS = [
  "pelvis", "left_hip", "right_hip", "spine1", "left_knee", "right_knee",
  "spine2", "left_ankle", "right_ankle", "spine3", "left_foot", "right_foot",
  "neck", "left_collar", "right_collar", "head", "left_shoulder",
  "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
  "jaw", "left_eye", "right_eye",
  "left_index1", "left_index2", "left_index3",
  "left_middle1", "left_middle2", "left_middle3",
  "left_pinky1", "left_pinky2", "left_pinky3",
  "left_ring1", "left_ring2", "left_ring3",
  "left_thumb1", "left_thumb2", "left_thumb3",
  "right_index1", "right_index2", "right_index3",
  "right_middle1", "right_middle2", "right_middle3",
  "right_pinky1", "right_pinky2", "right_pinky3",
  "right_ring1", "right_ring2", "right_ring3",
  "right_thumb1", "right_thumb2", "right_thumb3",
];
const POSE_DIM = 165;
const EXPR_DIM = 100;

// [SMPL-X joint, its parent, Mixamo bone], parents first.
const BODY = [
  ["pelvis", null, "Hips"],
  ["left_hip", "pelvis", "LeftUpLeg"],
  ["right_hip", "pelvis", "RightUpLeg"],
  ["spine1", "pelvis", "Spine"],
  ["left_knee", "left_hip", "LeftLeg"],
  ["right_knee", "right_hip", "RightLeg"],
  ["spine2", "spine1", "Spine1"],
  ["left_ankle", "left_knee", "LeftFoot"],
  ["right_ankle", "right_knee", "RightFoot"],
  ["spine3", "spine2", "Spine2"],
  ["left_foot", "left_ankle", "LeftToeBase"],
  ["right_foot", "right_ankle", "RightToeBase"],
  ["neck", "spine3", "Neck"],
  ["left_collar", "spine3", "LeftShoulder"],
  ["right_collar", "spine3", "RightShoulder"],
  ["head", "neck", "Head"],
  ["left_shoulder", "left_collar", "LeftArm"],
  ["right_shoulder", "right_collar", "RightArm"],
  ["left_elbow", "left_shoulder", "LeftForeArm"],
  ["right_elbow", "right_shoulder", "RightForeArm"],
  ["left_wrist", "left_elbow", "LeftHand"],
  ["right_wrist", "right_elbow", "RightHand"],
];
for (const side of ["left", "right"]) {
  const Side = side === "left" ? "Left" : "Right";
  for (const [finger, Finger] of [
    ["thumb", "Thumb"], ["index", "Index"], ["middle", "Middle"],
    ["ring", "Ring"], ["pinky", "Pinky"],
  ]) {
    for (let k = 1; k <= 3; k++) {
      const parent = k === 1 ? `${side}_wrist` : `${side}_${finger}${k - 1}`;
      BODY.push([`${side}_${finger}${k}`, parent, `${Side}Hand${Finger}${k}`]);
    }
  }
}
const JOINT_INDEX = Object.fromEntries(SMPLX_JOINTS.map((name, i) => [name, i]));

const container = document.getElementById("avatar");

async function main() {
  const probe = await fetch(MODEL_URL, { method: "HEAD" });
  if (!probe.ok) return;

  container.hidden = false;
  container.dataset.state = "loading";
  document.body.classList.add("has-avatar");

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.append(renderer.domElement);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 2.2));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(1, 2, 3);
  scene.add(key);

  const model = await new FBXLoader().loadAsync(MODEL_URL);
  scene.add(model);

  const box = new THREE.Box3().setFromObject(model);
  const center = box.getCenter(new THREE.Vector3());
  model.position.set(-center.x, -box.min.y, -center.z);
  const height = box.max.y - box.min.y;
  model.updateMatrixWorld(true);

  // Rest (T-pose) world orientations, before anything is moved: gesture
  // retargeting is relative to them.
  const rig = retargetRig(model);

  lowerArm(model, "mixamorigLeftArm", "mixamorigLeftForeArm", 1);
  lowerArm(model, "mixamorigRightArm", "mixamorigRightForeArm", -1);
  // The idle pose: rest with the arms down.
  const idle = new Map(rig.bones.map(({ bone }) => [bone, bone.quaternion.clone()]));
  const torso = torsoGuard(model);
  if (new URLSearchParams(location.search).has("debug")) {
    window.mouseMan = { model, torso, gestures: null };
  }

  const camera = new THREE.PerspectiveCamera(25, 1, height * 0.01, height * 20);
  const shots = cameraShots(model, height);
  let wide = 0; // 0 = head and shoulders, 1 = waist up (for gestures)
  let wideTarget = 0;

  const face = faceControls(model);
  const head = model.getObjectByName("mixamorigHead");
  const life = idleLife(model, idle);

  const gestures = new GestureTrack();
  if (window.mouseMan) window.mouseMan.gestures = gestures;
  window.addEventListener("huri-session", (e) => {
    gestures.clear();
    wideTarget = e.detail.outbound.includes("huri.motion") ? 1 : 0;
  });
  window.addEventListener("huri-motion", (e) => {
    const speaker = window.huriSpeaker;
    if (speaker) gestures.add(e.detail, speaker);
  });

  function resize() {
    const { clientWidth: w, clientHeight: h } = container;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const clock = new THREE.Clock();
  let mouth = 0;
  let talking = 0; // the voice's loudness, smoothed over a few syllables
  let nextBlink = 2;
  let gesturing = 0; // seconds since gestures took over from idle

  renderer.setAnimationLoop(() => {
    const dt = clock.getDelta();
    const t = clock.elapsedTime;
    if (container.offsetParent === null) return; // screen without Mouse-Man

    const speaker = window.huriSpeaker;
    const level = speaker ? speaker.level() : 0;
    const open = Math.min(1, Math.max(0, (level - MOUTH_FLOOR) * MOUTH_GAIN));
    mouth += (open - mouth) * (1 - Math.exp(-dt / MOUTH_SMOOTH_S));
    talking += (open - talking) * (1 - Math.exp(-dt / TALK_SMOOTH_S));
    face.set("jawOpen", mouth * 0.8);
    face.set("mouthFunnel", mouth * 0.2);

    let blink = 0;
    if (t > nextBlink) {
      const phase = (t - nextBlink) / 0.15;
      blink = phase < 1 ? Math.sin(phase * Math.PI) : 0;
      if (phase >= 1) nextBlink = t + 2 + Math.random() * 3;
    }
    face.set("eyeBlinkLeft", blink);
    face.set("eyeBlinkRight", blink);

    const now = speaker && speaker.ctx ? speaker.ctx.currentTime : null;
    const pose =
      now === null ? null : gestures.sample(now + GESTURE_LEAD_S, speaker.speaking());
    if (pose) {
      if (gesturing === 0) torso.forget(); // idle's swing is not the gesture's
      const smooth = gesturing < ENTER_S ? ENTER_SMOOTH_S : GESTURE_SMOOTH_S;
      gesturing += dt;
      torso.release(); // the gesture eases on from its own pose, not the swung one
      rig.apply(pose, 1 - Math.exp(-dt / smooth), MAX_BONE_RAD_S * dt, 1 - Math.exp(-dt / HEAD_SMOOTH_S));
      torso.keepArmsOut(dt);
    } else {
      if (gesturing > 0) torso.forget(); // idling eases on from the pose on screen
      gesturing = 0;
      const listener = window.huriListener;
      life.update(dt, Math.min(1, talking * 1.5), listener ? listener.heardAt() : null);
      torso.release();
      life.apply(1 - Math.exp(-dt / IDLE_SMOOTH_S));
      torso.keepArmsOut(dt);
    }
    // No mouth on this model: a little cartoon squash on loud syllables.
    if (head) head.scale.set(1 + mouth * 0.025, 1 - mouth * 0.035, 1 + mouth * 0.025);

    wide += (wideTarget - wide) * (1 - Math.exp(-dt / 0.5));
    const y = THREE.MathUtils.lerp(shots.close.y, shots.wide.y, wide);
    const z = THREE.MathUtils.lerp(shots.close.distance, shots.wide.distance, wide);
    camera.position.set(0, y, z);
    camera.lookAt(0, y, 0);

    renderer.render(scene, camera);
  });
  container.dataset.state = "ready";
}

// Gesture frames received from HuRI, on the AudioContext clock, in two
// tracks that never mix (alternating between two poses every frame shakes).
//
// Speech gestures (`mov`, EMAGE on HuRI's voice) come with a face (expression
// coefficients) and are timed by the voice. Thinking motion
// (huri-module-think's `think`, between a question and its answer) has no
// face: it starts at pts 0 and runs on the clock from its arrival, its eased
// return to rest included. While it runs it has the body: `mov` also
// gestures on the thinking phrases ("hmm..."), and those gestures wait.
class GestureTrack {
  constructor() {
    this.out = new Float32Array(SMPLX_JOINTS.length * 4);
    this.clear();
  }

  clear() {
    this.speech = []; // {time, quats: Float32Array(55 * 4)}, by time
    this.thinking = [];
    this.thinkBase = null; // AudioContext time of the thinking motion's pts 0
    this.lag = 0; // how late this utterance's gestures play behind the voice
    this.utterance = null; // when that utterance's voice started
  }

  // Gestures computed after their moment in the voice play late, from where
  // he is, rather than jumping into the middle of a move. The next chunks
  // continue from that delay, and play a little faster to make it up.
  // Returns the delay at the chunk's start and at its end, or null for a
  // chunk too late to show (he holds his pose meanwhile).
  lagFor(pts, seconds, speaker) {
    const start = speaker.contextTime(0);
    if (start !== this.utterance) {
      this.utterance = start;
      this.lag = 0;
    }
    const at = speaker.contextTime(pts);
    if (at === null) return [this.lag, this.lag];
    const needed = Math.max(0, speaker.ctx.currentTime + LATE_MARGIN_S - at);
    if (needed > MAX_LAG_S) return null;
    const first = Math.max(this.lag, needed);
    this.lag = Math.max(needed, first - CATCH_UP * seconds);
    return [first, this.lag];
  }

  // payload: [f64 BE pts][u32 BE fps][u32 BE n][poses n*165][expr n*100][trans n*3]
  add(payload, speaker) {
    const view = new DataView(payload);
    const pts = view.getFloat64(0);
    const fps = view.getUint32(8) || 30;
    const n = view.getUint32(12);
    const floats = new Float32Array(payload.slice(16));
    if (!speaker.ctx || floats.length < n * (POSE_DIM + EXPR_DIM + 3)) return;

    const face = floats.subarray(n * POSE_DIM, n * (POSE_DIM + EXPR_DIM));
    const thinking = face.every((x) => x === 0);
    if (thinking && (pts === 0 || this.thinkBase === null)) {
      this.thinkBase = speaker.ctx.currentTime + 0.05 - pts; // a new episode
    }
    const lags = thinking ? [0, 0] : this.lagFor(pts, n / fps, speaker);
    if (lags === null) return;
    const [lag0, lag1] = lags;
    const frames = thinking ? this.thinking : this.speech;

    const axis = new THREE.Vector3();
    const q = new THREE.Quaternion();
    for (let i = 0; i < n; i++) {
      const at = thinking
        ? this.thinkBase + pts + i / fps
        : speaker.contextTime(pts + i / fps);
      if (at === null) continue; // no voice to sync to
      const time = at + lag0 + ((lag1 - lag0) * i) / Math.max(1, n - 1);
      const quats = new Float32Array(SMPLX_JOINTS.length * 4);
      for (let j = 0; j < SMPLX_JOINTS.length; j++) {
        const o = i * POSE_DIM + j * 3;
        axis.set(floats[o], floats[o + 1], floats[o + 2]);
        const angle = axis.length();
        if (angle > 1e-8) q.setFromAxisAngle(axis.divideScalar(angle), angle);
        else q.identity();
        q.toArray(quats, j * 4);
      }
      frames.push({ time, quats });
    }
    frames.sort((a, b) => a.time - b.time);
  }

  // Interpolated joint quaternions at `time`, or null when not gesturing.
  sample(time, speaking) {
    return (
      this.sampleOf(this.thinking, time, GESTURE_HOLD_S) ||
      this.sampleOf(this.speech, time, speaking ? SPEAKING_HOLD_S : GESTURE_HOLD_S)
    );
  }

  sampleOf(frames, time, hold) {
    while (frames.length > 2 && frames[1].time < time - hold - 1) frames.shift();
    if (!frames.length) return null;
    const last = frames[frames.length - 1];
    if (time < frames[0].time || time > last.time + hold) return null;

    let lo = 0;
    let hi = frames.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (frames[mid].time <= time) lo = mid;
      else hi = mid - 1;
    }
    const a = frames[lo];
    const b = frames[Math.min(lo + 1, frames.length - 1)];
    const alpha =
      b.time > a.time ? Math.min(1, Math.max(0, (time - a.time) / (b.time - a.time))) : 0;
    for (let k = 0; k < this.out.length; k += 4) {
      THREE.Quaternion.slerpFlat(this.out, k, a.quats, k, b.quats, k, alpha);
    }
    return this.out;
  }
}

// SMPL-X rotations onto the Mixamo rig. SMPL-X's rest pose is identity, so a
// joint's chained (global) SMPL-X rotation is its world-space delta from rest;
// applied on top of the bone's rest world orientation it gives the bone's
// target world orientation, from which the local one follows.
function retargetRig(model) {
  const bones = [];
  const byJoint = {};
  for (const [joint, parent, mixamo] of BODY) {
    const bone = model.getObjectByName(`mixamorig${mixamo}`);
    if (!bone) continue;
    const entry = {
      joint,
      parent,
      bone,
      index: JOINT_INDEX[joint] * 4,
      rest: bone.getWorldQuaternion(new THREE.Quaternion()),
    };
    bones.push(entry);
    byJoint[joint] = entry;
  }
  const hips = byJoint.pelvis && byJoint.pelvis.bone;
  const rootParent = new THREE.Quaternion();
  if (hips && hips.parent) hips.parent.getWorldQuaternion(rootParent);
  const rootParentInv = rootParent.clone().invert();

  const smplxGlobal = {};
  const desired = {};
  for (const name of SMPLX_JOINTS) {
    smplxGlobal[name] = new THREE.Quaternion();
    desired[name] = new THREE.Quaternion();
  }
  const local = new THREE.Quaternion();
  const parentInv = new THREE.Quaternion();
  const target = new THREE.Quaternion();

  return {
    bones,
    apply(quats, ease, maxStep = Infinity, slowEase = ease) {
      // BODY is parents-first, and a joint without a bone in this rig leaves
      // its children unanimated rather than wrongly chained.
      for (const { joint, parent, bone, index, rest } of bones) {
        if (PLANTED.has(joint)) local.identity();
        else local.fromArray(quats, index);
        if (parent === null) local.slerp(IDENTITY, 1 - PELVIS_GAIN);
        if (parent === null) smplxGlobal[joint].copy(local);
        else if (byJoint[parent]) smplxGlobal[joint].copy(smplxGlobal[parent]).multiply(local);
        else continue;
        desired[joint].copy(smplxGlobal[joint]).multiply(rest);

        if (parent === null) parentInv.copy(rootParentInv);
        else parentInv.copy(desired[parent]).invert();
        target.copy(parentInv).multiply(desired[joint]);
        const k = SLOW_JOINTS.has(joint) ? Math.min(ease, slowEase) : ease;
        const step = Math.min(bone.quaternion.angleTo(target) * k, maxStep);
        bone.quaternion.rotateTowards(target, step);
      }
    },
  };
}

const IDENTITY = new THREE.Quaternion();

// Mouse-Man's torso, in his Spine1 bone's frame, as horizontal elliptic
// slices stacked from his hips to his chest, fitted to the vertices the hips
// and spine carry: it narrows at the armpits, where the arms hang close.
// keepArmsOut() swings an upper arm about the shoulder so that the elbow,
// forearm and hand stay outside; the swing builds up fast and lets go slowly,
// so it never jerks.
function torsoGuard(model) {
  const bone = (name) => model.getObjectByName(`mixamorig${name}`);
  const spine = bone("Spine1");
  const carriers = new Set(["Hips", "Spine", "Spine1", "Spine2"].map((n) => `mixamorig${n}`));
  model.updateMatrixWorld(true);
  const toSpine = spine.matrixWorld.clone().invert();
  const v = new THREE.Vector3();
  const points = [];
  model.traverse((mesh) => {
    if (!mesh.isSkinnedMesh) return;
    const { skinIndex, skinWeight, position } = mesh.geometry.attributes;
    for (let i = 0; i < position.count; i++) {
      let k = 0; // the bone carrying this vertex most
      for (let j = 1; j < 4; j++) if (skinWeight.getComponent(i, j) > skinWeight.getComponent(i, k)) k = j;
      const carrier = mesh.skeleton.bones[skinIndex.getComponent(i, k)];
      if (!carrier || !carriers.has(carrier.name)) continue;
      points.push(mesh.getVertexPosition(i, v).applyMatrix4(mesh.matrixWorld).applyMatrix4(toSpine).clone());
    }
  });
  const ys = points.map((p) => p.y);
  const yLo = Math.min(...ys);
  const step = (Math.max(...ys) - yLo) / TORSO_SLICES || 1;
  const slices = Array.from({ length: TORSO_SLICES }, () => ({ x0: Infinity, x1: -Infinity, z0: Infinity, z1: -Infinity }));
  for (const p of points) {
    const c = slices[Math.min(TORSO_SLICES - 1, Math.floor((p.y - yLo) / step))];
    c.x0 = Math.min(c.x0, p.x);
    c.x1 = Math.max(c.x1, p.x);
    c.z0 = Math.min(c.z0, p.z);
    c.z1 = Math.max(c.z1, p.z);
  }
  // Centre and radii of each slice; an empty one borrows its neighbour's.
  const shape = slices.map((c) =>
    c.x0 === Infinity ? null : [(c.x0 + c.x1) / 2, (c.x1 - c.x0) / 2, (c.z0 + c.z1) / 2, (c.z1 - c.z0) / 2],
  );
  for (let i = 1; i < shape.length; i++) shape[i] = shape[i] || shape[i - 1];
  for (let i = shape.length - 2; i >= 0; i--) shape[i] = shape[i] || shape[i + 1];
  const stats = { frames: 0, pushed: 0, maxPush: 0 };

  // The torso's cross-section at height y, interpolated between slices.
  const cut = [0, 0, 0, 0];
  function section(y) {
    const f = Math.min(TORSO_SLICES - 1, Math.max(0, (y - yLo) / step - 0.5));
    const i = Math.floor(f);
    const j = Math.min(TORSO_SLICES - 1, i + 1);
    for (let k = 0; k < 4; k++) cut[k] = shape[i][k] + (shape[j][k] - shape[i][k]) * (f - i);
    return cut;
  }
  // < 1 inside the torso (spine frame); 1 on its skin.
  function level(p) {
    if (p.y < yLo || p.y > yLo + step * TORSO_SLICES) return Infinity;
    const [cx, rx, cz, rz] = section(p.y);
    return ((p.x - cx) / rx) ** 2 + ((p.z - cz) / rz) ** 2;
  }

  const arms = ["Left", "Right"].map((side) => ({
    arm: bone(`${side}Arm`),
    joints: [bone(`${side}ForeArm`), bone(`${side}Hand`)],
    push: 0, // the swing applied, radians
    axis: new THREE.Vector3(),
    base: null, // the arm's rotation before the swing
  }));
  const shoulder = new THREE.Vector3();
  const normal = new THREE.Vector3();
  const turn = new THREE.Quaternion();
  const world = new THREE.Quaternion();
  const parent = new THREE.Quaternion();
  const spineQ = new THREE.Quaternion();
  const toWorld = new THREE.Matrix4();
  let tested = [];

  function clear(axis, angle) {
    turn.setFromAxisAngle(axis, angle);
    return tested.every((p) => level(v.copy(p).sub(shoulder).applyQuaternion(turn).add(shoulder)) >= 1);
  }

  return {
    stats,
    arms,
    enabled: true,
    level,
    release() {
      for (const side of arms) if (side.base) side.arm.quaternion.copy(side.base);
    },
    forget() {
      for (const side of arms) {
        side.base = null;
        side.push = 0;
        side.axis.set(0, 0, 0);
      }
    },
    keepArmsOut(dt) {
      if (!this.enabled || !spine) return;
      model.updateMatrixWorld(true);
      toSpine.copy(spine.matrixWorld).invert();
      toWorld.copy(spine.matrixWorld);
      spine.getWorldQuaternion(spineQ);
      stats.frames += 1;
      for (const side of arms) {
        const { arm, joints } = side;
        if (!arm || joints.some((j) => !j)) continue;
        // Elbow, mid-forearm and wrist, in the spine's frame.
        arm.getWorldPosition(shoulder).applyMatrix4(toSpine);
        const [elbow, hand] = joints.map((j) => j.getWorldPosition(new THREE.Vector3()).applyMatrix4(toSpine));
        tested = [elbow, elbow.clone().lerp(hand, 0.5), hand];

        // Out, horizontally, from the torso's axis at the hand's height.
        const [cx, rx, cz, rz] = section(hand.y);
        normal.set((hand.x - cx) / rx ** 2, 0, (hand.z - cz) / rz ** 2);
        // Turned gradually: deep in the belly the outward direction can flip
        // from one frame to the next, and the arm with it.
        const axis = v.copy(hand).sub(shoulder).cross(normal);
        if (axis.lengthSq() > 1e-12) {
          axis.normalize();
          if (side.axis.lengthSq() === 0) side.axis.copy(axis);
          else side.axis.lerp(axis, 1 - Math.exp(-dt / AXIS_SMOOTH_S)).normalize();
        }

        let needed = 0;
        if (!clear(side.axis, 0)) {
          let lo = 0;
          let hi = MAX_PUSH_RAD;
          for (let i = 0; i < 10; i++) {
            const mid = (lo + hi) / 2;
            if (clear(side.axis, mid)) hi = mid;
            else lo = mid;
          }
          needed = hi;
        }
        side.needed = needed;
        side.push = needed > side.push
          ? Math.min(needed, side.push + PUSH_IN_RAD_S * dt)
          : Math.max(needed, side.push - PUSH_OUT_RAD_S * dt);
        if (side.push <= 0) {
          side.base = null;
          continue;
        }

        // The swing, from the spine's frame to the world.
        side.base = (side.base || new THREE.Quaternion()).copy(arm.quaternion);
        turn.setFromAxisAngle(normal.copy(side.axis).applyQuaternion(spineQ), side.push);
        arm.getWorldQuaternion(world).premultiply(turn);
        arm.parent.getWorldQuaternion(parent).invert();
        arm.quaternion.copy(parent.multiply(world));
        arm.updateMatrixWorld(true);
        stats.pushed += 1;
        stats.maxPush = Math.max(stats.maxPush, side.push);
      }
    },
  };
}

// Camera heights and distances: head and shoulders, or waist up for gestures.
// Framed from the bones: a cartoon head is far bigger than human proportions.
function cameraShots(model, height) {
  const y = (name, fallback) => {
    const bone = model.getObjectByName(name);
    return bone ? bone.getWorldPosition(new THREE.Vector3()).y : fallback;
  };
  const top = y("mixamorigHeadTop_End", height);
  const neck = y("mixamorigNeck", height * 0.8);
  const hips = y("mixamorigHips", height * 0.45);
  const fit = (visible) => visible / 2 / Math.tan(THREE.MathUtils.degToRad(25 / 2));

  const close = (top - neck) * 1.75;
  // Room above for leaning and below for the hands.
  const wide = (top - hips) * 1.45;
  return {
    close: { y: (neck + top) / 2 - (top - neck) * 0.08, distance: fit(close) },
    wide: { y: (hips + top) / 2 + (top - hips) * 0.02, distance: fit(wide) },
  };
}

// Mouse-Man between gestures: alive the way a person standing still is, not
// a loop. Every behaviour draws its next goal at random, at a random time,
// and gets there on a critically damped spring (no snaps, no fixed rhythm):
//   * breathing, a breath at a time, chest and shoulders rising;
//   * gaze: mostly at you, now and then a glance aside;
//   * posture: the weight shifts, the torso leans and turns a little;
//   * arms: hang loose, swing and bend a little, fingers relaxed;
//   * listening (you talk): leans in, tilts the head, nods when you pause;
//   * talking (HuRI speaks without gestures): leans in, the head follows the
//     voice.
// Offsets are rotations about world axes (x: lean forward / nod, y: turn to
// his left, z: tilt to his left), in each bone's parent frame at rest.
const IDLE_DT_MAX = 0.05; // a hidden tab's long frame must not kick the springs
const LISTEN_S = 0.6; // the visitor still talks if heard this recently
const PAUSE_NOD_S = 0.35; // a pause this long gets a nod (sometimes)

function idleLife(model, idle) {
  const rng = (lo, hi) => lo + Math.random() * (hi - lo);
  const pick = (p) => Math.random() < p;
  const spring = (w) => ({ x: 0, v: 0, goal: 0, w });
  const step = (s, dt) => {
    s.v += (s.w * s.w * (s.goal - s.x) - 2 * s.w * s.v) * dt;
    s.x += s.v * dt;
    return s.x;
  };

  model.updateMatrixWorld(true);
  const joints = new Map(); // bone -> {rest, frame, frameInv}
  const bone = (name) => {
    const b = model.getObjectByName(`mixamorig${name}`);
    if (!b || !idle.has(b)) return null;
    if (!joints.has(b)) {
      const frame = b.parent.getWorldQuaternion(new THREE.Quaternion());
      joints.set(b, { rest: idle.get(b), frame, frameInv: frame.clone().invert() });
    }
    return b;
  };
  const B = {
    spine: bone("Spine"), spine1: bone("Spine1"), spine2: bone("Spine2"),
    neck: bone("Neck"), head: bone("Head"),
  };
  const sides = [["Left", 1], ["Right", -1]].map(([Side, s]) => ({
    s,
    collar: bone(`${Side}Shoulder`),
    arm: bone(`${Side}Arm`),
    forearm: bone(`${Side}ForeArm`),
    hand: bone(`${Side}Hand`),
    fingers: ["Index", "Middle", "Ring", "Pinky"].flatMap((f) =>
      [1, 2, 3].map((k) => [bone(`${Side}Hand${f}${k}`), k]),
    ),
    swing: spring(3), out: spring(3), bend: spring(3.5), wrist: spring(4),
    next: rng(1, 4),
  }));

  const gaze = { yaw: spring(9), pitch: spring(9), roll: spring(5), next: 1 };
  const neckLag = { yaw: spring(5), pitch: spring(5) };
  const posture = { lean: spring(1.6), turn: spring(1.6), tilt: spring(1.6), next: 3 };
  const listenLean = spring(2.5);
  const nod = spring(14);
  let nodLeft = 0; // seconds of the nod being played
  let breath = Math.random();
  let breathRate = rng(0.2, 0.28);
  let wasListening = false;
  let lastHeard = null;
  let pauseNodded = true;

  const euler = new THREE.Euler(0, 0, 0, "YXZ");
  const offset = new THREE.Quaternion();
  const goal = new THREE.Quaternion();
  const targets = new Map();
  function pose(b, x, y, z) {
    if (!b) return;
    const { rest, frame, frameInv } = joints.get(b);
    offset.setFromEuler(euler.set(x, y, z));
    goal.copy(frameInv).multiply(offset).multiply(frame).multiply(rest);
    targets.set(b, (targets.get(b) || new THREE.Quaternion()).copy(goal));
  }

  return {
    // Advance by dt; `talk` is HuRI's voice (0..1), `heardAt` when the
    // visitor last spoke (performance.now() ms, or null).
    update(dt, talk, heardAt) {
      dt = Math.min(dt, IDLE_DT_MAX);
      const listening = heardAt !== null && performance.now() - heardAt < LISTEN_S * 1000;
      const attentive = listening || talk > 0.05;

      // Gaze: hold, then look somewhere else; at you while someone talks.
      if ((gaze.next -= dt) <= 0) {
        const aside = !attentive && pick(0.3);
        gaze.yaw.goal = aside ? rng(0.12, 0.3) * (pick(0.5) ? 1 : -1) : rng(-0.07, 0.07);
        gaze.pitch.goal = aside ? rng(-0.08, 0.1) : rng(-0.04, 0.05);
        gaze.roll.goal = rng(-0.04, 0.04);
        // Talking, the head moves more often.
        gaze.next = aside ? rng(0.7, 1.6) : talk > 0.05 ? rng(0.8, 2) : rng(1.5, 4.5);
      }
      if (listening && !wasListening) {
        gaze.yaw.goal = rng(-0.04, 0.04);
        gaze.roll.goal = rng(0.05, 0.1) * (pick(0.5) ? 1 : -1); // a curious tilt
        gaze.next = rng(2, 4);
      }
      // A nod, often, when the visitor pauses; now and then while they talk.
      if (heardAt !== lastHeard) {
        lastHeard = heardAt; // still talking: the next pause is a new one
        pauseNodded = false;
      }
      const quiet = heardAt === null ? Infinity : (performance.now() - heardAt) / 1000;
      if (!pauseNodded && quiet > PAUSE_NOD_S && quiet < 2 * LISTEN_S) {
        pauseNodded = true;
        if (pick(0.7) && nodLeft <= 0) nodLeft = rng(0.45, 0.7);
      }
      if (listening && nodLeft <= 0 && pick(dt / 3)) nodLeft = rng(0.35, 0.5);
      wasListening = listening;
      if (nodLeft > 0) nodLeft -= dt;
      nod.goal = nodLeft > 0 ? 0.09 : 0;

      // Posture: shift the weight every few seconds.
      if ((posture.next -= dt) <= 0) {
        posture.lean.goal = rng(-0.02, 0.03);
        posture.turn.goal = rng(-0.07, 0.07);
        posture.tilt.goal = rng(-0.035, 0.035);
        posture.next = rng(3.5, 8);
      }
      listenLean.goal = attentive ? 0.045 : 0;

      // Breathing: a breath at a time, each a little different.
      breath += breathRate * dt;
      if (breath >= 1) {
        breath -= 1;
        breathRate = rng(0.2, 0.3);
      }
      const inhale = 0.5 - 0.5 * Math.cos(breath * 2 * Math.PI);

      const lean = step(posture.lean, dt) + step(listenLean, dt);
      const turn = step(posture.turn, dt);
      const tilt = step(posture.tilt, dt);
      const yaw = step(gaze.yaw, dt);
      const pitch = step(gaze.pitch, dt) + step(nod, dt) + talk * 0.05;
      const roll = step(gaze.roll, dt);
      neckLag.yaw.goal = yaw;
      neckLag.pitch.goal = pitch;
      const neckYaw = step(neckLag.yaw, dt);
      const neckPitch = step(neckLag.pitch, dt);

      pose(B.spine, lean * 0.4, turn * 0.3, tilt * 0.5);
      pose(B.spine1, lean * 0.3, turn * 0.3, tilt * 0.3);
      pose(B.spine2, lean * 0.3 - inhale * 0.03, turn * 0.4, tilt * 0.2);
      // The head turns first, the neck follows; together they undo the
      // torso's turn and tilt, so he keeps looking where he means to.
      pose(B.neck, neckPitch * 0.4, neckYaw * 0.4 - turn * 0.5, -tilt * 0.5);
      pose(B.head, pitch * 0.6 - lean * 0.5 + inhale * 0.012, yaw * 0.6 - turn * 0.5, roll - tilt * 0.5);

      for (const side of sides) {
        if ((side.next -= dt) <= 0) {
          side.swing.goal = rng(-0.1, 0.05);
          side.out.goal = rng(0, 0.08);
          side.bend.goal = pick(0.3) ? rng(0.3, 0.55) : rng(0.05, 0.25);
          side.wrist.goal = rng(-0.15, 0.15);
          side.next = rng(3, 8);
        }
        const { s } = side;
        const swing = step(side.swing, dt) - lean * 0.8; // arms hang plumb
        pose(side.collar, 0, 0, s * inhale * 0.035);
        pose(side.arm, swing, 0, s * step(side.out, dt));
        pose(side.forearm, -step(side.bend, dt), 0, 0);
        pose(side.hand, step(side.wrist, dt) * 0.3, 0, -s * step(side.wrist, dt) * 0.5);
        for (const [finger, k] of side.fingers) pose(finger, 0, 0, -s * [0, 0.3, 0.4, 0.3][k]);
      }
    },
    // Ease every idle bone toward its pose (the rest one if not posed).
    apply(ease) {
      for (const [b, rest] of idle) b.quaternion.slerp(targets.get(b) || rest, ease);
    },
  };
}

// Mixamo rigs come in a T-pose: swing an upper arm down along the body.
function lowerArm(model, armName, forearmName, side) {
  const arm = model.getObjectByName(armName);
  const forearm = model.getObjectByName(forearmName);
  if (!arm || !forearm) return;
  model.updateMatrixWorld(true);

  const from = forearm
    .getWorldPosition(new THREE.Vector3())
    .sub(arm.getWorldPosition(new THREE.Vector3()))
    .normalize();
  const to = new THREE.Vector3(side * 0.22, -1, 0.08).normalize();
  const swing = new THREE.Quaternion().setFromUnitVectors(from, to);

  const world = swing.multiply(arm.getWorldQuaternion(new THREE.Quaternion()));
  const parent = arm.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
  arm.quaternion.copy(parent.multiply(world));
}

// Blend shapes can be spread over several meshes (head, teeth, eyelashes):
// set(name, weight) updates every mesh that has that shape.
function faceControls(model) {
  const targets = [];
  model.traverse((obj) => {
    if (obj.isMesh && obj.morphTargetDictionary) targets.push(obj);
  });
  return {
    set(name, weight) {
      for (const mesh of targets) {
        const i = mesh.morphTargetDictionary[name];
        if (i !== undefined) mesh.morphTargetInfluences[i] = weight;
      }
    },
  };
}

main().catch((err) => {
  console.error("[avatar] could not load Mouse-Man:", err);
  container.hidden = true;
  document.body.classList.remove("has-avatar");
});
