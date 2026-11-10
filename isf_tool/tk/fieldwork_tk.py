"""RF-FIELDWORK desktop theme for Tkinter / ttk (Python 3.6+, Tk 8.5+).

One look for every RF-FIELDWORK tool: dark instrument glass, one accent colour
per product (the same VNA trace colours used on the website), one primary
action per panel, readable status, dark-matched plots.

Usage::

    import fieldwork_tk as fw
    root = tk.Tk()
    fw.apply(root, product="flux")          # flux | cap | layout | isf | vco
    bar = fw.header(root, "Flux Studio", "RF passive layout and EMX")
    bar.pack(fill="x")
    ttk.Button(panel, text="Run", style="Primary.TButton")
    chip = fw.StatusChip(statusbar); chip.set("running", "Sweep 3/8")
    fw.apply_matplotlib()                    # optional, if matplotlib is used

Vendor this single file into each product (no dependency on the website).
"""
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

__version__ = "1.0.0"

# --- palette ---------------------------------------------------------------
BG = "#111412"        # window
PANEL = "#181C1A"     # side panels, frames
RAISED = "#202522"    # inputs, buttons
LINE = "#2E3531"      # borders, separators
TEXT = "#D8E0DA"
DIM = "#8C978F"
MUTED = "#5F6963"
PLOT_BG = "#0C0F0D"
OK = "#93D65E"
WARN = "#F0B12C"
ERR = "#F26B5B"

ACCENTS = {
    "flux": "#F2C230",    # Tr1 yellow
    "cap": "#33D1DE",     # Tr2 cyan
    "layout": "#EC5AA5",  # Tr3 magenta
    "isf": "#93D65E",     # Tr4 green
    "vco": "#9C8CFF",     # Tr5 violet
}
INK = "#111412"       # text on accent fills

SPACE = 8             # base spacing unit (use 4, 8, 12, 16, 24)

_FAMILIES = ("Inter", "Noto Sans", "Source Sans 3", "Liberation Sans", "DejaVu Sans", "Helvetica")
_MONO = ("JetBrains Mono", "Noto Sans Mono", "Liberation Mono", "DejaVu Sans Mono", "Courier")
_CJK = ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "PingFang SC")


def _pick(root, names):
    have = set(tkfont.families(root))
    for n in names:
        if n in have:
            return n
    return "TkDefaultFont"


