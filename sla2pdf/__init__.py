"""sla2pdf: render Scribus .sla templates to PDF without running Scribus."""
from .colors import set_cmyk_profile
from .document import RenderResult, render_document
from .sla import load_document
from .variables import fill, load_values

__version__ = "0.1.0"
__all__ = ["render_document", "RenderResult", "load_document", "load_values", "fill",
           "set_cmyk_profile", "__version__"]
