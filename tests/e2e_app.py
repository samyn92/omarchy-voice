#!/usr/bin/python3
"""Tiny GTK window for e2e.py: two buttons and a text field that log what happens to them."""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

log = open(sys.argv[1], "w", buffering=1)
app = Gtk.Application(application_id="dev.jev.e2e")


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title="jev e2e test")
    win.set_default_size(700, 420)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    save = Gtk.Button(label="Save draft")
    save.connect("clicked", lambda *_: log.write("clicked Save draft\n"))
    delete = Gtk.Button(label="Delete everything")
    delete.connect("clicked", lambda *_: log.write("clicked Delete everything\n"))
    entry = Gtk.Entry(placeholder_text="Your name")
    entry.connect("changed", lambda e: log.write(f"entry {e.get_text()}\n"))
    area = Gtk.Label(label="scroll area")
    area.set_vexpand(True)
    for child in (save, delete, entry, area):
        box.append(child)
    win.set_child(box)
    win.present()


app.connect("activate", on_activate)
app.run([])
