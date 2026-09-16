// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "CallObserver",
    platforms: [
        .macOS(.v12)
    ],
    targets: [
        .executableTarget(
            name: "CallObserver",
            path: "Sources/CallObserver"
        )
    ]
)
