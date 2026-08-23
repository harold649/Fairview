"""Parsers for the Clover daily export set.

One module per export. Every parser runs a header-based identity gate before
reading a single row, because Clover ships several reports whose filenames
differ by a few characters and whose contents are not interchangeable.
"""

from .identity import FileIdentityError, gate  # noqa: F401
