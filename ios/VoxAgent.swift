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
                if let p = out.int16ChannelData {
                    capture.append(Data(bytes: p[0], count: Int(out.frameLength) * 2))
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
                                                           .init(name: "prio", value: "1")]) else {
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
                let cmd = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["cmd"] as? String
                switch cmd {
                case "start": begin()
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
        guard var req = request("/transcribe", method: "POST", timeout: 120) else { return }
        req.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Agent")
        do {
            let (data, resp) = try await URLSession.shared.upload(for: req, from: wav(pcm))
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            if (resp as? HTTPURLResponse)?.statusCode == 200 {
                lastText = json?["text"] as? String ?? ""
                status = "Ready"
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

/// Audio collected on the realtime thread, read on the main actor.
final class Capture: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private var _active = false

    var active: Bool { lock.withLock { _active } }

    func start() { lock.withLock { data.removeAll(); _active = true } }
    func append(_ d: Data) { lock.withLock { if _active { data.append(d) } } }
    func stop() -> Data { lock.withLock { _active = false; return data } }
}
