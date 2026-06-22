

import AVFoundation
import ScreenCaptureKit

public enum RecorderError: Error, CustomStringConvertible {
    case noDisplay
    case writeFailed(String)
    public var description: String {
        switch self {
        case .noDisplay: return "no display available to capture"
        case .writeFailed(let s): return "audio write failed: \(s)"
        }
    }
}

/// Captures the call (system audio) via ScreenCaptureKit and the user (mic) via
/// AVAudioEngine Voice Processing, writing each to its own mono WAV (no mixing).
///
/// The mic goes through native Voice Processing (AUVoiceProcessingIO) so the call
/// audio that bleeds into the mic on speakers — the others' voices — is cancelled
/// at capture time (Apple's VP uses other apps' playback as the echo reference).
/// This is the "works without headphones" fix, verified to remove cross-app audio.
///
/// The capture logic here is the reusable native core: a future macOS app links
/// `MeetingCaptureKit` and drives this class directly; only the CLI shell around
/// it is throwaway.
public final class MeetingRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let systemWriter: TrackWriter
    private let micURL: URL
    private let vpMic = VoiceProcessingMic()

    private let systemQueue = DispatchQueue(label: "capture.system")
    private var stream: SCStream?

    public init(systemURL: URL, micURL: URL) {
        self.systemWriter = TrackWriter(outURL: systemURL)
        self.micURL = micURL
    }

    public func start(micDeviceID: String? = nil) async throws {
        // This call is also the Screen Recording TCC gate; it throws if denied.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw RecorderError.noDisplay }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true                  // system audio (the call / others)
        cfg.sampleRate = 48_000
        cfg.channelCount = 2
        // NOTE: must be false — with Voice Processing active, the mic's IO unit
        // lives in our process and the system audio can be attributed to us;
        // excluding our process audio would then silence the system capture.
        cfg.excludesCurrentProcessAudio = false
        // SCStream needs a video config even when we only want audio. Keep it
        // tiny and never add a `.screen` output, so frames are never delivered.
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        // Start the Voice Processing mic FIRST — its engine has a longer startup
        // latency than SCStream, so starting it first keeps the two tracks roughly
        // time-aligned (both writing by the time "recording" begins).
        try vpMic.start(to: micURL)

        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: systemQueue)
        try await s.startCapture()
        self.stream = s
    }

    public func stop() async {
        vpMic.stop()
        try? await stream?.stopCapture()
        systemWriter.finish()
    }

    /// Surface the first write error (if any) so the CLI can report it.
    public var writeError: Error? {
        systemWriter.lastError
    }

    // MARK: SCStreamOutput

    public func stream(_ stream: SCStream,
                       didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                       of type: SCStreamOutputType) {
        guard sampleBuffer.isValid else { return }
        if type == .audio { systemWriter.append(sampleBuffer) }
    }

    // MARK: SCStreamDelegate

    public func stream(_ stream: SCStream, didStopWithError error: Error) {
        systemWriter.finish()
    }
}
