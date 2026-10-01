// VoxButton: a floating, always-on-top microphone button for macOS.
// Click to record, click again to send the audio to the voxbutton server,
// which transcribes it and types the text on the remote machine.
// Drag to move; right-click for the menu.

import AppKit
import AVFoundation

struct Config: Codable {
    var server: String
    var token: String
    var language: String?
    /// Show the floating button on the Mac. Off by default: the button lives on
    /// the PC and this app only lends it the microphone.
    var button: Bool?

    static let url = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/voxbutton/config.json")

    static func load() -> Config? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(Config.self, from: data)
    }
}

enum State { case idle, recording, busy, done, error }

final class Panel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class ButtonView: NSView {
    var state: State = .idle { didSet { needsDisplay = true } }
    var level: CGFloat = 0 { didSet { needsDisplay = true } }
    var onClick: (() -> Void)?
    var onMenu: ((NSEvent) -> Void)?
    private var downAt: NSPoint?
    private var dragged = false

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 4, dy: 4)
        let fill: NSColor = switch state {
        case .idle: NSColor(white: 0.15, alpha: 0.85)
        case .recording: NSColor.systemRed
        case .busy: NSColor.systemOrange
        case .done: NSColor.systemGreen
        case .error: NSColor.systemPurple
        }
        if state == .recording {
            // Halo that follows the mic level, so you can see it's hearing you.
            let grow = 4 * level
            fill.withAlphaComponent(0.35).setFill()
            NSBezierPath(ovalIn: r.insetBy(dx: -grow, dy: -grow).intersection(bounds)).fill()
        }
        fill.setFill()
        NSBezierPath(ovalIn: r.insetBy(dx: 4, dy: 4)).fill()

        let symbol = switch state {
        case .idle, .recording: "mic.fill"
        case .busy: "ellipsis"
        case .done: "checkmark"
        case .error: "exclamationmark.triangle.fill"
        }
        let cfg = NSImage.SymbolConfiguration(pointSize: r.width * 0.32, weight: .semibold)
            .applying(.init(paletteColors: [.white]))
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(cfg) {
            let s = img.size
            img.draw(in: NSRect(x: bounds.midX - s.width / 2, y: bounds.midY - s.height / 2,
                                width: s.width, height: s.height))
        }
    }

    override func mouseDown(with event: NSEvent) {
        downAt = NSEvent.mouseLocation
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard let start = downAt, let win = window else { return }
        let now = NSEvent.mouseLocation
        let dx = now.x - start.x, dy = now.y - start.y
        if !dragged && hypot(dx, dy) < 4 { return }
        dragged = true
        win.setFrameOrigin(NSPoint(x: win.frame.origin.x + dx, y: win.frame.origin.y + dy))
        downAt = now
    }

    override func mouseUp(with event: NSEvent) {
        if dragged {
            if let o = window?.frame.origin {
                UserDefaults.standard.set([o.x, o.y], forKey: "origin")
            }
        } else {
            onClick?()
        }
        downAt = nil
    }

    override func rightMouseDown(with event: NSEvent) { onMenu?(event) }
}

final class App: NSObject, NSApplicationDelegate {
    let size: CGFloat = 64
    var panel: Panel!
    var view: ButtonView!
    var recorder: AVAudioRecorder?
    var listener: Listener?
    var meter: Timer?
    var resetTimer: Timer?
    let file = FileManager.default.temporaryDirectory.appendingPathComponent("voxbutton.wav")
    var pollTask: URLSessionDataTask?

    func applicationDidFinishLaunching(_ note: Notification) {
        var origin = NSPoint(x: 40, y: 200)
        if let saved = UserDefaults.standard.array(forKey: "origin") as? [CGFloat], saved.count == 2 {
            origin = NSPoint(x: saved[0], y: saved[1])
        }
        panel = Panel(contentRect: NSRect(origin: origin, size: NSSize(width: size, height: size)),
                      styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        // isFloatingPanel resets the level to .floating, so it has to come first.
        panel.isFloatingPanel = true
        // Above everything, including fullscreen apps like Moonlight, on every Space.
        panel.level = .screenSaver
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true

        view = ButtonView(frame: NSRect(x: 0, y: 0, width: size, height: size))
        view.onClick = { [weak self] in self?.toggle() }
        view.onMenu = { [weak self] e in self?.showMenu(e) }
        panel.contentView = view
        if Config.load()?.button == true {
            panel.orderFrontRegardless()
        }

        AVCaptureDevice.requestAccess(for: .audio) { _ in }
        if Config.load() == nil {
            flash(.error)
            alert("No config found",
                  "Create \(Config.url.path) with:\n{\"server\": \"http://<pc-ip>:8765\", \"token\": \"…\"}")
        }
        pollAgent()
        // Priority depends on the front app, so tell the server right away when it changes.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.recorder == nil else { return }
            self.pollTask?.cancel()
        }
    }

