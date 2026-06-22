import AVFoundation
import CoreGraphics
import Foundation

/// Screen Recording + Microphone TCC handling, plus mic enumeration.
public enum Permissions {

    /// Ensure Screen Recording access (required for ScreenCaptureKit system audio).
    /// Returns false if not granted — granting it requires the user to enable the
    /// terminal app in System Settings and then RELAUNCH the terminal.
    public static func ensureScreenRecording() -> Bool {
        if CGPreflightScreenCaptureAccess() { return true }
        // Triggers the system prompt / opens the settings pane. Even if the user
        // grants it now, the current process must be relaunched to pick it up.
        return CGRequestScreenCaptureAccess()
    }

    /// Ensure Microphone access. Prompts on first run (needs the embedded
    /// NSMicrophoneUsageDescription). Blocks until the user responds.
    public static func ensureMic() -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            let sem = DispatchSemaphore(value: 0)
            nonisolated(unsafe) var granted = false
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                granted = ok
                sem.signal()
            }
            sem.wait()
            return granted
        default:   // .denied, .restricted
            return false
        }
    }

    /// Print available microphones (localizedName + uniqueID) for `--mic`.
    public static func printMics() {
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.microphone, .external],
            mediaType: .audio,
            position: .unspecified)
        let mics = session.devices
        if mics.isEmpty {
            FileHandle.standardError.write(Data("No microphones found.\n".utf8))
            return
        }
        print("Microphones (use the uniqueID with --mic):\n")
        let defaultUID = AVCaptureDevice.default(for: .audio)?.uniqueID
        for m in mics {
            let marker = m.uniqueID == defaultUID ? "  (default)" : ""
            print("  \(m.localizedName)\(marker)\n    \(m.uniqueID)")
        }
    }
}