def accent(product):
    return ACCENTS.get(product, ACCENTS["flux"])


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def apply(root, product="flux", scale=None):
    """Configure fonts, ttk styles and classic-Tk option defaults."""
    acc = accent(product)
    root._fw_accent = acc
    fam = _pick(root, _FAMILIES)
    mono = _pick(root, _MONO)
    if scale:
        root.tk.call("tk", "scaling", scale)

    for name, size, weight in (("TkDefaultFont", 10, "normal"), ("TkTextFont", 10, "normal"),
                               ("TkMenuFont", 10, "normal"), ("TkHeadingFont", 10, "bold"),
                               ("TkCaptionFont", 11, "bold"), ("TkSmallCaptionFont", 9, "normal"),
                               ("TkTooltipFont", 9, "normal")):
        try:
            tkfont.nametofont(name).configure(family=fam, size=size, weight=weight)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=mono, size=10)
    except tk.TclError:
        pass
    root.fw_fonts = {
        "title": (fam, 14, "bold"), "h2": (fam, 11, "bold"), "body": (fam, 10),
        "small": (fam, 9), "mono": (mono, 10), "num": (fam, 12, "bold"),
    }

    root.configure(bg=BG)
    # classic Tk widgets (Listbox, Text, Canvas, Menu, Toplevel ...)
    for pat, val in (("*Background", BG), ("*Foreground", TEXT), ("*activeBackground", RAISED),
                     ("*activeForeground", TEXT), ("*selectBackground", _mix(BG, acc, .35)),
                     ("*selectForeground", TEXT), ("*insertBackground", acc),
                     ("*highlightThickness", 0), ("*borderWidth", 0), ("*relief", "flat"),
                     ("*Listbox.background", RAISED), ("*Text.background", RAISED),
                     ("*Entry.background", RAISED), ("*Canvas.background", PLOT_BG),
                     ("*Menu.background", PANEL), ("*Menu.activeBackground", _mix(PANEL, acc, .25)),
                     ("*Menu.borderWidth", 1), ("*troughColor", BG)):
        root.option_add(pat, val)

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=RAISED, bordercolor=LINE,
                 lightcolor=LINE, darkcolor=LINE, troughcolor=BG, focuscolor=acc,
                 selectbackground=_mix(BG, acc, .35), selectforeground=TEXT, insertcolor=acc,
                 font=(fam, 10))
    st.map(".", foreground=[("disabled", MUTED)])

    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Panel.TLabel", background=PANEL)
    st.configure("Dim.TLabel", foreground=DIM)
    st.configure("PanelDim.TLabel", background=PANEL, foreground=DIM)
    st.configure("Section.TLabel", background=PANEL, foreground=DIM, font=(fam, 9, "bold"))
    st.configure("Title.TLabel", font=(fam, 14, "bold"))
    st.configure("Num.TLabel", font=(fam, 12, "bold"))
    st.configure("TLabelframe", background=PANEL, bordercolor=LINE)
    st.configure("TLabelframe.Label", background=PANEL, foreground=DIM, font=(fam, 9, "bold"))

    # buttons: neutral, primary (one per panel), ghost (tertiary), danger
    st.configure("TButton", background=RAISED, foreground=TEXT, bordercolor=LINE, lightcolor=RAISED,
                 darkcolor=RAISED, padding=(12, 6), relief="flat")
    st.map("TButton", background=[("disabled", BG), ("pressed", LINE), ("active", "#2A302C")],
           foreground=[("disabled", MUTED)], bordercolor=[("focus", acc)])
    st.configure("Primary.TButton", background=acc, foreground=INK, bordercolor=acc, lightcolor=acc,
                 darkcolor=acc, font=(fam, 10, "bold"), padding=(14, 7))
    st.map("Primary.TButton", background=[("disabled", _mix(BG, acc, .18)), ("pressed", _mix(acc, BG, .25)),
                                          ("active", _mix(acc, "#FFFFFF", .15))],
           foreground=[("disabled", _mix(BG, acc, .5))], bordercolor=[("disabled", _mix(BG, acc, .18))])
    st.configure("Ghost.TButton", background=BG, foreground=DIM, bordercolor=BG, lightcolor=BG, darkcolor=BG)
    st.map("Ghost.TButton", foreground=[("active", TEXT), ("disabled", MUTED)], background=[("active", PANEL)])
    st.configure("Danger.TButton", foreground=ERR)

    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground=RAISED, background=RAISED, foreground=TEXT, bordercolor=LINE,
                     lightcolor=RAISED, darkcolor=RAISED, arrowcolor=DIM, padding=(6, 4))
        st.map(w, bordercolor=[("focus", acc)], fieldbackground=[("readonly", RAISED), ("disabled", BG)],
               foreground=[("disabled", MUTED)], arrowcolor=[("active", acc)])
    root.option_add("*TCombobox*Listbox.background", RAISED)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", _mix(BG, acc, .35))

    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=BG, foreground=TEXT, indicatorbackground=RAISED,
                     indicatorforeground=acc, focuscolor=BG)
        st.map(w, indicatorbackground=[("selected", RAISED)], indicatorcolor=[("selected", acc), ("!selected", RAISED)],
               background=[("active", BG)], foreground=[("disabled", MUTED)])
    st.configure("Panel.TCheckbutton", background=PANEL)
    st.configure("Panel.TRadiobutton", background=PANEL)

    st.configure("TNotebook", background=BG, bordercolor=LINE, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=DIM, bordercolor=BG, lightcolor=BG,
                 padding=(14, 6), font=(fam, 10, "bold"))
    st.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", TEXT), ("active", TEXT)],
           lightcolor=[("selected", acc)], bordercolor=[("selected", LINE)])

    st.configure("Treeview", background=RAISED, fieldbackground=RAISED, foreground=TEXT, bordercolor=LINE,
                 rowheight=24)
    st.map("Treeview", background=[("selected", _mix(BG, acc, .35))], foreground=[("selected", TEXT)])
    st.configure("Treeview.Heading", background=PANEL, foreground=DIM, bordercolor=LINE, lightcolor=PANEL,
                 darkcolor=PANEL, font=(fam, 9, "bold"), padding=(6, 4))
    st.map("Treeview.Heading", background=[("active", RAISED)])

    st.configure("TScrollbar", background=RAISED, troughcolor=BG, bordercolor=BG, arrowcolor=DIM,
                 lightcolor=RAISED, darkcolor=RAISED, gripcount=0)
    st.map("TScrollbar", background=[("active", LINE)])
    st.configure("Horizontal.TProgressbar", background=acc, troughcolor=RAISED, bordercolor=BG,
                 lightcolor=acc, darkcolor=acc, thickness=6)
    st.configure("TScale", background=BG, troughcolor=RAISED)
    st.configure("TSeparator", background=LINE)
    st.configure("TPanedwindow", background=LINE)
    st.configure("Sash", sashthickness=4, background=LINE)
    return st


