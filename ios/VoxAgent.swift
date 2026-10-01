// VoxButton for iPhone: lends the phone's microphone to the voxbutton server.
// Keep it running (it stays alive in the background while "Lend microphone" is
// on) and click the button on the PC: the phone records until the next click
// and sends the audio to be transcribed and typed there.

import AVFoundation
import SwiftUI

@main
struct VoxAgentApp: App {
    @StateObject private var agent = Agent()

    var body: some Scene {
        WindowGroup { ContentView().environmentObject(agent) }
    }
}

struct ContentView: View {
    @EnvironmentObject var agent: Agent

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Lend microphone", isOn: Binding(get: { agent.enabled }, set: { agent.setEnabled($0) }))
                    LabeledContent("Status") {
                        Text(agent.status).foregroundStyle(agent.recording ? .red : .secondary)
                    }
                    if !agent.lastText.isEmpty {
                        Text(agent.lastText).font(.callout).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("While this is on, the microphone stays open (orange dot) so the PC button can start a recording even when Moonlight is in front.")
                }
                Section("Server") {
                    TextField("https://…", text: $agent.server)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    TextField("Token", text: $agent.token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
            }
            .navigationTitle("VoxButton")
        }
    }
}

@MainActor
final class Agent: ObservableObject {
    @Published var enabled = false
    @Published var status = "Off"
    @Published var recording = false
    @Published var lastText = ""
    @Published var server: String { didSet { UserDefaults.standard.set(server, forKey: "server") } }
    @Published var token: String { didSet { UserDefaults.standard.set(token, forKey: "token") } }

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private let capture = Capture()
    private var pollTask: Task<Void, Never>?

