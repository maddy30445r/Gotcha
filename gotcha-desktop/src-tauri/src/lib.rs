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
use tauri::{Emitter, State};
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

fn upload_blocking(
    server_url: String,
    token: String,
    name: String,
    system_path: String,
    mic_path: String,
    glossary: String,
    process: bool,
) -> Result<String, String> {
    let url = format!("{}/api/upload", server_url.trim_end_matches('/'));
    let client = reqwest::blocking::Client::new();

    // A flaky network shouldn't cost the user their recording. Retry transient
    // failures (dropped connection, 5xx, 429) with backoff. The local WAVs are
    // deleted ONLY after a confirmed 2xx (the backend persists both tracks before
    // responding), so giving up leaves them on disk for the user to retry.
    let mut last_err = String::new();
    for attempt in 0..3u32 {
        // multipart::Form is consumed by send(), so rebuild it (and re-open the
        // files) on each attempt.
        let form = reqwest::blocking::multipart::Form::new()
            .text("name", name.clone())
            .text("glossary", glossary.clone())
            .text("process", if process { "true" } else { "false" })
            .file("system", &system_path)
            .map_err(|e| format!("reading system track: {e}"))?
            .file("mic", &mic_path)
            .map_err(|e| format!("reading mic track: {e}"))?;

        match client.post(&url).bearer_auth(&token).multipart(form).send() {
            Ok(resp) => {
                let status = resp.status();
                let body = resp.text().unwrap_or_default();
                if status.is_success() {
                    // Durably stored server-side now — drop the local copies.
                    let _ = std::fs::remove_file(&system_path);
                    let _ = std::fs::remove_file(&mic_path);
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
    process: bool,
) -> Result<String, String> {
    let base = tauri::async_runtime::spawn_blocking(move || {
        upload_blocking(server_url, token, name, system_path, mic_path, glossary, process)
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

/// Open the hosted sign-in page in the system browser. After the user signs in
/// (Google / email link), the server redirects to gotcha://connect?server=…&token=…,
/// which the deep-link handler below binds — so this replaces pasting a token.
#[tauri::command]
fn open_signin(server_url: String) -> Result<(), String> {
    let base = server_url.trim_end_matches('/');
    let url = format!("{base}/login?client=desktop");
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
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            upload_recording,
            open_privacy_pane,
            open_signin,
            relaunch
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
