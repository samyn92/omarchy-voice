// The orb: a glowing circle above the bottom edge while voice control listens, with a ring
// that shows the last two seconds of your voice.
//
// It is drawn by the shell itself rather than by the engine, so every frame is Qt's scene
// graph on the GPU, locked to the monitor's refresh.  The engine only streams
// {state, level} lines over a socket, at most one per 25 ms; the animation runs on its own
// and interpolates between them, which is what makes it look continuous.
import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

Scope {
  id: root

  readonly property string socketPath: Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-voice/orb.sock"

  property string state: "off"          // off · listening · hearing · thinking · acting · transcribe · error
  property real level: 0                // right now, 0…1
  property var levels: new Array(72).fill(0)   // the last ~1.8 s, newest first
  property double lastSeen: 0                  // when the engine last said anything

  readonly property bool shown: state !== "off"
  readonly property color accent: state === "error" ? Color.yellow : Color.accent

  // One sample every 25 ms whatever the socket does: time passes even when nobody speaks,
  // and the ring has to keep moving or it looks frozen.
  Timer {
    interval: 25
    running: root.shown
    repeat: true
    onTriggered: {
      const next = root.levels.slice(0, 71)
      next.unshift(root.level)
      root.levels = next
    }
  }

  Socket {
    id: stream
    path: root.socketPath
    connected: true
    parser: SplitParser {
      onRead: line => {
        try {
          const msg = JSON.parse(line)
          root.lastSeen = Date.now()
          root.state = String(msg.state || "off")
          root.level = Math.max(0, Math.min(1, Number(msg.level) || 0))
        } catch (e) {
          // a half-written line is not worth a log entry
        }
      }
    }
  }

  // The engine restarts, the shell does not: reconnect until it answers again. A failed
  // connect leaves `connected` true with nothing behind it, so the socket is cycled rather
  // than just switched on.
  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      if (Date.now() - root.lastSeen < 4000)
        return
      root.state = "off"          // nothing is listening: take the orb off the screen
      stream.connected = false
      stream.connected = true
    }
  }

  PanelWindow {
    id: panel
    visible: root.shown
    anchors { bottom: true }
    implicitWidth: 320
    implicitHeight: 320
    color: "transparent"
    WlrLayershell.namespace: "omarchy-voice-orb"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region {}        // visual only: never take a click from the window below

    Item {
      id: orb
      anchors.centerIn: parent
      width: 300
      height: 300

      readonly property real cx: width / 2
      readonly property real cy: height / 2
      readonly property real ringRadius: 44
      readonly property real coreRadius: {
        switch (root.state) {
        case "hearing": return 13 + 13 * root.level
        case "transcribe": return 15 + 11 * root.level
        case "acting": return 30
        case "thinking": return 15
        default: return 13
        }
      }

      // breathing while it waits, a quicker beat while it decides
      property real breath: 1
      SequentialAnimation on breath {
        running: root.state === "listening" || root.state === "transcribe"
        loops: Animation.Infinite
        NumberAnimation { to: 1.12; duration: 1600; easing.type: Easing.InOutSine }
        NumberAnimation { to: 1.0; duration: 1600; easing.type: Easing.InOutSine }
      }
      SequentialAnimation on breath {
        running: root.state === "thinking"
        loops: Animation.Infinite
        NumberAnimation { to: 1.25; duration: 420; easing.type: Easing.InOutSine }
        NumberAnimation { to: 1.0; duration: 420; easing.type: Easing.InOutSine }
      }

      // the meter: one tick per 25 ms, newest at the top, older fading clockwise
      Repeater {
        model: 72
        delegate: Rectangle {
          required property int index
          readonly property real ticks: 2.5 + 26 * (root.levels[index] || 0)
          width: 3
          radius: 1.5
          height: ticks
          color: root.accent
          opacity: root.shown ? Math.pow(1 - index / 72, 1.6) * 0.85 + 0.06 : 0
          x: orb.cx - width / 2
          y: orb.cy - orb.ringRadius - height
          // no Behavior here on purpose: 72 ticks animating would restart 2880 animations a
          // second on the GUI thread. The samples arrive every 25 ms, which is the motion.
          transform: Rotation {
            origin.x: 1.5
            origin.y: height + orb.ringRadius
            angle: index * 5
          }
        }
      }

      // where the loudest moment of the last second was
      Rectangle {
        readonly property real peak: Math.max.apply(null, root.levels.slice(0, 40).concat([0]))
        width: 2 * (orb.ringRadius + 4 + 26 * peak)
        height: width
        x: orb.cx - width / 2
        y: orb.cy - height / 2
        radius: width / 2
        color: "transparent"
        border.width: 1
        border.color: root.accent
        opacity: 0.22
        visible: root.shown
        Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutQuad } }
      }

      // a ring that goes round while Jev decides
      Shape {
        anchors.fill: parent
        visible: root.state === "thinking"
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
          strokeColor: root.accent
          strokeWidth: 2.5
          fillColor: "transparent"
          capStyle: ShapePath.RoundCap
          PathAngleArc {
            centerX: orb.cx
            centerY: orb.cy
            radiusX: orb.coreRadius * orb.breath + 9
            radiusY: orb.coreRadius * orb.breath + 9
            startAngle: 0
            sweepAngle: 80
          }
        }
        RotationAnimator on rotation {
          running: root.state === "thinking"
          loops: Animation.Infinite
          from: 0
          to: 360
          duration: 1400
        }
      }

      // the orb itself, with its glow
      Item {
        id: core
        width: 56                                     // fixed size, scaled: no geometry rebuilt per frame
        height: 56
        x: orb.cx - width / 2
        y: orb.cy - height / 2
        scale: 2 * orb.coreRadius * orb.breath / width
        Behavior on scale { NumberAnimation { duration: 70; easing.type: Easing.OutQuad } }

        Rectangle {
          anchors.fill: parent
          radius: width / 2
          gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.lighter(root.accent, 1.35) }
            GradientStop { position: 1.0; color: root.accent }
          }
        }
      }

      // The halo: a radial gradient the GPU fills in one pass. A blurred layer looks the
      // same and costs ten percent of a core, because it re-renders offscreen every frame.
      Shape {
        id: halo
        width: 240
        height: 240
        x: orb.cx - width / 2
        y: orb.cy - height / 2
        z: -1
        scale: core.scale * 0.9
        opacity: root.state === "acting" ? 1.0 : 0.62
        Behavior on scale { NumberAnimation { duration: 70; easing.type: Easing.OutQuad } }
        Behavior on opacity { NumberAnimation { duration: 200 } }
        ShapePath {
          strokeWidth: 0
          fillGradient: RadialGradient {
            centerX: 120
            centerY: 120
            centerRadius: 120
            focalX: 120
            focalY: 120
            GradientStop { position: 0.00; color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.55) }
            GradientStop { position: 0.22; color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.26) }
            GradientStop { position: 0.55; color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.07) }
            GradientStop { position: 1.00; color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.0) }
          }
          PathAngleArc {
            centerX: 120
            centerY: 120
            radiusX: 120
            radiusY: 120
            startAngle: 0
            sweepAngle: 360
          }
        }
      }

      // one bright bloom when a command is carried out
      Rectangle {
        id: bloom
        width: 60
        height: 60
        x: orb.cx - width / 2
        y: orb.cy - height / 2
        radius: width / 2
        color: "transparent"
        border.width: 2
        border.color: root.accent
        opacity: 0
        ParallelAnimation {
          id: bloomAnimation
          NumberAnimation { target: bloom; property: "width"; from: 40; to: 190; duration: 520; easing.type: Easing.OutCubic }
          NumberAnimation { target: bloom; property: "height"; from: 40; to: 190; duration: 520; easing.type: Easing.OutCubic }
          NumberAnimation { target: bloom; property: "opacity"; from: 0.75; to: 0; duration: 520; easing.type: Easing.OutCubic }
        }
      }
      Connections {
        target: root
        function onStateChanged() {
          if (root.state === "acting")
            bloomAnimation.restart()
        }
      }
    }
  }
}
