"""The one visual style shared by the live panel and the run replay (#21).

Both pages stay self-contained: the shared stylesheet and display script are inlined into each
page's template at the `/*@SHARED_CSS@*/` and `/*@SHARED_JS@*/` markers.
"""

from importlib.resources import files


def assemble(template):
    """A template with the shared theme and display script inlined."""
    ui = files("flapsim").joinpath("data/ui")
    css = ui.joinpath("theme.css").read_text(encoding="utf-8")
    js = ui.joinpath("display.js").read_text(encoding="utf-8")
    # Both assets are static and contain neither marker, so chained replaces are exact.
    return template.replace("/*@SHARED_CSS@*/", css).replace("/*@SHARED_JS@*/", js)
