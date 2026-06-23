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
use tauri::State;

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
    // DEV path: <project>/mac_recorder/.build/release/mac-recorder.
    // TODO(Phase 3): bundle as a Tauri sidecar and resolve via resource_dir for
    // the shipped, notarized app.
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
) -> Result<String, String> {
    let url = format!("{}/api/upload", server_url.trim_end_matches('/'));
    let form = reqwest::blocking::multipart::Form::new()
        .text("name", name)
        .file("system", &system_path)
        .map_err(|e| format!("reading system track: {e}"))?
        .file("mic", &mic_path)
        .map_err(|e| format!("reading mic track: {e}"))?;

    let resp = reqwest::blocking::Client::new()
        .post(url)
        .bearer_auth(token)
        .multipart(form)
        .send()
        .map_err(|e| format!("upload failed: {e}"))?;

    let status = resp.status();
    let body = resp.text().unwrap_or_default();
    if !status.is_success() {
        return Err(format!("server {}: {}", status.as_u16(), body));
    }

    // Upload succeeded — drop the local copies (audio lives on the backend now).
    let _ = std::fs::remove_file(&system_path);
    let _ = std::fs::remove_file(&mic_path);

    let v: serde_json::Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
    Ok(v.get("base").and_then(|b| b.as_str()).unwrap_or("").to_string())
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
) -> Result<String, String> {
    let base = tauri::async_runtime::spawn_blocking(move || {
        upload_blocking(server_url, token, name, system_path, mic_path)
    })
    .await
    .map_err(|e| e.to_string())??;
    Ok(base)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(RecMutex::default())
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            upload_recording
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
