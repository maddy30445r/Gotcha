import AVFoundation
import CoreMedia

/// Writes a single audio source (system OR mic) to a mono 16-bit PCM WAV file.
///
/// Source-agnostic on purpose: the same writer handles ScreenCaptureKit's
/// `.audio`/`.microphone` buffers, or — if mic capture has to fall back to a
/// separate `AVCaptureSession` — those buffers too. It lazily reads the real
/// input `AVAudioFormat` off the first sample buffer and downmixes to mono via
/// `AVAudioConverter`, so we never hardcode the incoming format.
public final class TrackWriter: @unchecked Sendable {
    private let outURL: URL
    private let lock = NSLock()

    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?
    private var finished = false

    /// Captured so the caller can surface a write failure instead of it vanishing
    /// inside the audio callback.
    public private(set) var lastError: Error?

    public init(outURL: URL) {
        self.outURL = outURL
    }

    /// Append one CMSampleBuffer of PCM audio. Safe to call from the capture queue.
    public func append(_ sampleBuffer: CMSampleBuffer) {
        lock.lock()
        defer { lock.unlock() }
        if finished { return }

        do {
            guard let pcm = try makePCMBuffer(from: sampleBuffer) else { return }
            let f = try ensureFile(inputFormat: pcm.format)
            let converted = try downmixToMono(pcm, fileFormat: f.processingFormat)
            try f.write(from: converted)
        } catch {
            lastError = error
        }
    }

    /// Flush and close the file. Idempotent.
    public func finish() {
        lock.lock()
        defer { lock.unlock() }
        finished = true
        file = nil   // ARC closes the AVAudioFile, flushing the header/length.
    }

    // MARK: - internals

    private func ensureFile(inputFormat fmt: AVAudioFormat) throws -> AVAudioFile {
        if let file { return file }
        // On-disk format: mono, 16-bit PCM, at the source sample rate (typically
        // 48 kHz). Matches what the old sounddevice path produced and what Sarvam
        // happily accepts.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: fmt.sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let f = try AVAudioFile(forWriting: outURL, settings: settings,
                                commonFormat: .pcmFormatFloat32, interleaved: false)
        self.file = f
        return f
    }

    private func makePCMBuffer(from sampleBuffer: CMSampleBuffer) throws -> AVAudioPCMBuffer? {
        guard let fmtDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc)
        else { return nil }

        let fmt: AVAudioFormat
        if let cached = inputFormat {
            fmt = cached
        } else {
            guard let f = AVAudioFormat(streamDescription: asbd) else { return nil }
            inputFormat = f
            fmt = f
        }

        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frames > 0,
              let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames)
        else { return nil }
        buf.frameLength = frames

        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames),
            into: buf.mutableAudioBufferList)
        guard status == noErr else { return nil }
        return buf
    }

    private func downmixToMono(_ input: AVAudioPCMBuffer,
                              fileFormat: AVAudioFormat) throws -> AVAudioPCMBuffer {
        // The file rate is pinned to the source rate (see ensureFile), so this is
        // purely a channel downmix (e.g. stereo → mono) at the same sample rate —
        // no resampling, so the simple convert(to:from:) API is sufficient.
        if input.format == fileFormat { return input }

        if converter == nil || converter?.inputFormat != input.format {
            converter = AVAudioConverter(from: input.format, to: fileFormat)
        }
        guard let converter,
              let out = AVAudioPCMBuffer(pcmFormat: fileFormat,
                                         frameCapacity: input.frameLength)
        else { throw WriterError.noConverter }

        try converter.convert(to: out, from: input)
        return out
    }

    enum WriterError: Error { case noConverter }
}
