"""Media decoding and playback for the client.

Isolated from ``client/net`` and ``client/gui`` because everything here needs
PyAV, which is an optional extra. The client must still start and explain
itself on a machine without it.
"""
