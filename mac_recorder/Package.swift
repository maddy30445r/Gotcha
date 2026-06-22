// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "mac_recorder",
    platforms: [.macOS(.v15)],   // SCStreamConfiguration.captureMicrophone needs macOS 15+
    targets: [
        // Reusable native capture core — this is what the shipped app links against.
        .target(name: "MeetingCaptureKit"),

        // Thin CLI shell. The embedded Info.plist gives the bare executable an
        // NSMicrophoneUsageDescription so mic access doesn't SIGABRT under TCC.
        .executableTarget(
            name: "mac-recorder",
            dependencies: ["MeetingCaptureKit"],
            // Embedded into the binary via -sectcreate below, not bundled as a resource.
            exclude: ["Info.plist"],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/mac-recorder/Info.plist",
                ])
            ]
        ),
    ]
)
