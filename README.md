# HuRI-Playground

A public web page to talk with [HuRI](https://github.com/Lucy-Robotics/huri-core#huri-ecosystem),
then rate it: visitors pick the modules HuRI runs for them, have a short
conversation (typed, or spoken into the microphone), hear HuRI's voice and
watch Mouse-Man gesture, and answer the Godspeed (5-point) and RoSAS (7-point)
questionnaires. It installs as the `huri-playground` package and command.

HuRI-Playground is a **use case** of HuRI, not a part of it, like
[HuRI-CLI](https://github.com/Lucy-Robotics/HuRI-CLI) (the reference client)
or the [acceptance test site](https://github.com/Lucy-Robotics/HuRI_ATP_testsite).
It uses nothing but the public client SDK of
[huri-core](https://github.com/Lucy-Robotics/huri-core), pinned by version
range, and it has its own versions and releases.

## How it works

The playground is a gateway that runs as its own process in front of a running
HuRI server; the browser never connects to HuRI directly. HuRI builds whatever
pipeline the first message of a session describes and has no notion of a
visitor, so the gateway owns what a public page needs:

- **the pipeline**: the browser ticks module names among those HuRI reports on
  `GET /modules`, so the list follows whatever this HuRI runs. Ticking a module
  ticks what feeds it (`tts` brings `rag`, `stt` brings `vad`), and the page
  says what the pipeline does before it starts. The browser sends module names
  only: labels, args and the few options a visitor may set are in
  [huri_playground/modules.yaml](huri_playground/modules.yaml), and the
  gateway refuses a pipeline in which a module's input can never arrive;
- **the cost**: a cap on concurrent sessions and on session length;
- **abuse**: an optional shared access code, a topic allow-list, per-topic rate
  and size limits;
- **the questionnaire**: a session id handed out when the conversation starts
  is the one-time ticket to submit answers, so every response maps to a real
  conversation.

On the HuRI side the gateway is a client of the huri-core SDK (`HuRIClient`):
the SDK opens the websocket, sends the session handshake (the modules, the
topics the browser `send`s: `huri.question` and `huri.audio.in`; the topics it
`subscribe`s to: among `huri.token`, `huri.audio.out`, `huri.motion`,
`huri.transcript`, `huri.question`, `huri.emotion`, those the pipeline
produces) and frames every event. The gateway relays the events without
reading them: each subscribed topic is decoded as the raw JSON object or the
raw bytes and framed again for the browser, unchanged, so it needs no event
package. The browser-side protocol is documented at the top of
[huri_playground/gateway.py](huri_playground/gateway.py).

With `vad` the page offers a microphone (it needs `https` or `localhost`).
With `tts` it plays HuRI's voice and, if the Mouse-Man model is present, shows
him above the conversation; with `mov` (EMAGE) he gestures with the speech,
without it he only idles, nodding with the voice. With `emo` and `eag` the
page shows the emotion HuRI reads from the voice, and the visitor chooses
whether HuRI answers in parallel with reading it (`qag`'s `mode: parallel`,
the default: an emotion read after its question left goes with the next
question, flagged `emotion_earlier`) or waits for it (`sequential`).

## Install

In a virtual environment (Python 3.10 to 3.12), from git at a ref (a release
tag, or `dev`). huri-core is not on PyPI: give it as a git URL on the same
command line, so pip never looks it up on an index:

```sh
pip install \
  "huri-core[client] @ git+https://github.com/Lucy-Robotics/huri-core.git@<core-ref>" \
  "huri-playground @ git+https://github.com/Lucy-Robotics/HuRI-Playground.git@<ref>"
```

Pick refs that match the table in
[Versions and compatibility](#versions-and-compatibility). The server side is a
HuRI set up with [huri-modules](https://github.com/Lucy-Robotics/huri-modules)
(`install.sh`, Docker or Helm) running the modules you want to offer.

### Working on huri-playground

```sh
pip install "huri-core[client] @ git+https://github.com/Lucy-Robotics/huri-core.git@dev"
pip install -e ".[dev]"
pytest -q
black --check huri_playground tests scripts && isort --check-only huri_playground tests scripts
flake8 huri_playground tests scripts && mypy huri_playground scripts --check-untyped-defs
```

A local checkout works as well: `pip install -e "../huri-core[client]"`. The
tests never reach the network: HuRI is a fake behind the SDK's websocket. With
huri-modules' packages installed next to it (`pip install -e
../huri-modules/packages/huri-module-rag` and so on), the tests also check the
args of `modules.yaml` against the modules' real signatures; without them that
check is skipped. `python scripts/check_modules_yaml.py <registry.toml>` checks
the module names of `modules.yaml` against huri-modules' registry (CI does,
at `HURI_MODULES_REF`).

## Usage

Start a HuRI server first, then:

```sh
HURI_URL=ws://localhost:8000/session huri-playground   # http://127.0.0.1:8080
```

(`python -m huri_playground` is the same.) Put the page behind a reverse proxy
that terminates TLS: the microphone needs `https`, and HuRI's own port should
stay off the internet.

| Variable                     | Default                       | Meaning                                                  |
| ---------------------------- | ----------------------------- | -------------------------------------------------------- |
| `HURI_URL`                   | `ws://localhost:8000/session` | HuRI's session endpoint (keep it off the internet)       |
| `PLAYGROUND_ACCESS_CODE`     | empty (no code)               | Shared code visitors must type before starting           |
| `PLAYGROUND_MAX_SESSIONS`    | `2`                           | Concurrent conversations; others are told to wait        |
| `PLAYGROUND_SESSION_SECONDS` | `300`                         | Conversation length before it moves to the questions     |
| `PLAYGROUND_QUESTIONNAIRES`  | `godspeed,rosas`              | Which questionnaires to show, in order                   |
| `PLAYGROUND_MODULES`         | empty (all HuRI runs)         | Modules visitors may pick, e.g. `rag,tts`                |
| `PLAYGROUND_DATA_DIR`        | `playground_data`             | Where `responses.jsonl` is written                       |
| `PLAYGROUND_HOST` / `_PORT`  | `127.0.0.1` / `8080`          | Listen address                                           |

`GET /modules` is asked on the HuRI server next to `HURI_URL`
(`http://localhost:8000/modules` for the default) and again every 10 seconds
at most, so a HuRI redeployed with other modules changes the page without a
restart. Its `details` give each module's input and output topics, which is
what the gateway wires with; a HuRI older than huri-core 0.4 has no such
route and the page reports HuRI as offline.

Each line of `responses.jsonl` holds one visitor's answers, the per-dimension
scores (Godspeed: anthropomorphism, animacy, likeability, perceived
intelligence, perceived safety; RoSAS: warmth, competence, discomfort), the
conversation length, number of questions, the `modules` the visitor picked
and the `options` they set (`qag`'s mode), but not what the visitor wrote.
Questionnaire items are in
[huri_playground/questionnaires/](huri_playground/questionnaires/).

### The Mouse-Man model

The 3D model (30 MB) is not committed: copy `frontend/public/model.fbx` from
[HuRI_website_demo](https://github.com/Lucy-Robotics/HuRI_website_demo) (Git
LFS) to `huri_playground/static/avatar/model.fbx`. Without it the page is text
only. That model has no mouth rig, so his mouth does not move. EMAGE also runs
on AMD GPUs with the ROCm PyTorch build (measured on an RX 7700S: 0.07 s per
3 s of speech), although huri-modules' installer turns `mov` off on AMD.

### Comparing QAG's modes

[scripts/compare_qag_modes.py](scripts/compare_qag_modes.py) talks to a
running playground as a browser would, streams a spoken sentence as microphone
frames in both of `qag`'s modes and measures what a person waits for, from the
moment they stop talking to the first sound of HuRI's answer, with where the
time went. `python scripts/compare_qag_modes.py --help`; it needs `numpy`,
`websockets`, `webrtcvad` and, to read a sentence aloud instead of replaying a
recording, Piper (`piper-tts`) and a voice in `HURI_PIPER_VOICE`.

## Versions and compatibility

huri-playground follows semver on its command line, its environment variables
and the keys of `modules.yaml`. Releases are git tags `X.Y.Z` (no `v`) on
`main`; `dev` carries the next version. See [RELEASING.md](RELEASING.md).

| huri-playground | huri-core | huri-modules (the server) |
|---|---|---|
| 0.1.x | 0.4.x (`>=0.4,<0.5`) | any 0.1.x set serves the page; showing the emotion (`huri.emotion`) and the `emotion_earlier` flag of `huri.question` need its huri-events >= 0.2 |

CI tests against huri-core at the ref set by the `HURI_CORE_REF` variable of
[ci.yml](.github/workflows/ci.yml), the release tag inside the
`huri-core[client]` range (`0.4.0`), and checks the module names of
`modules.yaml` against huri-modules' `registry.toml` at `HURI_MODULES_REF`
(`dev`).

## License

GPL-3.0, see [LICENSE](LICENSE).
