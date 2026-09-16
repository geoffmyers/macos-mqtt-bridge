// CallObserver — emits JSON lines on stdout for real-time call state changes.
//
// The Python bridge spawns this binary as a subprocess and parses each line.
// CallKit's CXCallObserver reports incoming/outgoing/connected/ended events
// for both phone and FaceTime calls without requiring VoIP provider entitlements.

import Foundation
import CallKit

let stdout = FileHandle.standardOutput
let stderr = FileHandle.standardError

func emit(_ event: [String: Any]) {
    var enriched = event
    enriched["ts"] = ISO8601DateFormatter().string(from: Date())
    guard let data = try? JSONSerialization.data(withJSONObject: enriched, options: []) else {
        return
    }
    if let line = String(data: data, encoding: .utf8) {
        stdout.write(Data((line + "\n").utf8))
    }
}

func log(_ msg: String) {
    stderr.write(Data(("[call-observer] " + msg + "\n").utf8))
}

class Observer: NSObject, CXCallObserverDelegate {
    let observer = CXCallObserver()
    // Track each call's previous state so we can emit transitions.
    var state: [UUID: (outgoing: Bool, connected: Bool)] = [:]

    override init() {
        super.init()
        observer.setDelegate(self, queue: nil)
        log("started, watching \(observer.calls.count) existing call(s)")
        emit(["event": "observer_started", "existing_calls": observer.calls.count])
    }

    func callObserver(_ observer: CXCallObserver, callChanged call: CXCall) {
        let prev = state[call.uuid]
        let isNew = prev == nil
        state[call.uuid] = (outgoing: call.isOutgoing, connected: call.hasConnected)

        let base: [String: Any] = [
            "uuid": call.uuid.uuidString,
            "outgoing": call.isOutgoing,
            "on_hold": call.isOnHold,
        ]

        if call.hasEnded {
            emit(base.merging([
                "event": "call_ended",
                "was_connected": call.hasConnected,
            ]) { _, new in new })
            state.removeValue(forKey: call.uuid)
            return
        }

        if call.hasConnected {
            if prev?.connected != true {
                emit(base.merging([
                    "event": "call_connected",
                ]) { _, new in new })
            }
            return
        }

        // Not connected, not ended — this is a ringing call.
        if isNew {
            emit(base.merging([
                "event": call.isOutgoing ? "call_outgoing_started" : "call_incoming_ringing",
            ]) { _, new in new })
        }
    }
}

let observer = Observer()
RunLoop.main.run()
