"""
Compatibility shim for packet/frame layer.
"""

try:
    from cup.link.frame import *  # type: ignore
except Exception:
    try:
        from packet import *  # type: ignore
    except Exception as e:
        raise ImportError(
            "Cannot find packet/frame implementation.\n"
            "Expected one of:\n"
            "  - cup/link/frame.py\n"
            "  - packet.py (project root)"
        ) from e