def header(parent, product_name, subtitle="", product=None, right=None):
    """Top bar shared by all tools: accent stripe, product name, subtitle.

    ``right`` is an optional callable(frame) to add actions on the right.
    """
    root = parent.winfo_toplevel()
    acc = accent(product) if product else getattr(root, "_fw_accent", ACCENTS["flux"])
    fonts = getattr(root, "fw_fonts", {"title": ("TkDefaultFont", 14, "bold"), "small": ("TkDefaultFont", 9)})
    outer = tk.Frame(parent, bg=PANEL)
    tk.Frame(outer, bg=acc, height=3).pack(fill="x", side="top")
    row = tk.Frame(outer, bg=PANEL)
    row.pack(fill="x", padx=16, pady=(8, 8))
    dot = tk.Canvas(row, width=12, height=12, bg=PANEL, highlightthickness=0)
    dot.create_oval(1, 1, 11, 11, fill=acc, outline="")
    dot.pack(side="left", padx=(0, 10))
    tk.Label(row, text=product_name, bg=PANEL, fg=TEXT, font=fonts["title"]).pack(side="left")
    if subtitle:
        tk.Label(row, text=subtitle, bg=PANEL, fg=DIM, font=fonts["small"]).pack(side="left", padx=(12, 0), pady=(3, 0))
    if right:
        box = tk.Frame(row, bg=PANEL)
        box.pack(side="right")
        right(box)
    tk.Frame(outer, bg=LINE, height=1).pack(fill="x", side="bottom")
    return outer


class StatusChip(tk.Frame):
    """Readable state indicator: idle | running | done | warn | error."""
    COLORS = {"idle": MUTED, "running": None, "done": OK, "warn": WARN, "error": ERR}
    LABELS = {"idle": "Ready", "running": "Running", "done": "Done", "warn": "Check", "error": "Error"}

    def __init__(self, parent, bg=PANEL, **kw):
        tk.Frame.__init__(self, parent, bg=bg, **kw)
        root = parent.winfo_toplevel()
        self._acc = getattr(root, "_fw_accent", ACCENTS["flux"])
        f = getattr(root, "fw_fonts", {"small": ("TkDefaultFont", 9), "h2": ("TkDefaultFont", 10, "bold")})
        self._dot = tk.Canvas(self, width=10, height=10, bg=bg, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 6))
        self._state = tk.Label(self, bg=bg, fg=TEXT, font=f["h2"])
        self._state.pack(side="left")
        self._msg = tk.Label(self, bg=bg, fg=DIM, font=f["small"])
        self._msg.pack(side="left", padx=(8, 0))
        self.set("idle")

    def set(self, state, message=""):
        col = self.COLORS.get(state) or self._acc
        self._dot.delete("all")
        self._dot.create_oval(1, 1, 9, 9, fill=col, outline="")
        self._state.configure(text=self.LABELS.get(state, state), fg=col if state != "idle" else TEXT)
        self._msg.configure(text=message)


def section(parent, text):
    """Small section heading inside a panel (sentence case, not all caps)."""
    return ttk.Label(parent, text=text, style="Section.TLabel")


# --- plots -----------------------------------------------------------------
SERIES = ["#33D1DE", "#F2C230", "#EC5AA5", "#93D65E", "#9C8CFF", "#F28C4B", "#7FB5FF", "#E6E6E6"]
SEQUENTIAL = "viridis"      # magnitudes (Q, L, |Z|): never a diverging map
DIVERGING = "RdBu_r"        # only for signed data centred on zero (e.g. error, signed k)


def apply_matplotlib(dark=True):
    """Match matplotlib figures to the theme. Call once before creating figures."""
    try:
        import matplotlib as mpl
    except ImportError:
        return
    if not dark:
        return
    vals = {
        "figure.facecolor": PLOT_BG, "axes.facecolor": PLOT_BG, "savefig.facecolor": PLOT_BG,
        "axes.edgecolor": LINE, "axes.labelcolor": DIM, "axes.titlecolor": TEXT,
        "xtick.color": DIM, "ytick.color": DIM, "grid.color": "#26302B", "grid.linewidth": 0.6,
        "axes.grid": True, "text.color": TEXT, "legend.facecolor": PANEL, "legend.edgecolor": LINE,
        "legend.labelcolor": TEXT, "axes.prop_cycle": mpl.cycler(color=SERIES), "lines.linewidth": 1.6,
        "image.cmap": SEQUENTIAL, "font.size": 9,
    }
    for k, v in vals.items():
        try:
            mpl.rcParams[k] = v
        except (KeyError, ValueError):
            pass  # older matplotlib lacks some keys


def tk_plot_colors():
    """Colours for hand-drawn Canvas plots."""
    return {"bg": PLOT_BG, "grid": "#26302B", "axis": LINE, "text": DIM, "series": SERIES}
