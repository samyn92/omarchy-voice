//! The ear: microphone in, transcripts out.
//!
//! It owns the timing-critical half of voice control — capture, voice activity detection and
//! the fast recognizer — and publishes what it hears as JSON lines on a unix socket:
//!
//!   {"t":"level","v":0.42}                              every 25 ms, for the orb
//!   {"t":"speech"}                                      speech started
//!   {"t":"utterance","text":"scroll down","wav":"…","ms":26}
//!
//! The engine subscribes instead of holding the microphone itself. That keeps about a
//! gigabyte of Python models out of memory, and it is why a command is understood in roughly
//! 25 ms instead of 150.
//!
//! It listens only while the engine's `listening` file exists and closes the microphone the
//! moment it does not: a process that runs all day must not keep an audio device awake.

use std::io::Write;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use earshot::Detector;
use sherpa_onnx::{OfflineRecognizer, OfflineRecognizerConfig, OfflineTransducerModelConfig};

const RATE: u32 = 16_000;
const FRAME: usize = 256; // 16 ms at 16 kHz — the frame size earshot wants
const LEVEL_EVERY: Duration = Duration::from_millis(25);
const SPEECH_RMS: f32 = 0.12; // a normal speaking voice reads about 1.0
const NOISE_FLOOR: f32 = 0.004;
const PREROLL: usize = 20; // frames kept before speech starts (~320 ms), so no word is clipped
const HANGOVER: usize = 15; // frames of quiet that end an utterance (~240 ms)
const VOICED: f32 = 0.5; // earshot scores 0…1; over half is speech
const MAX_FRAMES: usize = 20 * 1000 / 16; // never record more than 20 s in one go

fn runtime_dir() -> PathBuf {
    let base = std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".into());
    Path::new(&base).join("omarchy-voice")
}

/// Everyone subscribed to the ear. A slow reader is skipped, never waited for: levels are
/// disposable and the audio thread must never block on a drawing program.
#[derive(Clone, Default)]
struct Subscribers(Arc<Mutex<Vec<UnixStream>>>);

impl Subscribers {
    fn publish(&self, line: &str) {
        let mut clients = match self.0.lock() {
            Ok(c) => c,
            Err(poisoned) => poisoned.into_inner(),
        };
        let payload = format!("{line}\n");
        clients.retain_mut(|client| {
            let _ = client.set_nonblocking(true);
            match client.write_all(payload.as_bytes()) {
                Ok(()) => true,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => true, // skip this one
                Err(_) => false,
            }
        });
    }

    fn serve(&self, path: &Path) -> Result<()> {
        let _ = std::fs::remove_file(path);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let listener = UnixListener::bind(path).with_context(|| format!("bind {path:?}"))?;
        let subscribers = self.clone();
        std::thread::spawn(move || {
            for client in listener.incoming().flatten() {
                if let Ok(mut clients) = subscribers.0.lock() {
                    clients.push(client);
                }
            }
        });
        Ok(())
    }
}

fn level_of(frame: &[f32]) -> f32 {
    if frame.is_empty() {
        return 0.0;
    }
    let sum: f32 = frame.iter().map(|s| s * s).sum();
    let rms = (sum / frame.len() as f32).sqrt();
    ((rms - NOISE_FLOOR) / SPEECH_RMS).clamp(0.0, 1.0)
}

/// Open the default input and hand back 16 kHz mono frames.
fn capture(tx: SyncSender<Vec<f32>>) -> Result<cpal::Stream> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| anyhow!("no input device"))?;
    let config = device.default_input_config()?;
    let in_rate = config.sample_rate();
    let channels = config.channels() as usize;
    let step = in_rate as f64 / RATE as f64;
    let mut pending: Vec<f32> = Vec::with_capacity(FRAME);
    let mut position = 0f64; // fractional read head, for the resample

    let stream = device.build_input_stream(
        config.config(),
        move |data: &[f32], _: &cpal::InputCallbackInfo| {
            // down-mix to mono, then take every `step`-th sample: the recognizer wants 16 kHz
            let mono: Vec<f32> = data
                .chunks(channels)
                .map(|f| f.iter().sum::<f32>() / channels as f32)
                .collect();
            while (position as usize) < mono.len() {
                pending.push(mono[position as usize]);
                position += step;
                if pending.len() == FRAME {
                    let _ = tx.try_send(std::mem::take(&mut pending));
                    pending = Vec::with_capacity(FRAME);
                }
            }
            position -= mono.len() as f64;
        },
        |err| eprintln!("[ear] audio: {err}"),
        None,
    )?;
    stream.play()?;
    Ok(stream)
}

struct Recognizer {
    inner: OfflineRecognizer,
}

impl Recognizer {
    fn load(model: &Path, threads: i32) -> Result<Self> {
        let mut config = OfflineRecognizerConfig::default();
        config.model_config.transducer = OfflineTransducerModelConfig {
            encoder: Some(model.join("encoder.int8.onnx").display().to_string()),
            decoder: Some(model.join("decoder.int8.onnx").display().to_string()),
            joiner: Some(model.join("joiner.int8.onnx").display().to_string()),
        };
        config.model_config.tokens = Some(model.join("tokens.txt").display().to_string());
        config.model_config.model_type = Some("nemo_transducer".into());
        config.model_config.num_threads = threads;
        let inner = OfflineRecognizer::create(&config)
            .ok_or_else(|| anyhow!("could not load the model in {model:?}"))?;
        Ok(Self { inner })
    }

    fn transcribe(&self, samples: &[f32]) -> String {
        let stream = self.inner.create_stream();
        stream.accept_waveform(RATE as i32, samples);
        self.inner.decode(&stream);
        stream
            .get_result()
            .map(|r| r.text.trim().to_string())
            .unwrap_or_default()
    }
}

