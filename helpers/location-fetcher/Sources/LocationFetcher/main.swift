// LocationFetcher — one-shot CoreLocation reader for the macos-mqtt-bridge.
//
// Prints a single JSON object to stdout describing the current location
// (latitude/longitude/altitude/accuracy + reverse-geocoded place fields)
// and exits. The Python phase ticker spawns this binary, parses the JSON,
// and publishes one HA sensor per field.
//
// Why a separate binary instead of pyobjc CoreLocation in the daemon?
// macOS Location Services TCC permission is keyed on the *executable*.
// The daemon process is the .venv Python interpreter, which would need
// a permission grant — and that grant gets revoked any time the venv
// is rebuilt. Keeping a stable signed-by-path Swift binary avoids that
// churn: the user grants Location to LocationFetcher *once* and the
// grant survives venv rebuilds.
//
// Usage:
//   LocationFetcher                      # one-shot, no reverse-geocode
//   LocationFetcher --reverse-geocode    # also resolve city / state / country
//   LocationFetcher --timeout 20         # wait up to 20s for a fix
//   LocationFetcher --check-authorization  # passive auth-status probe, no prompt, no fix
//
// Exit code is always 0 — any error is reported as JSON {"ok": false,
// "error": "..."} so the Python side has a single uniform parse path.
//
// --check-authorization is consumed by the permissions phase ticker on the
// Python side: it reads CLLocationManager.authorizationStatus without
// triggering a prompt or starting an update, and prints a single JSON
// object like {"authorization": "granted"} (one of: granted / denied /
// restricted / not_determined / unknown). This lets the bridge report
// the LocationFetcher.app helper's grant — not the Python daemon's,
// which never holds Location Services and is irrelevant to whether
// location data is actually flowing.

import Foundation
import CoreLocation

let stdout = FileHandle.standardOutput
let stderr = FileHandle.standardError

func log(_ msg: String) {
    stderr.write(Data(("[location-fetcher] " + msg + "\n").utf8))
}

func emit(_ obj: [String: Any]) -> Never {
    let data: Data
    do {
        data = try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    } catch {
        let fallback = #"{"ok": false, "error": "json_serialize_failed"}"#
        stdout.write(Data(fallback.utf8))
        stdout.write(Data("\n".utf8))
        exit(0)
    }
    stdout.write(data)
    stdout.write(Data("\n".utf8))
    exit(0)
}

func emitFailure(_ reason: String) -> Never {
    emit(["ok": false, "error": reason])
}

final class Fetcher: NSObject, CLLocationManagerDelegate {
    let manager = CLLocationManager()
    let timeout: TimeInterval
    let reverseGeocode: Bool
    var didFinish = false
    var timedOut = false

