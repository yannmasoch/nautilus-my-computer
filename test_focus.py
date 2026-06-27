import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
def on_focus(win, param):
    print("focus changed to", win.get_focus())
