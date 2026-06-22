import AVFoundation

/// Captures the microphone through AVAudioEngine with macOS native Voice
/// Processing (AUVoiceProcessingIO, AEC3-class). Per Apple, VP uses ALL playback
/// audio — including OTHER apps (Discord/Meet) — as the echo reference, so this
/// removes the call audio that bleeds into the mic when not on headphones.
///
/// Gotcha (undocumented): enabling VP silently makes the mic input MULTICHANNEL
/// (7-9 ch). Channel 0 is the clean echo-cancelled mono mic; the rest are
/// reference channels. We extract channel 0 manually — feeding the multichannel
/// buffer to AVAudioConverter crashes. We also do NOT route the multichannel
/// input through the engine's output graph (that fails AUInitialize with -10875).
public final class VoiceProcessingMic: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?

    public init() {}

    /// Start capturing the echo-cancelled mic to `url` (mono 16-bit PCM WAV at the
    /// input's native rate). Call `stop()` to finish.
    public func start(to url: URL) throws {
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)
        // Reduce how aggressively VP ducks "other audio" (the call) on output.
        if #available(macOS 14.0, *) {
            let duck = AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                enableAdvancedDucking: false, duckingLevel: .min)
            input.voiceProcessingOtherAudioDuckingConfiguration = duck
        }

        let inFmt = input.outputFormat(forBus: 0)   // multichannel under VP
        let sampleRate = inFmt.sampleRate

        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let f = try AVAudioFile(forWriting: url, settings: settings,
                                commonFormat: .pcmFormatFloat32, interleaved: false)
        self.file = f

        // Mono format for channel 0 only — what we write to disk.
        guard let monoFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                          sampleRate: sampleRate, channels: 1,
                                          interleaved: false) else {
            throw NSError(domain: "VPMic", code: 1)
        }

        input.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { [weak self] buf, _ in
            guard let self, let src = buf.floatChannelData else { return }
            let frames = Int(buf.frameLength)
            // Copy channel 0 (the clean echo-cancelled mic) into a mono buffer.
            guard let mono = AVAudioPCMBuffer(pcmFormat: monoFmt,
                                              frameCapacity: buf.frameLength),
                  let dst = mono.floatChannelData else { return }
            mono.frameLength = buf.frameLength
            dst[0].update(from: src[0], count: frames)
            try? self.file?.write(from: mono)
        }

        engine.prepare()
        try engine.start()
    }

    public func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        file = nil
    }
}