    /// 2 while Moonlight is in front (you're at the Mac driving the PC), else 0,
    /// so the iPhone gets the recording instead.
    var priority: Int {
        let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""
        return front.lowercased().contains("moonlight") ? 2 : 0
    }

    /// Long-polls the server for "start"/"stop" from the button on the PC.
    func pollAgent() {
        guard let cfg = Config.load(),
              var comps = URLComponents(string: cfg.server + "/agent/wait") else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.pollAgent() }
            return
        }
        comps.queryItems = [.init(name: "name", value: "mac"), .init(name: "prio", value: String(priority))]
        var req = URLRequest(url: comps.url!, timeoutInterval: 40)
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        let task = URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            let cancelled = (err as? URLError)?.code == .cancelled
            let ok = cancelled || (err == nil && (resp as? HTTPURLResponse)?.statusCode == 200)
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let cmd = json?["cmd"] as? String
            DispatchQueue.main.async {
                guard let self else { return }
                switch cmd {
                case "listen" where self.recorder == nil && self.listener == nil:
                    let l = Listener(pause: json?["pause"] as? Double ?? 0.6, chunk: json?["chunk"] as? Double ?? 6) {
                        [weak self] wav in DispatchQueue.main.async { self?.upload(wav, segment: true) }
                    }
                    if l.start() { self.listener = l } else { self.reportAgentError("can't listen: check microphone permission") }
                case "stop" where self.listener != nil:
                    self.listener?.stop()
                    self.listener = nil
                case "start" where self.recorder == nil:
                    if !self.start() { self.reportAgentError("can't record: check microphone permission") }
                case "stop" where self.recorder != nil:
                    self.stopAndSend()
                case "start", "stop", "listen":
                    self.reportAgentError("got \(cmd!) while \(self.recorder == nil ? "idle" : "recording")")
                default:
                    break
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + (ok ? 0 : 3)) { self.pollAgent() }
            }
        }
        pollTask = task
        task.resume()
    }

    func reportAgentError(_ msg: String) {
        guard let cfg = Config.load(), let url = URL(string: cfg.server)?.appendingPathComponent("agent/error") else {
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        URLSession.shared.uploadTask(with: req, from: Data(msg.utf8)).resume()
    }

    func toggle() {
        switch view.state {
        case .recording: stopAndSend()
        case .busy: break
        default: start()
        }
    }

    @discardableResult
    func start() -> Bool {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        do {
            let rec = try AVAudioRecorder(url: file, settings: settings)
            rec.isMeteringEnabled = true
            guard rec.record() else { throw NSError(domain: "voxbutton", code: 1) }
            recorder = rec
            resetTimer?.invalidate()
            view.state = .recording
            meter = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
                guard let self, let r = self.recorder else { return }
                r.updateMeters()
                let db = r.averagePower(forChannel: 0)  // -160...0
                self.view.level = CGFloat(max(0, min(1, (db + 50) / 50)))
            }
            return true
        } catch {
            flash(.error)
            if Config.load()?.button == true {
                alert("Can't record", "Check microphone permission in System Settings → Privacy & Security.")
            }
            return false
        }
    }

    func stopAndSend() {
        recorder?.stop()
        recorder = nil
        meter?.invalidate()
        view.level = 0
        guard let audio = try? Data(contentsOf: file) else {
            flash(.error)
            reportAgentError("couldn't read the recording")
            return
        }
        upload(audio, segment: false)
    }

    /// Sends audio to be transcribed; `segment` marks a piece of "always" listening.
    func upload(_ audio: Data, segment: Bool) {
        guard let cfg = Config.load(), let url = URL(string: cfg.server)?.appendingPathComponent("transcribe") else {
            flash(.error)
            return
        }
        if !segment { view.state = .busy }
        var req = URLRequest(url: url, timeoutInterval: 120)
        req.httpMethod = "POST"
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        req.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Agent")
        if segment { req.setValue("1", forHTTPHeaderField: "X-Segment") }
        if let lang = cfg.language { req.setValue(lang, forHTTPHeaderField: "X-Language") }
        URLSession.shared.uploadTask(with: req, from: audio) { [weak self] data, resp, err in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            if !ok {
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? err?.localizedDescription ?? "?"
                NSLog("voxbutton: request failed: \(body)")
            }
            DispatchQueue.main.async { self?.flash(ok ? .done : .error) }
        }.resume()
    }

    func flash(_ s: State) {
        view.state = s
        resetTimer?.invalidate()
        resetTimer = Timer.scheduledTimer(withTimeInterval: s == .error ? 2.5 : 0.8, repeats: false) {
            [weak self] _ in
            if let self, self.view.state == s { self.view.state = .idle }
        }
    }

    func showMenu(_ e: NSEvent) {
        let menu = NSMenu()
        menu.addItem(withTitle: "Quit VoxButton", action: #selector(NSApplication.terminate(_:)),
                     keyEquivalent: "")
        NSMenu.popUpContextMenu(menu, with: e, for: view)
    }

    func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        NSApp.activate(ignoringOtherApps: true)
        a.runModal()
    }
}