    init() {
        let info = Bundle.main.infoDictionary ?? [:]
        server = UserDefaults.standard.string(forKey: "server") ?? info["VBServer"] as? String ?? ""
        token = UserDefaults.standard.string(forKey: "token") ?? info["VBToken"] as? String ?? ""
        let nc = NotificationCenter.default
        nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] n in
            let type = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt).flatMap(AVAudioSession.InterruptionType.init)
            Task { @MainActor in if type == .ended { self?.restartAudio() } }
        }
        nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.restartAudio() }
        }
        if UserDefaults.standard.bool(forKey: "enabled") { setEnabled(true) }
    }

    func setEnabled(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: "enabled")
        enabled = on
        if on {
            AVAudioApplication.requestRecordPermission { granted in
                Task { @MainActor in
                    guard granted else { self.status = "Microphone permission denied"; self.enabled = false; return }
                    self.startAudio()
                    self.pollTask = Task { await self.pollLoop() }
                }
            }
        } else {
            pollTask?.cancel()
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            status = "Off"
        }
    }

    // MARK: audio

    private func startAudio() {
        let session = AVAudioSession.sharedInstance()
        do {
            // mixWithOthers so Moonlight keeps its sound; the running input is what
            // keeps this app alive in the background.
            try session.setCategory(.playAndRecord, mode: .default,
                                    options: [.mixWithOthers, .defaultToSpeaker, .allowBluetoothA2DP])
            try session.setActive(true)
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            converter = AVAudioConverter(from: format, to: target)
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 4096, format: format) { [capture, target, converter] buf, _ in
                guard capture.active, let converter else { return }
                let ratio = target.sampleRate / buf.format.sampleRate
                let cap = AVAudioFrameCount(Double(buf.frameLength) * ratio) + 32
                guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
                var fed = false
                converter.convert(to: out, error: nil) { _, status in
                    if fed { status.pointee = .noDataNow; return nil }
                    fed = true
                    status.pointee = .haveData
                    return buf
                }
                if let p = out.int16ChannelData, out.frameLength > 0 {
                    let n = Int(out.frameLength)
                    var sum: Float = 0
                    for i in 0..<n { let v = Float(p[0][i]) / 32768; sum += v * v }
                    capture.append(Data(bytes: p[0], count: n * 2), level: 10 * log10(max(sum / Float(n), 1e-12)),
                                   seconds: Double(n) / 16000)
                }
            }
            engine.prepare()
            try engine.start()
            status = "Ready"
        } catch {
            status = "Audio error: \(error.localizedDescription)"
        }
    }

    private func restartAudio() {
        guard enabled else { return }
        engine.stop()
        startAudio()
    }

    // MARK: server

    private func request(_ path: String, query: [URLQueryItem] = [], method: String = "GET",
                         timeout: TimeInterval = 40) -> URLRequest? {
        guard var comps = URLComponents(string: server.trimmingCharacters(in: .whitespaces)) else { return nil }
        comps.path = (comps.path.hasSuffix("/") ? String(comps.path.dropLast()) : comps.path) + path
        comps.queryItems = query.isEmpty ? nil : query
        guard let url = comps.url else { return nil }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("Bearer \(token.trimmingCharacters(in: .whitespaces))", forHTTPHeaderField: "Authorization")
        return req
    }

    private func pollLoop() async {
        while !Task.isCancelled {
            guard let req = request("/agent/wait", query: [.init(name: "name", value: "iphone"),
                                                           .init(name: "prio", value: "1"),
                                                           .init(name: "ips", value: localIPv4s().joined(separator: ","))]) else {
                status = "Set the server URL"
                try? await Task.sleep(for: .seconds(5))
                continue
            }
            do {
                let (data, resp) = try await URLSession.shared.data(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                    status = "Server said \((resp as? HTTPURLResponse)?.statusCode ?? 0)"
                    try? await Task.sleep(for: .seconds(3))
                    continue
                }
                if !recording { status = "Ready" }
                let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                let cmd = json?["cmd"] as? String
                switch cmd {
                case "start": begin()
                case "listen": listen(pause: json?["pause"] as? Double ?? 0.6, chunk: json?["chunk"] as? Double ?? 6)
                case "once": listenOnce(pause: json?["pause"] as? Double ?? 0.6)
                case "stop" where onceActive: endOnce()
                case "stop" where listening: stopListening()
                case "stop": await finish()
                default: break
                }
            } catch {
                if Task.isCancelled { return }
                status = "Offline: \(error.localizedDescription)"
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    @Published var listening = false

    private func listen(pause: Double, chunk: Double) {
        if !engine.isRunning { restartAudio() }
        capture.onSegment = { [weak self] pcm in
            Task { @MainActor in await self?.send(pcm, segment: true) }
        }
        capture.listen(pause: pause, chunk: chunk)
        listening = true
        recording = true
        status = "Listening…"
    }

    private var onceActive = false

    /// Voice command: record until the first pause, send it, stop. Gives up
    /// after 8 s without speech.
    private func listenOnce(pause: Double) {
        if !engine.isRunning { restartAudio() }
        capture.onSegment = { [weak self] pcm in
            Task { @MainActor in
                guard let self, self.onceActive else { return }
                self.onceActive = false
                _ = self.capture.stopListening()
                self.listening = false
                self.recording = false
                self.status = "Running command…"
                await self.send(pcm, segment: false)
            }
        }
        capture.listen(pause: pause, chunk: 4)  // commands are short: 4 s of speech at most
        onceActive = true
        listening = true
        recording = true
        status = "Listening for a command…"
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(8))
            self.endOnce()
        }
    }

    /// Ends a one-shot listen early (second click or timeout); what was said so
    /// far still goes out through onSegment.
    private func endOnce() {
        guard onceActive else { return }
        if !capture.stopListening() {
            onceActive = false
            listening = false
            recording = false
            status = "Ready"
            Task {
                if var e = request("/agent/error", method: "POST", timeout: 10) {
                    e.httpBody = Data("no speech heard".utf8)
                    _ = try? await URLSession.shared.data(for: e)
                }
            }
        }
    }

    private func stopListening() {
        _ = capture.stop()
        listening = false
        recording = false
        status = "Ready"
    }

    private func begin() {
        if !engine.isRunning { restartAudio() }
        capture.start()
        recording = true
        status = "Recording…"
    }

    private func finish() async {
        guard recording else { return }
        recording = false
        let pcm = capture.stop()
        status = "Transcribing…"
        await send(pcm, segment: false)
    }

    private func send(_ pcm: Data, segment: Bool) async {
        guard var req = request("/transcribe", method: "POST", timeout: 120) else { return }
        req.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Agent")
        if segment { req.setValue("1", forHTTPHeaderField: "X-Segment") }
        do {
            let (data, resp) = try await URLSession.shared.upload(for: req, from: wav(pcm))
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            if (resp as? HTTPURLResponse)?.statusCode == 200 {
                lastText = json?["text"] as? String ?? ""
                status = listening ? "Listening…" : "Ready"
            } else {
                status = "Error: \(json?["error"] as? String ?? "HTTP \((resp as? HTTPURLResponse)?.statusCode ?? 0)")"
            }
        } catch {
            status = "Upload failed: \(error.localizedDescription)"
            if var e = request("/agent/error", method: "POST", timeout: 10) {
                e.httpBody = Data("upload failed: \(error.localizedDescription)".utf8)
                _ = try? await URLSession.shared.data(for: e)
            }
        }
    }

    /// This phone's IPv4 addresses (Wi-Fi, cellular, the Tailscale tunnel), so the
    /// server can tell it's the device Moonlight is streaming to.
    private func localIPv4s() -> [String] {
        var out: [String] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0 else { return out }
        defer { freeifaddrs(head) }
        var p = head
        while let ifa = p?.pointee {
            if let addr = ifa.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET), (ifa.ifa_flags & UInt32(IFF_LOOPBACK)) == 0 {
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                if getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                    out.append(String(cString: host))
                }
            }
            p = ifa.ifa_next
        }
        return out
    }

    private func wav(_ pcm: Data) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + pcm.count))
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(16000); u32(32000); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(UInt32(pcm.count))
        d.append(pcm)
        return d
    }
}

