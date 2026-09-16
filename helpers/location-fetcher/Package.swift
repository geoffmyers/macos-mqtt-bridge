// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "LocationFetcher",
    platforms: [
        .macOS(.v12)
    ],
    targets: [
        .executableTarget(
            name: "LocationFetcher",
            path: "Sources/LocationFetcher"
        )
    ]
)
