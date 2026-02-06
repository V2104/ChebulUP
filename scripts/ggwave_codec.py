"""
Compatibility shim for ggwave codec.
"""

try:
    # New structured location
    from cup.phy.codec import *  # type: ignore
except Exception:
    try:
        # Legacy / flat layout fallback
        from ggwave_codec import *  # type: ignore
    except Exception as e:
        raise ImportError(
            "Cannot find ggwave codec implementation.\n"
            "Expected one of:\n"
            "  - cup/phy/codec.py\n"
            "  - ggwave_codec.py (project root)"
        ) from e