fn write_wav(path: &Path, samples: &[f32]) -> Result<()> {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: RATE,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(path, spec)?;
    for s in samples {
        writer.write_sample((s.clamp(-1.0, 1.0) * i16::MAX as f32) as i16)?;
    }
    writer.finalize()?;
    Ok(())
}

/// One utterance at a time: collect frames while there is speech, hand them over when it stops.
struct Segmenter {
    vad: Box<Detector>,
    preroll: Vec<Vec<f32>>,
    utterance: Vec<f32>,
    quiet: usize,
    speaking: bool,
}

impl Segmenter {
    fn new() -> Self {
        Self {
            vad: Detector::default_boxed(),
            preroll: Vec::new(),
            utterance: Vec::new(),
            quiet: 0,
            speaking: false,
        }
    }

    /// Feed one frame. Returns (speech just started, the finished utterance if it just ended).
    fn push(&mut self, frame: &[f32]) -> (bool, Option<Vec<f32>>) {
        let voiced = self.vad.predict_f32(frame) > VOICED;

        if self.speaking {
            self.utterance.extend_from_slice(frame);
            self.quiet = if voiced { 0 } else { self.quiet + 1 };
            let too_long = self.utterance.len() > MAX_FRAMES * FRAME;
            if self.quiet >= HANGOVER || too_long {
                self.speaking = false;
                self.quiet = 0;
                return (false, Some(std::mem::take(&mut self.utterance)));
            }
            return (false, None);
        }

        if voiced {
            self.speaking = true;
            self.utterance = self.preroll.concat();
            self.utterance.extend_from_slice(frame);
            self.preroll.clear();
            return (true, None);
        }

        self.preroll.push(frame.to_vec());
        if self.preroll.len() > PREROLL {
            self.preroll.remove(0);
        }
        (false, None)
    }

    fn reset(&mut self) {
        self.preroll.clear();
        self.utterance.clear();
        self.quiet = 0;
        self.speaking = false;
    }
}

fn main() -> Result<()> {
    let runtime = runtime_dir();
    let spool = runtime.join("spool");
    std::fs::create_dir_all(&spool)?;
    let listening = runtime.join("listening");
    let socket = runtime.join("ear.sock");

    let model = std::env::var("OMARCHY_VOICE_MODEL")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let data = std::env::var("XDG_DATA_HOME").unwrap_or_else(|_| {
                format!("{}/.local/share", std::env::var("HOME").unwrap_or_default())
            });
            Path::new(&data).join("omarchy-voice/asr-models/parakeet-tdt-110m")
        });
    let threads: i32 = std::env::var("OMARCHY_VOICE_THREADS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(2);

    let started = Instant::now();
    let recognizer = Recognizer::load(&model, threads)?;
    eprintln!(
        "[ear] {} loaded in {} ms",
        model.file_name().unwrap_or_default().to_string_lossy(),
        started.elapsed().as_millis()
    );

    let subscribers = Subscribers::default();
    subscribers.serve(&socket)?;
    eprintln!("[ear] publishing on {}", socket.display());

    let mut segmenter = Segmenter::new();
    let mut stream: Option<cpal::Stream> = None;
    let mut frames: Option<Receiver<Vec<f32>>> = None;
    let mut last_level = Instant::now();
    let mut counter: u64 = 0;

    loop {
        let want = listening.exists();
        match (want, stream.is_some()) {
            (true, false) => {
                let (tx, rx) = sync_channel::<Vec<f32>>(64);
                match capture(tx) {
                    Ok(s) => {
                        stream = Some(s);
                        frames = Some(rx);
                        segmenter.reset();
                        eprintln!("[ear] microphone open");
                    }
                    Err(e) => {
                        eprintln!("[ear] cannot open the microphone: {e}");
                        std::thread::sleep(Duration::from_secs(2));
                    }
                }
            }
            (false, true) => {
                stream = None; // dropping it closes the device
                frames = None;
                segmenter.reset();
                subscribers.publish(r#"{"t":"idle"}"#);
                eprintln!("[ear] microphone closed");
            }
            _ => {}
        }

        let Some(rx) = frames.as_ref() else {
            std::thread::sleep(Duration::from_millis(200));
            continue;
        };

        let frame = match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(f) => f,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(_) => {
                stream = None;
                frames = None;
                continue;
            }
        };

        if last_level.elapsed() >= LEVEL_EVERY {
            last_level = Instant::now();
            subscribers.publish(&format!(r#"{{"t":"level","v":{:.3}}}"#, level_of(&frame)));
        }

        let (speech_started, finished) = segmenter.push(&frame);
        if speech_started {
            subscribers.publish(r#"{"t":"speech"}"#);
        }
        let Some(audio) = finished else { continue };
        if audio.len() < FRAME * 4 {
            continue; // a cough, not a command
        }

        let at = Instant::now();
        let text = recognizer.transcribe(&audio);
        let ms = at.elapsed().as_millis();
        counter += 1;
        let wav = spool.join(format!("utt-{counter:06}.wav"));
        if let Err(e) = write_wav(&wav, &audio) {
            eprintln!("[ear] could not write {wav:?}: {e}");
        }
        if let Some(old) = counter.checked_sub(8) {
            let _ = std::fs::remove_file(spool.join(format!("utt-{old:06}.wav")));
        }
        let line = serde_json::json!({
            "t": "utterance",
            "text": text,
            "wav": wav.display().to_string(),
            "ms": ms,
            "seconds": audio.len() as f32 / RATE as f32,
        });
        eprintln!("[ear] {ms:>3} ms  {text:?}");
        subscribers.publish(&line.to_string());
    }
}
