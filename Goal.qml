// The goal overlay: what the harness is doing right now, step by step, in the top-right corner.
//
// Every step says who chose it — a SKILL replayed from last time, the MAP knowing the way, or
// JEV working it out — because that is the difference between instant and free, and a model
// deciding. It appears when a goal starts, stays while it works or asks, and fades out a few
// seconds after it ends. It never takes a click.
import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Scope {
  id: root

  property string title: ""
  property string where: ""
  property string status: ""          // working · asking · done · stuck · declined · stopped · refused · error
  property var steps: []
  property string answer: ""
  property string used: ""            // skill · map · jev · skill+jev · map+jev
  property bool learned: false
  property real cost: 0
  property real finished: 0           // seconds since the epoch, 0 while working
  property string fontFamily: Style.font.family

  readonly property bool running: status === "working" || status === "asking"
  property real now: Date.now() / 1000
  readonly property bool shown: title !== "" && (running || (finished > 0 && now - finished < 12))
  readonly property color accent: Color.accent
  readonly property color text: Color.popups.text
  readonly property color dim: Util.alpha(Color.popups.text, 0.55)

  Timer {                              // only to let the finished card fade on time
    interval: 1000
    running: root.finished > 0 && !root.running
    repeat: true
    onTriggered: root.now = Date.now() / 1000
  }

  function badgeColor(source) {
    if (source === "skill") return Color.accent
    if (source === "map") return Qt.lighter(Color.accent, 1.25)
    return Color.muted
  }

  function mark(step) {
    if (step.kind === "done") return "✓"
    if (step.asked && step.ok === null) return "?"
    if (step.ok === false) return "✕"
    if (step.ok === true) return "•"
    return "◐"
  }

  function statusText() {
    switch (root.status) {
    case "working": return "working"
    case "asking": return "waiting for you"
    case "done": return root.learned ? "done · learned" : "done"
    case "stuck": return "got stuck"
    case "declined": return "you said no"
    case "stopped": return "stopped"
    case "refused": return "refused"
    default: return root.status
    }
  }

  PanelWindow {
    visible: root.shown
    anchors { top: true; right: true }
    margins { top: Style.space(8); right: Style.space(12) }
    implicitWidth: card.width
    implicitHeight: card.height
    color: "transparent"
    WlrLayershell.namespace: "omarchy-voice-goal"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    mask: Region {}

    BorderSurface {
      id: card
      width: Style.space(380)
      height: column.implicitHeight + Style.space(28)
      color: Util.alpha(Color.popups.background, 0.96)
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      radius: Style.cornerRadius
      opacity: root.running || root.now - root.finished < 10 ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 600; easing.type: Easing.OutCubic } }

      Column {
        id: column
        x: Style.space(14)
        y: Style.space(14)
        width: parent.width - Style.space(28)
        spacing: Style.space(8)

        // where, and how it is going
        Item {
          width: parent.width
          height: whereText.implicitHeight
          Text {
            id: whereText
            text: "󰓅  Goal" + (root.where ? " · in " + root.where : "")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          Text {
            anchors.right: parent.right
            text: root.statusText()
            color: root.status === "done" ? root.accent : (root.running ? root.dim : Color.urgent)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: root.status === "asking"
          }
        }

        Text {
          width: parent.width
          text: root.title
          color: root.text
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
          wrapMode: Text.Wrap
          maximumLineCount: 2
          elide: Text.ElideRight
        }

        // the steps, each with who chose it
        Repeater {
          model: root.steps
          delegate: Item {
            required property var modelData
            width: column.width
            height: Math.max(label.implicitHeight, badge.height)
            Text {
              id: markText
              width: Style.space(14)
              text: root.mark(modelData)
              color: modelData.ok === false ? Color.urgent : root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
            Text {
              id: label
              anchors.left: markText.right
              anchors.right: badge.left
              anchors.rightMargin: Style.space(8)
              text: modelData.label || modelData.kind
              color: modelData.kind === "done" ? root.text : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
            Rectangle {
              id: badge
              anchors.right: parent.right
              anchors.verticalCenter: label.verticalCenter
              width: badgeText.implicitWidth + Style.space(10)
              height: badgeText.implicitHeight + Style.space(4)
              radius: height / 2
              color: Util.alpha(root.badgeColor(modelData.source), 0.18)
              border.width: 1
              border.color: Util.alpha(root.badgeColor(modelData.source), 0.6)
              Text {
                id: badgeText
                anchors.centerIn: parent
                text: String(modelData.source || "jev").toUpperCase()
                color: root.badgeColor(modelData.source)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption - 1
                font.bold: true
              }
            }
          }
        }

        // the answer, quoted from the app or the page
        Text {
          visible: root.answer !== ""
          width: parent.width
          text: "“" + root.answer + "”"
          color: root.text
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.italic: true
          wrapMode: Text.Wrap
          maximumLineCount: 3
          elide: Text.ElideRight
        }

        Text {
          width: parent.width
          text: root.running
                ? "say “stop” to end it"
                : (root.used === "skill" ? "replayed from a skill — no model, no cost"
                   : root.used === "map" ? "found on the app map — no model, no cost"
                   : "Jev · $" + root.cost.toFixed(4) + (root.learned ? " · kept as a skill for next time" : ""))
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }
      }
    }
  }
}
