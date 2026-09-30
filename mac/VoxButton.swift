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
    var meter: Timer?
    var resetTimer: Timer?
    let file = FileManager.default.temporaryDirectory.appendingPathComponent("voxbutton.wav")

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
        panel.orderFrontRegardless()

        AVCaptureDevice.requestAccess(for: .audio) { _ in }
        if Config.load() == nil {
            flash(.error)
            alert("No config found",
                  "Create \(Config.url.path) with:\n{\"server\": \"http://<pc-ip>:8765\", \"token\": \"…\"}")
        }
    }

    func toggle() {
        switch view.state {
        case .recording: stopAndSend()
        case .busy: break
        default: start()
        }
    }

    func start() {
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
        } catch {
            flash(.error)
            alert("Can't record", "Check microphone permission in System Settings → Privacy & Security.")
        }
    }

    func stopAndSend() {
        recorder?.stop()
        recorder = nil
        meter?.invalidate()
        view.level = 0
        guard let cfg = Config.load(), let url = URL(string: cfg.server)?.appendingPathComponent("transcribe"),
              let audio = try? Data(contentsOf: file) else {
            flash(.error)
            return
        }
        view.state = .busy
        var req = URLRequest(url: url, timeoutInterval: 120)
        req.httpMethod = "POST"
        req.setValue("Bearer \(cfg.token)", forHTTPHeaderField: "Authorization")
        req.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
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

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
