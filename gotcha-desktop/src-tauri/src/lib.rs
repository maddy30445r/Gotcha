// Gotcha desktop — native capture + upload.
//
// The capture core is the existing Swift `mac-recorder` binary; we spawn it and
// drive its tiny protocol (stdout "RECORDING" handshake; stop by writing a
// newline to stdin). The two WAVs are then uploaded to the backend's
// /api/upload with the user's bearer token. The webview (webapp/static) owns all
// UI; these commands are the only native pieces.

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{Emitter, Manager, State};
use tauri_plugin_deep_link::DeepLinkExt;

struct RecState {
    child: Child,
    stdin: ChildStdin,
    reader: BufReader<ChildStdout>,
    info: RecInfo,
}

type RecMutex = Mutex<Option<RecState>>;

#[derive(Serialize, Clone)]
struct RecInfo {
    base: String,
    system_path: String,
    mic_path: String,
}

fn recorder_bin() -> PathBuf {
    if let Ok(p) = std::env::var("GOTCHA_RECORDER_BIN") {
        return PathBuf::from(p);
    }
    // Bundled sidecar: Tauri places the externalBin next to the app executable
    // (Contents/MacOS/mac-recorder), so it ships inside Gotcha.app and runs with
    // Gotcha's own TCC identity. This is the path in a distributed build.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(side) = exe.parent().map(|d| d.join("mac-recorder")) {
            if side.exists() {
                return side;
            }
        }
    }
    // Dev fallback: the binary built in the repo (used by `tauri dev`).
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../mac_recorder/.build/release/mac-recorder")
}

fn recordings_dir() -> std::io::Result<PathBuf> {
    let d = std::env::temp_dir().join("gotcha-recordings");
    std::fs::create_dir_all(&d)?;
    Ok(d)
}

fn sanitize(name: &str) -> String {
    let s: String = name
        .chars()
        .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    let s = s.trim_matches('_').to_string();
    if s.is_empty() { "meeting".into() } else { s }
}

fn failure_message(code: i32) -> String {
    if code == 2 {
        "Permission needed: grant Microphone and Screen Recording to Gotcha in \
         System Settings → Privacy & Security, then fully quit and relaunch."
            .into()
    } else {
        format!("Recorder failed (exit {code}).")
    }
}

fn start_blocking(name: String) -> Result<RecState, String> {
    let dir = recordings_dir().map_err(|e| e.to_string())?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let base = format!("{}_{}", stamp, sanitize(&name));
    let system_path = dir.join(format!("{base}.system.wav"));
    let mic_path = dir.join(format!("{base}.mic.wav"));

    let bin = recorder_bin();
    if !bin.exists() {
        return Err(format!("recorder binary not found at {}", bin.display()));
    }

    let mut child = Command::new(&bin)
        .arg("--out-system")
        .arg(&system_path)
        .arg("--out-mic")
        .arg(&mic_path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("failed to launch recorder: {e}"))?;

    let stdin = child.stdin.take().ok_or("no stdin pipe")?;
    let mut reader = BufReader::new(child.stdout.take().ok_or("no stdout pipe")?);

    // Wait for the "RECORDING" handshake; an early EOF means the child exited
    // (e.g. a permission failure, exit code 2).
    let mut line = String::new();
    loop {
        line.clear();
        let n = reader.read_line(&mut line).map_err(|e| e.to_string())?;
        if n == 0 {
            let code = child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
            return Err(failure_message(code));
        }
        if line.trim() == "RECORDING" {
            break;
        }
    }

    let info = RecInfo {
        base,
        system_path: system_path.to_string_lossy().into_owned(),
        mic_path: mic_path.to_string_lossy().into_owned(),
    };
    Ok(RecState { child, stdin, reader, info })
}