/// "Always on": keeps the mic open and hands over what you say as 16 kHz mono
/// WAVs: at every pause, and every `chunk` seconds while you keep talking (cut
/// at the quietest moment of the last 1.5 s, so words stay whole).
final class Listener {
    static let speech: Float = -40  // dBFS: louder than this counts as talking
    static let preroll = 0.3  // s kept from before the speech started
    static let bytesPerSecond = 32000.0

    private let pause: Double
    private let chunk: Double
    private let engine = AVAudioEngine()
    private let onSegment: (Data) -> Void
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private var ring = Data()  // recent audio before speech starts
    private var current = Data()
    private var frames: [(end: Int, level: Float)] = []  // per buffer in `current`
    private var talking = false
    private var quiet = 0.0

    init(pause: Double, chunk: Double, onSegment: @escaping (Data) -> Void) {
        self.pause = pause
        self.chunk = chunk
        self.onSegment = onSegment
    }

    func start() -> Bool {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, let conv = AVAudioConverter(from: format, to: target) else { return false }
        converter = conv
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buf, _ in self?.feed(buf) }
        do {
            try engine.start()
            return true
        } catch {
            input.removeTap(onBus: 0)
            return false
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if talking { send(upTo: current.count) }
    }

    private func feed(_ buf: AVAudioPCMBuffer) {
        guard let converter else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * target.sampleRate / buf.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
        var fed = false
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buf
        }
        guard let p = out.int16ChannelData, out.frameLength > 0 else { return }
        let n = Int(out.frameLength)
        var sum: Float = 0
        for i in 0..<n { let v = Float(p[0][i]) / 32768; sum += v * v }
        let level = 10 * log10(max(sum / Float(n), 1e-12))
        let bytes = Data(bytes: p[0], count: n * 2)

        if talking {
            current.append(bytes)
            frames.append((current.count, level))
            quiet = level < Self.speech ? quiet + Double(n) / target.sampleRate : 0
            if quiet >= pause {
                send(upTo: current.count)
            } else if Double(current.count) / Self.bytesPerSecond >= chunk {
                send(upTo: quietestCut())
            }
        } else if level >= Self.speech {
            talking = true
            quiet = 0
            current = ring + bytes
            frames = [(ring.count, -120), (current.count, level)]
        } else {
            ring.append(bytes)
            let keep = Int(Self.preroll * Self.bytesPerSecond) & ~1
            if ring.count > keep { ring = ring.suffix(keep) }
        }
    }

    /// End of the quietest buffer in the last 1.5 s: the gap between two words.
    private func quietestCut() -> Int {
        let from = current.count - Int(1.5 * Self.bytesPerSecond)
        let candidates = frames.filter { $0.end >= from && $0.end < current.count }
        return candidates.min { $0.level < $1.level }?.end ?? current.count
    }

    /// Sends current[..<cut]; whatever follows stays as the start of the next piece.
    private func send(upTo cut: Int) {
        let piece = Data(current.prefix(cut))
        if cut >= current.count {
            talking = false
            current = Data()
            frames = []
            ring = Data()
        } else {
            current = Data(current.suffix(from: current.startIndex + cut))
            frames = frames.filter { $0.end > cut }.map { ($0.end - cut, $0.level) }
        }
        if Double(piece.count) / Self.bytesPerSecond >= 0.4 { onSegment(Listener.wav(piece)) }
    }

    static func wav(_ pcm: Data) -> Data {
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

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
