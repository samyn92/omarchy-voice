import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.samyn92.omarchy-voice"

  // the glowing orb above the bottom edge — its own layer surface, fed by the engine's socket
  Orb {}

  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice"
  readonly property string statePath: runtimeDir + "/state.json"
  readonly property string controlCli: Quickshell.env("HOME") + "/.local/bin/omarchy-voice"
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color barFg: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property string status: "stopped"
  property bool listening: false
  property bool yoloMode: false
  property bool modelReady: false
  property string transcript: ""
  property string message: "Voice control is stopped"
  property string pendingAction: ""
  property string pendingLabel: ""
  property string lastActionLabel: ""
  property bool lastOk: true
  property string mode: ""
  property string voxtypeState: ""
  property bool jevOnly: false
  // the engine is installed by install.sh next to this file; until then the panel offers to run it
  property bool engineInstalled: true
  readonly property string installScript: String(Qt.resolvedUrl("install.sh")).replace("file://", "")
  property string transcribeKind: "long"
  property real transcribeSilence: 3
  property string autopilotGoal: ""
  property int autopilotStep: 0
  property string autopilotLabel: ""
  property int closeSeq: -1
  property int latencyMs: 0
  property var traceEntries: []
  property var traceStats: ({})
  readonly property string tracePath: runtimeDir + "/trace.json"
  readonly property var stages: [
    { key: "listening", label: "Hear", icon: "󰍬" },
    { key: "transcribing", label: "Transcribe", icon: "󰗊" },
    { key: "deciding", label: "Decide", icon: "󰧑" },
    { key: "acting", label: "Act", icon: "󰆽" }
  ]
  readonly property var routeInfo: ({
    "local": { label: "LOCAL", tip: "phrase matched on this machine" },
    "fuzzy": { label: "FUZZY", tip: "sound-alike match on this machine" },
    "jev": { label: "JEV", tip: "decided by Jev" },
    "unclear": { label: "UNCLEAR", tip: "no confident decision" },
    "ignored": { label: "IGNORED", tip: "not addressed to the computer" },
    "waiting": { label: "WAITING", tip: "unfinished — joined with the next phrase" },
    "confirm": { label: "CONFIRM", tip: "waiting for your confirmation" },
    "voxtype": { label: "VOXTYPE", tip: "part of a voxtype transcription, not a command" }
  })

  function parseTrace(raw) {
    try {
      var parsed = JSON.parse(String(raw || "{}"))
      traceEntries = parsed.entries || []
      traceStats = parsed.stats || {}
    } catch (error) {}
  }
  function ago(epoch) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - epoch))
    if (s < 60) return s + "s"
    if (s < 3600) return Math.round(s / 60) + "m"
    return Math.round(s / 3600) + "h"
  }
  function timing(ms) {
    if (!ms) return ""
    var parts = []
    var order = [["stt_fast", "whisper-s"], ["stt", "whisper-L"], ["scene_wait", "look"], ["jev", "jev"], ["exec", "act"]]
    for (var i = 0; i < order.length; i++) {
      var v = ms[order[i][0]]
      if (v !== undefined && v !== null) parts.push(order[i][1] + " " + Math.round(v))
    }
    return parts.join(" · ") + (ms.e2e ? "  =  " + ms.e2e + " ms" : "")
  }
  property var queuedArgs: []
  property bool popupOpen: false

  readonly property bool busy: commandProc.running
    || status === "starting"
    || status === "loading"
    || status === "transcribing"
    || status === "deciding"
    || status === "acting"
  readonly property bool confirming: pendingAction !== ""
  readonly property bool opened: popupOpen
  readonly property string statusTitle: {
    if (confirming) return "Confirmation needed"
    if (mode === "transcribe") return transcribeKind === "quick" ? "Transcribing · quick" : "Transcribing"
    if (voxtypeState === "recording") return "Dictating"
    if (mode === "dictation") return "Dictation"
    if (mode === "hints") return "Say a number"
    if (mode === "grid") return "Grid"
    if (status === "loading" || status === "starting") return "Loading local model"
    if (status === "transcribing") return "Transcribing"
    if (status === "deciding") return "Understanding"
    if (status === "acting") return "Running action"
    if (status === "error") return "Voice control error"
    if (listening) return jevOnly ? "Listening · Jev only" : "Listening"
    return modelReady ? "Paused" : "Stopped"
  }
  readonly property var lastEntry: traceEntries.length > 0 ? traceEntries[0] : null
  readonly property bool voxtypeActive: mode === "transcribe" || voxtypeState === "recording" || voxtypeState === "transcribing"
  readonly property string statusDetail: {
    if (confirming) return pendingLabel
    if (transcript !== "" && listening) return "Heard: “" + transcript + "”" + (latencyMs > 0 ? " · " + latencyMs + " ms" : "")
    return message
  }

  function parseState(raw) {
    var parsed
    try {
      parsed = JSON.parse(String(raw || "{}"))
    } catch (error) {
      return
    }
    status = String(parsed.status || "stopped")
    listening = parsed.listening === true
    yoloMode = parsed.yolo_mode === true
    modelReady = parsed.model_ready === true
    transcript = String(parsed.transcript || "")
    message = String(parsed.message || "")
    pendingAction = String(parsed.pending_action || "")
    pendingLabel = String(parsed.pending_label || "")
    lastActionLabel = String(parsed.last_action_label || "")
    lastOk = parsed.last_ok !== false
    mode = String(parsed.mode || "")
    voxtypeState = String(parsed.voxtype || "")
    jevOnly = parsed.jev_only === true
    transcribeKind = String(parsed.transcribe_kind || "long")
    transcribeSilence = Number(parsed.transcribe_silence_s || 3)
    autopilotGoal = String(parsed.autopilot_goal || "")
    autopilotStep = Number(parsed.autopilot_step || 0)
    autopilotLabel = String(parsed.autopilot_label || "")
    // the daemon is about to type: an open popup would swallow the keystrokes
    var seq = Number(parsed.close_panel || 0)
    if (closeSeq >= 0 && seq !== closeSeq) popupOpen = false
    closeSeq = seq
    latencyMs = Number(parsed.latency_ms || 0)
  }

  function open() { popupOpen = true }
  function close() { popupOpen = false }
  function refresh() { stateFile.reload(); traceFile.reload() }
  function run(args) {
    if (commandProc.running) {
      queuedArgs = args
      return
    }
    commandProc.command = [controlCli].concat(args)
    commandProc.running = true
  }
  function toggleListening() { run(["toggle"]) }
  function toggleYolo() { run(["yolo"]) }
  function toggleJevOnly() { run(["jev-only"]) }
  function runAction(actionId) { run(["action", actionId]) }
  function confirmAction() { run(["confirm"]) }
  function cancelAction() { run(["cancel"]) }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: engineCheck
    command: ["test", "-x", root.controlCli]
    running: true
    onExited: function(exitCode) { root.engineInstalled = exitCode === 0 }
  }

  FileView {
    id: traceFile
    path: root.tracePath
    watchChanges: true
    printErrors: false
    onLoaded: root.parseTrace(text())
    onFileChanged: reload()
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onLoaded: root.parseState(text())
    onFileChanged: reload()
    onLoadFailed: {
      root.status = "stopped"
      root.listening = false
      root.modelReady = false
      root.message = "Voice control is stopped"
      root.pendingAction = ""
      root.pendingLabel = ""
    }
  }

  Process {
    id: commandProc
    running: false
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var problem = String(text || "").trim()
        if (problem !== "") root.message = problem
      }
    }
    onExited: function(exitCode) {
      root.refresh()
      if (root.queuedArgs.length > 0) {
        var next = root.queuedArgs
        root.queuedArgs = []
        Qt.callLater(function() { root.run(next) })
      } else if (exitCode !== 0) {
        root.lastOk = false
      }
    }
  }

  Process {
    id: installProc
    command: ["xdg-terminal-exec", "bash", "-c", root.installScript + "; echo; read -r -p 'Press Enter to close'"]
    running: false
    onExited: engineCheck.running = true
  }

  Timer {
    interval: root.busy || root.listening ? 350 : 1500
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.confirming ? "󰋼" : (root.mode === "transcribe" || root.voxtypeState === "recording" ? "󰗊" : (root.mode === "dictation" ? "󰌌" : (root.listening ? "󰍬" : "󰍭")))
    foreground: root.confirming ? Color.accent : (root.listening ? root.barFg : Qt.darker(root.barFg, 1.55))
    tooltipText: "Voice control: " + root.statusTitle + " · left click for controls, right click to toggle"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleListening()
      else root.popupOpen = !root.popupOpen
    }

    SequentialAnimation on opacity {
      running: root.listening && (root.status === "listening" || root.status === "transcribing")
      loops: Animation.Infinite
      NumberAnimation { to: 0.45; duration: 700; easing.type: Easing.InOutQuad }
      NumberAnimation { to: 1.0; duration: 700; easing.type: Easing.InOutQuad }
      onStopped: button.opacity = 1
    }
  }

  PopupCard {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    triggerMode: "click"
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(520))
    contentHeight: popup.fittedContentHeight(Math.min(content.implicitHeight, Style.space(820)), Style.space(860))

    Flickable {
      id: flick
      width: parent.width
      height: Math.min(content.implicitHeight, Style.space(820))
      contentWidth: width
      contentHeight: content.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height

      Column {
        id: content
        width: flick.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "Voice control"
          meta: root.engineInstalled ? root.statusTitle : "Not set up"
          detail: ""
          foreground: root.fg
          fontFamily: root.fontFamily
          iconOpacity: root.listening ? 1 : 0.5

          iconComponent: Component {
            Text {
              text: root.confirming ? "󰋼" : (root.listening ? "󰍬" : "󰍭")
              color: root.confirming ? Color.accent : root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }

          trailingControl: Component {
            ToggleSwitch {
              checked: root.listening
              busy: root.busy
              foreground: root.fg
              onToggled: root.toggleListening()
            }
          }
        }

        Column {
          visible: root.confirming
          width: parent.width
          spacing: Style.space(8)

          Text {
            text: "CONFIRM ACTION"
            color: Color.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.font.caption * 0.1
          }

          Text {
            width: parent.width
            text: root.pendingLabel
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            wrapMode: Text.Wrap
          }

          Row {
            spacing: Style.space(8)
            anchors.horizontalCenter: parent.horizontalCenter

            ControlButton {
              icon: "󰅖"
              label: "Cancel"
              onActivated: root.cancelAction()
            }
            ControlButton {
              icon: "󰄬"
              label: "Confirm"
              primary: true
              onActivated: root.confirmAction()
            }
          }
        }

        CommandRow {
          visible: !root.engineInstalled
          icon: "󰏗"
          label: "Set up Omarchy Voice"
          detail: "Installs the voice engine: speech models, service, API key (opens a terminal)"
          onActivated: {
            root.popupOpen = false
            installProc.running = true
          }
        }

        PanelSeparator { foreground: root.fg }

        // live card: where the current utterance is, and what happened to the last one.
        // While voxtype owns the microphone it turns into one calm banner instead.
        BorderSurface {
          width: parent.width
          implicitHeight: liveCol.implicitHeight + Style.space(16)
          radius: Style.cornerRadius
          color: root.voxtypeActive ? Style.selectedFillFor(root.fg, Color.accent) : "transparent"
          borderSpec: Border.controlSpec(root.voxtypeActive ? "selected" : "normal", root.fg, Color.accent)

          Column {
            id: liveCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            spacing: Style.space(6)

            // working towards a goal
            Text {
              visible: root.autopilotGoal !== ""
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: "󰓅  " + root.autopilotGoal
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              visible: root.autopilotGoal !== ""
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: (root.autopilotStep > 0 ? root.autopilotStep + ". " + root.autopilotLabel + " · " : "")
                    + "say “stop” to end it"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }

            // voxtype banner
            Text {
              visible: root.voxtypeActive
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: "󰗊  " + (root.voxtypeState === "transcribing" ? "voxtype is typing your text…"
                    : (root.mode === "transcribe" && root.transcribeKind === "quick" ? "Quick transcription" : "voxtype is recording"))
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
            Text {
              visible: root.voxtypeActive
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: root.mode !== "transcribe" ? "Commands are paused while you dictate with F9"
                    : (root.transcribeKind === "quick"
                       ? "Ends after " + root.transcribeSilence + " s of silence · “end transcribe send” submits"
                       : "Say “end transcribe” to finish · “end transcribe send” to submit · “cancel transcription”")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }

            // pipeline stages
            Row {
              visible: !root.voxtypeActive
              width: parent.width
              spacing: Style.space(4)
              Repeater {
                model: root.stages
                delegate: Item {
                  required property var modelData
                  required property int index
                  readonly property bool active: root.listening && root.status === modelData.key
                  width: (parent.width - Style.space(4) * 3) / 4
                  implicitHeight: stageRow.implicitHeight + Style.space(6)
                  Rectangle {
                    anchors.fill: parent
                    radius: Style.cornerRadius
                    color: parent.active ? Style.selectedFillFor(root.fg, Color.accent) : "transparent"
                  }
                  Row {
                    id: stageRow
                    anchors.centerIn: parent
                    spacing: Style.space(4)
                    Text {
                      text: modelData.icon
                      color: parent.parent.active ? Color.accent : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                    Text {
                      text: modelData.label
                      color: parent.parent.active ? root.fg : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: parent.parent.active
                    }
                  }
                }
              }
            }

            // the last thing heard, and what came of it
            Row {
              visible: !root.voxtypeActive && root.lastEntry !== null
              width: parent.width
              spacing: Style.space(6)
              Text {
                text: root.lastEntry ? (root.routeInfo[root.lastEntry.route] || { label: String(root.lastEntry.route).toUpperCase() }).label : ""
                color: root.lastEntry && (root.lastEntry.route === "jev" || root.lastEntry.route === "fuzzy") ? Color.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
              Text {
                width: parent.width - Style.space(150)
                text: root.lastEntry ? "“" + root.lastEntry.heard + "”" : ""
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }
              Text {
                text: root.lastEntry && root.lastEntry.ms && root.lastEntry.ms.e2e ? root.lastEntry.ms.e2e + " ms" : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
            Text {
              visible: !root.voxtypeActive && root.lastEntry !== null
              width: parent.width
              text: root.lastEntry ? (root.lastEntry.action ? "→ " + root.lastEntry.action : "· " + (root.lastEntry.message || "no action")) : ""
              color: root.lastEntry && root.lastEntry.ok === false ? Color.accent : root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: root.lastEntry !== null && root.lastEntry.action !== ""
              elide: Text.ElideRight
            }
            Text {
              visible: !root.voxtypeActive && root.lastEntry === null
              width: parent.width
              text: root.listening ? "Say something — “scroll down”, “open spotify”, “transcribe”…" : "Right-click the microphone in the bar, or flip the switch, to start listening."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }
          }
        }

        // numbers that say whether it is working
        Row {
          width: parent.width
          spacing: Style.space(6)
          Repeater {
            model: [
              { value: (root.traceStats.median_ms || 0) + " ms", label: "median" },
              { value: (root.traceStats.p90_ms || 0) + " ms", label: "p90" },
              { value: (root.traceStats.local_pct || 0) + "%", label: "on-device" },
              { value: (root.traceStats.jev_today || 0) + " · $" + Number(root.traceStats.cost_today || 0).toFixed(3), label: "Jev today" }
            ]
            delegate: Column {
              required property var modelData
              width: (parent.width - Style.space(6) * 3) / 4
              spacing: Style.space(1)
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: modelData.value
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                text: modelData.label
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(6)
          ControlButton { width: (parent.width - Style.space(6) * 3) / 4; icon: "󰗊"; label: root.mode === "transcribe" ? "End" : "Transcribe"; primary: root.mode === "transcribe"; onActivated: root.run(["speak", root.mode === "transcribe" ? "end transcribe" : "start transcribe"]) }
          ControlButton { width: (parent.width - Style.space(6) * 3) / 4; icon: "󰎠"; label: "Hints"; primary: root.mode === "hints"; onActivated: root.run(root.mode === "hints" ? ["clear"] : ["hints"]) }
          ControlButton { width: (parent.width - Style.space(6) * 3) / 4; icon: root.yoloMode ? "󰚐" : "󰋼"; label: root.yoloMode ? "YOLO" : "Safe"; primary: root.yoloMode; onActivated: root.toggleYolo() }
          ControlButton { width: (parent.width - Style.space(6) * 3) / 4; icon: "󰧑"; label: "Jev only"; primary: root.jevOnly; onActivated: root.toggleJevOnly() }
        }

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          text: "Quick “transcribe” ends after " + root.transcribeSilence + " s of silence  ⟳"
          color: silenceHover.hovered ? root.fg : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          HoverHandler { id: silenceHover }
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.run(["transcribe-silence"])   // cycles 0.5 → 1 → 2 → 3 → 5 s
          }
        }

        PanelSeparator { foreground: root.fg }

        Text {
          text: "TRACE · " + (root.traceStats.heard || 0) + " heard · " + (root.traceStats.commands || 0) + " acted · " + (root.traceStats.failed || 0) + " failed"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: Style.font.caption * 0.1
        }

        Text {
          visible: root.traceEntries.length === 0
          width: parent.width
          text: "Nothing heard yet. Right-click the mic to start listening."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        Repeater {
          model: root.traceEntries
          delegate: BorderSurface {
            id: entryCard
            required property var modelData
            readonly property var info: root.routeInfo[modelData.route] || { label: String(modelData.route || "?").toUpperCase(), tip: "" }
            readonly property bool quiet: modelData.route === "ignored" || modelData.route === "waiting"
            readonly property bool failed: modelData.ok === false
            width: parent.width
            implicitHeight: entryCol.implicitHeight + Style.space(12)
            radius: Style.cornerRadius
            color: failed ? Style.normalFillFor(root.fg, Color.accent) : "transparent"
            borderSpec: Border.controlSpec("normal", root.fg, Color.accent)
            opacity: quiet ? 0.55 : 1

            Column {
              id: entryCol
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(2)

              Row {
                width: parent.width
                spacing: Style.space(6)
                Text {
                  text: entryCard.info.label
                  color: modelData.route === "jev" || modelData.route === "fuzzy" ? Color.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
                Text {
                  width: parent.width - Style.space(110)
                  text: (modelData.heard.charAt(0) === "(" ? modelData.heard : "“" + modelData.heard + "”") + (modelData.count > 1 ? "  ×" + modelData.count : "")
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
                Text {
                  text: (modelData.ok === true ? "✓ " : (modelData.ok === false ? "✗ " : "")) + root.ago(modelData.at)
                  color: entryCard.failed ? Color.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              Text {
                visible: text !== ""
                width: parent.width
                text: (modelData.action ? "→ " + modelData.action : (modelData.message ? "· " + modelData.message : ""))
                      + (modelData.fast && modelData.fast !== modelData.heard ? "   (also heard “" + modelData.fast + "”)" : "")
                color: entryCard.failed ? Color.accent : root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: modelData.action !== ""
                elide: Text.ElideRight
              }
              Text {
                visible: text !== ""
                width: parent.width
                text: [modelData.detail || "", modelData.scene || "", root.timing(modelData.ms), modelData.cost ? "$" + Number(modelData.cost).toFixed(5) : ""]
                      .filter(function(x) { return x !== "" }).join("  ·  ")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }
        }
      }
    }
  }

  component ControlButton: BorderSurface {
    id: control
    property string icon: ""
    property string label: ""
    property bool primary: false
    signal activated()

    width: Style.space(112)
    implicitHeight: labels.implicitHeight + Style.space(14)
    radius: Style.cornerRadius
    color: primary
      ? Style.selectedFillFor(root.fg, Color.accent)
      : (hover.hovered ? Style.normalFillFor(root.fg, Color.accent) : "transparent")
    borderSpec: Border.controlSpec(primary ? "selected" : "normal", root.fg, Color.accent)

    Row {
      id: labels
      anchors.centerIn: parent
      spacing: Style.space(6)
      Text {
        text: control.icon
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
      Text {
        text: control.label
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: control.primary
      }
    }
    HoverHandler { id: hover }
    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: control.activated()
    }
  }

  component CommandRow: BorderSurface {
    id: command
    property string icon: ""
    property string label: ""
    property string detail: ""
    signal activated()

    width: parent.width
    implicitHeight: row.implicitHeight + Style.space(12)
    radius: Style.cornerRadius
    color: commandHover.hovered ? Style.normalFillFor(root.fg, Color.accent) : "transparent"
    borderSpec: Border.none()

    Row {
      id: row
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)
      Text {
        width: Style.space(22)
        anchors.verticalCenter: parent.verticalCenter
        text: command.icon
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
        horizontalAlignment: Text.AlignHCenter
      }
      Column {
        width: parent.width - Style.space(32)
        anchors.verticalCenter: parent.verticalCenter
        Text {
          width: parent.width
          text: command.label
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideRight
        }
        Text {
          width: parent.width
          text: command.detail
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
    HoverHandler { id: commandHover }
    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: command.activated()
    }
  }
}