fn stop_blocking(mut rec: RecState) -> Result<(), String> {
    // A newline on stdin (then EOF on drop) tells the recorder to flush + exit.
    let _ = rec.stdin.write_all(b"\n");
    let _ = rec.stdin.flush();
    drop(rec.stdin);

    // Drain the remaining stdout (the two saved paths) so the child can exit.
    let mut buf = String::new();
    while let Ok(n) = rec.reader.read_line(&mut buf) {
        if n == 0 {
            break;
        }
        buf.clear();
    }

    let status = rec.child.wait().map_err(|e| e.to_string())?;
    let code = status.code().unwrap_or(-1);
    if code != 0 {
        return Err(failure_message(code));
    }
    Ok(())
}

/// Downsample a 48 kHz mono PCM16 WAV to 16 kHz — the STT-native rate, so it's
/// effectively lossless for transcription — which cuts the upload payload ~3x.
/// macOS-only (uses the built-in `afconvert`); returns None on any failure so
/// the caller transparently falls back to uploading the original file.
fn downsample_16k(src: &str) -> Option<String> {
    let stem = std::path::Path::new(src)
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "track.wav".into());
    let out = std::env::temp_dir().join(format!("gotcha-16k-{stem}"));
    let out_str = out.to_string_lossy().into_owned();
    let ok = std::process::Command::new("afconvert")
        .args(["-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, &out_str])
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if ok {
        Some(out_str)
    } else {
        let _ = std::fs::remove_file(&out_str);
        None
    }
}

fn upload_blocking(
    server_url: String,
    token: String,
    name: String,
    system_path: String,
    mic_path: String,
    glossary: String,
    language: String,
    process: bool,
) -> Result<String, String> {
    let url = format!("{}/api/upload", server_url.trim_end_matches('/'));

    // Shrink the payload ~3x before sending by downsampling to 16 kHz. These are
    // temp copies; the original 48 kHz WAVs stay on disk until the server
    // confirms a durable write (so a failed upload is still retryable).
    let sys_up = downsample_16k(&system_path).unwrap_or_else(|| system_path.clone());
    let mic_up = downsample_16k(&mic_path).unwrap_or_else(|| mic_path.clone());

    let result = upload_files(&url, &token, &name, &sys_up, &mic_up, &glossary, &language, process);

    // Drop the temp downsampled copies (if any were made).
    if sys_up != system_path {
        let _ = std::fs::remove_file(&sys_up);
    }
    if mic_up != mic_path {
        let _ = std::fs::remove_file(&mic_up);
    }
    // Durably stored server-side now — drop the local originals. On failure we
    // keep them so the user can retry.
    if result.is_ok() {
        let _ = std::fs::remove_file(&system_path);
        let _ = std::fs::remove_file(&mic_path);
    }
    result
}

fn upload_files(
    url: &str,
    token: &str,
    name: &str,
    system_path: &str,
    mic_path: &str,
    glossary: &str,
    language: &str,
    process: bool,
) -> Result<String, String> {
    let client = reqwest::blocking::Client::new();

    // A flaky network shouldn't cost the user their recording. Retry transient
    // failures (dropped connection, 5xx, 429) with backoff. The caller deletes
    // the local WAVs ONLY after a confirmed 2xx, so giving up leaves them on
    // disk for the user to retry.
    let mut last_err = String::new();
    for attempt in 0..3u32 {
        // multipart::Form is consumed by send(), so rebuild it (and re-open the
        // files) on each attempt.
        let form = reqwest::blocking::multipart::Form::new()
            .text("name", name.to_string())
            .text("glossary", glossary.to_string())
            .text("language", language.to_string())
            .text("process", if process { "true" } else { "false" })
            .file("system", system_path)
            .map_err(|e| format!("reading system track: {e}"))?
            .file("mic", mic_path)
            .map_err(|e| format!("reading mic track: {e}"))?;

        match client.post(url).bearer_auth(token).multipart(form).send() {
            Ok(resp) => {
                let status = resp.status();
                let body = resp.text().unwrap_or_default();
                if status.is_success() {
                    let v: serde_json::Value =
                        serde_json::from_str(&body).map_err(|e| e.to_string())?;
                    return Ok(v.get("base").and_then(|b| b.as_str())
                        .unwrap_or("").to_string());
                }
                // 4xx other than 429 (e.g. 401 bad token, 413 too large) won't
                // clear on retry — fail fast with the server's message.
                if status.as_u16() < 500 && status.as_u16() != 429 {
                    return Err(format!("server {}: {}", status.as_u16(), body));
                }
                last_err = format!("server {}: {}", status.as_u16(), body);
            }
            Err(e) => last_err = format!("upload failed: {e}"),
        }
        if attempt < 2 {
            std::thread::sleep(std::time::Duration::from_secs(2u64.pow(attempt + 1)));
        }
    }
    Err(format!("upload failed after retries (your recording is kept locally — \
                 try again): {last_err}"))
}

#[tauri::command]
async fn start_recording(state: State<'_, RecMutex>, name: String) -> Result<RecInfo, String> {
    if state.lock().unwrap().is_some() {
        return Err("A recording is already in progress.".into());
    }
    let rec = tauri::async_runtime::spawn_blocking(move || start_blocking(name))
        .await
        .map_err(|e| e.to_string())??;
    let info = rec.info.clone();
    *state.lock().unwrap() = Some(rec);
    Ok(info)
}

#[tauri::command]
async fn stop_recording(state: State<'_, RecMutex>) -> Result<RecInfo, String> {
    let rec = state
        .lock()
        .unwrap()
        .take()
        .ok_or("No active recording to stop.")?;
    let info = rec.info.clone();
    tauri::async_runtime::spawn_blocking(move || stop_blocking(rec))
        .await
        .map_err(|e| e.to_string())??;
    Ok(info)
}

#[tauri::command]
async fn upload_recording(
    server_url: String,
    token: String,
    name: String,
    system_path: String,
    mic_path: String,
    glossary: String,
    language: String,
    process: bool,
) -> Result<String, String> {
    let base = tauri::async_runtime::spawn_blocking(move || {
        upload_blocking(server_url, token, name, system_path, mic_path, glossary, language, process)
    })
    .await
    .map_err(|e| e.to_string())??;
    Ok(base)
}

/// Open the exact macOS privacy pane for a permission ("mic" or "screen").
#[tauri::command]
fn open_privacy_pane(which: String) -> Result<(), String> {
    let anchor = match which.as_str() {
        "mic" => "Privacy_Microphone",
        "screen" => "Privacy_ScreenCapture",
        _ => "Privacy",
    };
    let url = format!("x-apple.systempreferences:com.apple.preference.security?{anchor}");
    std::process::Command::new("open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Open the hosted connect page in the system browser. If the browser already has a
/// web session the server links that account immediately; otherwise it shows sign-in.
/// Either way it ends by redirecting to gotcha://connect?server=…&token=…, which the
/// deep-link handler below binds — so this replaces pasting a token.
#[tauri::command]
fn open_signin(server_url: String) -> Result<(), String> {
    let base = server_url.trim_end_matches('/');
    let url = format!("{base}/api/auth/desktop/connect");
    std::process::Command::new("open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

// --- loopback sign-in (RFC 8252) -------------------------------------------
// Robust alternative to the gotcha:// deep link: start a localhost server, open the
// browser to the connect endpoint pointing back at it, and receive the token directly.
// No URL scheme → no "no application set to open the URL" error and no stale-handler
// ghosting. Blocks until the browser hits the callback or it times out.
fn pct_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

fn hexval(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn pct_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => match (hexval(bytes[i + 1]), hexval(bytes[i + 2])) {
                (Some(h), Some(l)) => {
                    out.push(h * 16 + l);
                    i += 3;
                }
                _ => {
                    out.push(bytes[i]);
                    i += 1;
                }
            },
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Pull token & state out of an HTTP request's first line `GET /callback?token=…&state=… …`.
fn parse_callback(req: &str) -> (String, String) {
    let path = req
        .lines()
        .next()
        .unwrap_or("")
        .split_whitespace()
        .nth(1)
        .unwrap_or("");
    let query = path.splitn(2, '?').nth(1).unwrap_or("");
    let (mut token, mut state) = (String::new(), String::new());
    for pair in query.split('&') {
        let mut it = pair.splitn(2, '=');
        match (it.next().unwrap_or(""), it.next().unwrap_or("")) {
            ("token", v) => token = pct_decode(v),
            ("state", v) => state = pct_decode(v),
            _ => {}
        }
    }
    (token, state)
}

fn loopback_signin_blocking(server_url: String) -> Result<String, String> {
    use std::io::Read;
    use std::net::TcpListener;
    use std::time::{Duration, Instant};

    let base = server_url.trim_end_matches('/');
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;

    // A nonce the backend echoes back, so we only accept our own callback.
    let nonce = format!(
        "{:x}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    let redirect = format!("http://127.0.0.1:{port}/callback");
    let url = format!(
        "{base}/api/auth/desktop/connect?redirect={}&state={}",
        pct_encode(&redirect),
        pct_encode(&nonce)
    );
    std::process::Command::new("open")
        .arg(&url)
        .spawn()
        .map_err(|e| e.to_string())?;

    // 5 min: enough for the slower email magic-link path, not just Google.
    let deadline = Instant::now() + Duration::from_secs(300);
    loop {
        if Instant::now() >= deadline {
            return Err("timed out waiting for sign-in".into());
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                let mut buf = [0u8; 4096];
                let n = stream.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]);
                let (token, state) = parse_callback(&req);
                let ok = !token.is_empty() && state == nonce;
                let body = if ok {
                    "<!doctype html><meta charset=utf-8><title>Gotcha</title>\
                     <body style='font-family:-apple-system,sans-serif;text-align:center;padding:60px'>\
                     <h2>You're signed in \u{2713}</h2><p>You can close this tab and return to Gotcha.</p>"
                } else {
                    "<!doctype html><meta charset=utf-8><title>Gotcha</title>\
                     <body style='font-family:-apple-system,sans-serif;text-align:center;padding:60px'>\
                     <h2>Sign-in failed</h2><p>Please try again from the Gotcha app.</p>"
                };
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(resp.as_bytes());
                let _ = stream.flush();
                if ok {
                    return Ok(token);
                }
                // non-matching request (e.g. favicon) — keep waiting
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(120));
            }
            Err(e) => return Err(e.to_string()),
        }
    }
}

/// Bring the main window to the foreground. Called when an auth token arrives so the
/// user is returned to the app instead of left staring at the browser. macOS:
/// set_focus does makeKeyAndOrderFront + app activation.
fn focus_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

#[tauri::command]
async fn start_loopback_signin(
    app: tauri::AppHandle,
    server_url: String,
) -> Result<String, String> {
    let res = tauri::async_runtime::spawn_blocking(move || loopback_signin_blocking(server_url))
        .await
        .map_err(|e| e.to_string())?;
    if res.is_ok() {
        focus_main(&app); // raise the window now that we're signed in
    }
    res
}

/// Open an external http(s) link in the system browser. The webview is locked to
/// the app shell (see app.js), so outbound links are routed here instead.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("refusing to open non-http(s) url".into());
    }
    std::process::Command::new("open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Relaunch the app — needed after granting Screen Recording (the grant only
/// takes effect on relaunch). `restart()` diverges (replaces the process).
#[tauri::command]
fn relaunch(app: tauri::AppHandle) {
    app.restart();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_deep_link::init())
        .manage(RecMutex::default())
        .setup(|app| {
            // gotcha://connect?server=…&token=… → forward each URL to the webview,
            // which parses it and saves the settings (zero-paste onboarding).
            let handle = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    let _ = handle.emit("deep-link", url.to_string());
                }
                focus_main(&handle); // a gotcha:// link arrived → bring the app forward
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            upload_recording,
            open_privacy_pane,
            open_signin,
            start_loopback_signin,
            open_external,
            relaunch
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
