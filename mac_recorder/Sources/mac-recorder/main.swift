import Foundation
import MeetingCaptureKit

// CLI shell around MeetingCaptureKit. Protocol contract for the Python launcher:
//   stdout "RECORDING"           — emitted once capture has started
//   stdout <system path>         — second-to-last line
//   stdout <mic path>            — last line
//   stderr "ERR_PERMISSION" (2)  — screen/mic access not granted
//   stderr "ERR_START:<err>" (3) — stream failed to start
// Stop by writing a newline to stdin (or closing it), or sending SIGINT/SIGTERM.
//
// The main thread runs dispatchMain() so the main queue stays live for
// ScreenCaptureKit's XPC callbacks and async continuations; we exit() from the
// stop handler rather than blocking the main thread on a semaphore.

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

// ---- args ----
var systemPath: String?
var micPath: String?
var micDeviceID: String?

var argv = Array(CommandLine.arguments.dropFirst())

var idx = 0
while idx < argv.count {
    let arg = argv[idx]
    switch arg {
    case "--out-system": idx += 1; systemPath = idx < argv.count ? argv[idx] : nil
    case "--out-mic":    idx += 1; micPath = idx < argv.count ? argv[idx] : nil
    case "--mic":        idx += 1; micDeviceID = idx < argv.count ? argv[idx] : nil
    case "--list-mics":  Permissions.printMics(); exit(0)
    default: fail("unknown argument: \(arg)", code: 64)
    }
    idx += 1
}

guard let systemPath, let micPath else {
    fail("usage: mac-recorder --out-system <path> --out-mic <path> [--mic <uid>]", code: 64)
}

// ---- permissions ----
guard Permissions.ensureMic() else { fail("ERR_PERMISSION", code: 2) }
guard Permissions.ensureScreenRecording() else { fail("ERR_PERMISSION", code: 2) }

let recorder = MeetingRecorder(
    systemURL: URL(fileURLWithPath: systemPath),
    micURL: URL(fileURLWithPath: micPath))

// ---- stop handling (idempotent) ----
let stopLock = NSLock()
nonisolated(unsafe) var stopping = false

func requestStop() {
    stopLock.lock()
    if stopping { stopLock.unlock(); return }
    stopping = true
    stopLock.unlock()

    Task {
        await recorder.stop()
        if let err = recorder.writeError {
            fail("ERR_START:write \(err)", code: 3)
        }
        print(systemPath)
        print(micPath)
        fflush(stdout)
        exit(0)
    }
}

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigint.setEventHandler { requestStop() }
sigterm.setEventHandler { requestStop() }
sigint.resume()
sigterm.resume()

// A newline or EOF on stdin also stops (how the Python launcher requests stop).
DispatchQueue.global().async {
    _ = readLine(strippingNewline: false)
    requestStop()
}

// ---- start ----
Task {
    do {
        try await recorder.start(micDeviceID: micDeviceID)
        print("RECORDING")
        fflush(stdout)
    } catch {
        fail("ERR_START:\(error)", code: 3)
    }
}

dispatchMain()