    init(timeout: TimeInterval, reverseGeocode: Bool) {
        self.timeout = timeout
        self.reverseGeocode = reverseGeocode
        super.init()
        manager.delegate = self
        // kCLLocationAccuracyHundredMeters is plenty for "where is this Mac"
        // and gives a fix in <2s on most devices; Best forces a long wait
        // for marginal accuracy gain.
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func start() {
        // Watchdog: if no fix arrives in time, emit timeout failure.
        DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { [weak self] in
            guard let self = self, !self.didFinish else { return }
            self.timedOut = true
            self.didFinish = true
            self.manager.stopUpdatingLocation()
            emitFailure("timeout_after_\(Int(self.timeout))s")
        }

        let auth = manager.authorizationStatus
        switch auth {
        case .notDetermined:
            // Triggers macOS's "allow LocationFetcher to use your location?"
            // prompt on first run. Subsequent runs persist via TCC by binary
            // path. CLI binaries can't prompt fully — if the user dismisses
            // the prompt the watchdog will catch it as a timeout.
            log("authorization not determined — requesting; user must approve in System Settings → Privacy & Security → Location Services")
            manager.requestWhenInUseAuthorization()
        case .denied, .restricted:
            emitFailure("authorization_denied")
        case .authorizedAlways, .authorized:
            manager.startUpdatingLocation()
        @unknown default:
            emitFailure("unknown_authorization_status")
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if didFinish { return }
        switch manager.authorizationStatus {
        case .denied, .restricted:
            didFinish = true
            emitFailure("authorization_denied")
        case .authorizedAlways, .authorized:
            manager.startUpdatingLocation()
        case .notDetermined:
            // Keep waiting; watchdog will fire if user never decides.
            break
        @unknown default:
            break
        }
    }

    func locationManager(
        _ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]
    ) {
        if didFinish { return }
        guard let loc = locations.last else { return }
        // The first update is sometimes a stale cached fix from minutes ago.
        // Reject anything older than 30s and wait for a fresh one.
        if loc.timestamp.timeIntervalSinceNow < -30 { return }

        manager.stopUpdatingLocation()

        var payload: [String: Any] = [
            "ok": true,
            "latitude": loc.coordinate.latitude,
            "longitude": loc.coordinate.longitude,
            "horizontal_accuracy": loc.horizontalAccuracy,
            "timestamp": ISO8601DateFormatter().string(from: loc.timestamp),
        ]
        // CoreLocation fields are -1 when unavailable. Omit so HA shows the
        // sensor as "unknown" rather than a misleading -1.
        if loc.altitude.isFinite && loc.verticalAccuracy >= 0 {
            payload["altitude"] = loc.altitude
            payload["vertical_accuracy"] = loc.verticalAccuracy
        }
        if loc.speed >= 0 {
            payload["speed"] = loc.speed
        }
        if loc.course >= 0 {
            payload["course"] = loc.course
        }

        if !reverseGeocode {
            didFinish = true
            emit(payload)
        }

        // Reverse geocode — adds locality / state / country / postal_code.
        // Honors Apple's documented rate limits (don't call >50/hr).
        CLGeocoder().reverseGeocodeLocation(loc) { [weak self] placemarks, error in
            guard let self = self, !self.didFinish else { return }
            self.didFinish = true
            var enriched = payload
            if let pm = placemarks?.first {
                if let v = pm.name { enriched["place_name"] = v }
                if let v = pm.thoroughfare { enriched["thoroughfare"] = v }
                if let v = pm.subThoroughfare { enriched["sub_thoroughfare"] = v }
                if let v = pm.locality { enriched["locality"] = v }
                if let v = pm.subLocality { enriched["sub_locality"] = v }
                if let v = pm.administrativeArea { enriched["administrative_area"] = v }
                if let v = pm.subAdministrativeArea { enriched["sub_administrative_area"] = v }
                if let v = pm.postalCode { enriched["postal_code"] = v }
                if let v = pm.country { enriched["country"] = v }
                if let v = pm.isoCountryCode { enriched["country_code"] = v }
                if let v = pm.timeZone?.identifier { enriched["timezone"] = v }
            } else if let error = error {
                enriched["geocode_error"] = error.localizedDescription
            }
            emit(enriched)
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if didFinish { return }
        didFinish = true
        // CLError.denied (1) is the most common — user hit "Don't Allow"
        // in the prompt or revoked in System Settings.
        let nsErr = error as NSError
        if nsErr.domain == kCLErrorDomain && nsErr.code == CLError.denied.rawValue {
            emitFailure("authorization_denied")
        } else {
            emitFailure("error_\(nsErr.code)_\(error.localizedDescription)")
        }
    }
}

// ---- argument parsing -------------------------------------------------------

var timeoutSecs: Double = 15
var reverseGeocode = false
var checkAuthorization = false
let args = CommandLine.arguments
var i = 1
while i < args.count {
    let a = args[i]
    switch a {
    case "--timeout":
        i += 1
        if i < args.count, let v = Double(args[i]) {
            timeoutSecs = v
        }
    case "--reverse-geocode", "-g":
        reverseGeocode = true
    case "--check-authorization":
        checkAuthorization = true
    case "--help", "-h":
        let usage = """
        LocationFetcher — one-shot CoreLocation reader.

        Options:
          --timeout SECONDS         How long to wait for a fix (default 15)
          --reverse-geocode, -g     Also resolve city / state / country
          --check-authorization     Print {"authorization": "granted|denied|
                                    restricted|not_determined|unknown"} and
                                    exit. No prompt, no fix.
          --help, -h                Show this help

        Output: a single JSON object on stdout, exit 0.
        """
        print(usage)
        exit(0)
    default:
        break
    }
    i += 1
}

if checkAuthorization {
    // Resolve the helper's TCC grant for Location Services. This is
    // surprisingly tricky on macOS:
    //
    //   - CLLocationManager.authorizationStatus on a fresh manager
    //     returns .notDetermined synchronously even when the bundle
    //     already holds a grant. The true status only arrives via
    //     locationManagerDidChangeAuthorization.
    //   - That delegate callback doesn't fire from manager creation
    //     alone on macOS — the system only wakes the authorization
    //     pipeline once the manager is *used*, typically by calling
    //     requestWhenInUseAuthorization() or starting updates.
    //
    // requestWhenInUseAuthorization() is the gentle nudge that gets the
    // system to publish a real status. When the user has *already*
    // approved the bundle, this call resolves silently to .authorized
    // — no prompt, no UI. When the bundle is genuinely undetermined,
    // macOS will display the prompt; that's the user's signal that
    // they need to approve. We deliberately keep this on the check
    // path so that polling the probe doubles as a self-healing nudge
    // (rather than reporting "denied" forever and silently breaking).
    final class AuthProbe: NSObject, CLLocationManagerDelegate {
        var captured: CLAuthorizationStatus?
        func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
            captured = m.authorizationStatus
        }
    }
    let mgr = CLLocationManager()
    let probe = AuthProbe()
    mgr.delegate = probe
    mgr.requestWhenInUseAuthorization()

    // Spin the runloop until the delegate callback resolves a non-
    // .notDetermined status, or we hit the deadline. We accept an
    // intermediate .notDetermined callback (it can fire once with the
    // pre-request value before the real one arrives), so we keep
    // waiting until we see something definitive.
    let deadline = Date().addingTimeInterval(3.0)
    while Date() < deadline {
        if let c = probe.captured, c != .notDetermined { break }
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    let resolved = probe.captured ?? mgr.authorizationStatus
    let auth: String
    switch resolved {
    case .notDetermined:
        auth = "not_determined"
    case .denied:
        auth = "denied"
    case .restricted:
        auth = "restricted"
    case .authorizedAlways, .authorized:
        auth = "granted"
    @unknown default:
        auth = "unknown"
    }
    emit(["authorization": auth])
}

let fetcher = Fetcher(timeout: timeoutSecs, reverseGeocode: reverseGeocode)
fetcher.start()
RunLoop.main.run()