/// Audio collected on the realtime thread, read on the main actor. In listen
/// mode ("always on") it hands over what you say at every pause, and every
/// `chunk` seconds while you keep talking (cut at the quietest moment of the
/// last 1.5 s, so words stay whole).
final class Capture: @unchecked Sendable {
    static let speech: Float = -40  // dBFS: louder than this counts as talking
    static let preroll = 0.3  // s kept from before the speech started
    static let bytesPerSecond = 32000.0

    private let lock = NSLock()
    private var data = Data()
    private var frames: [(end: Int, level: Float)] = []
    private var mode = 0  // 0 off, 1 recording, 2 listening
    private var ring = Data()
    private var talking = false
    private var quiet = 0.0
    private var pause = 0.6
    private var chunk = 6.0
    // Speech starts well above the room's background noise and ends when the
    // level falls back near it (or far below your own peak).
    private var floorLevels: [Float] = []
    private var peak: Float = -120

    private var noiseFloor: Float {
        guard !floorLevels.isEmpty else { return -60 }
        return floorLevels.sorted()[floorLevels.count / 2]
    }
    var onSegment: ((Data) -> Void)?

    var active: Bool { lock.withLock { mode != 0 } }

    func start() { lock.withLock { data.removeAll(); mode = 1 } }

    func listen(pause: Double, chunk: Double) {
        lock.withLock {
            data.removeAll(); frames.removeAll(); ring.removeAll()
            talking = false
            self.pause = pause
            self.chunk = chunk
            mode = 2
        }
    }

    /// Ends listening; returns whether a last piece went to onSegment.
    func stopListening() -> Bool {
        let segment = lock.withLock { () -> Data? in
            let seg = mode == 2 && talking && data.count >= 12800 ? data : nil
            mode = 0
            talking = false
            return seg
        }
        if let segment { onSegment?(segment) }
        return segment != nil
    }

    func stop() -> Data {
        let (out, segment) = lock.withLock { () -> (Data, Data?) in
            let seg = mode == 2 && talking ? data : nil
            mode = 0
            talking = false
            return (data, seg)
        }
        if let segment, segment.count >= 12800 { onSegment?(segment) }
        return out
    }

    func append(_ d: Data, level: Float, seconds: Double) {
        var ready: Data?
        lock.withLock {
            switch mode {
            case 1:
                data.append(d)
            case 2:
                if talking {
                    data.append(d)
                    frames.append((data.count, level))
                    peak = max(peak, level)
                    quiet = level < max(Self.speech, noiseFloor + 6, peak - 20) ? quiet + seconds : 0
                    if quiet >= pause {
                        ready = take(upTo: data.count)
                    } else if Double(data.count) / Self.bytesPerSecond >= chunk {
                        let from = data.count - Int(1.5 * Self.bytesPerSecond)
                        let cut = frames.filter { $0.end >= from && $0.end < data.count }
                            .min { $0.level < $1.level }?.end ?? data.count
                        ready = take(upTo: cut)
                    }
                } else if level >= max(Self.speech, noiseFloor + 10) {
                    talking = true
                    quiet = 0
                    peak = level
                    data = ring + d
                    frames = [(ring.count, -120), (data.count, level)]
                } else {
                    floorLevels.append(level)
                    if floorLevels.count > 24 { floorLevels.removeFirst() }
                    ring.append(d)
                    let keep = Int(Self.preroll * Self.bytesPerSecond) & ~1
                    if ring.count > keep { ring = ring.suffix(keep) }
                }
            default:
                break
            }
        }
        if let ready, ready.count >= 12800 { onSegment?(ready) }  // >= 0.4 s
    }

    /// Takes data[..<cut]; the rest stays as the start of the next piece. Lock held.
    private func take(upTo cut: Int) -> Data {
        let piece = Data(data.prefix(cut))
        if cut >= data.count {
            data.removeAll(); frames.removeAll(); ring.removeAll()
            talking = false
        } else {
            data = Data(data.suffix(from: data.startIndex + cut))
            frames = frames.filter { $0.end > cut }.map { ($0.end - cut, $0.level) }
        }
        return piece
    }
}
