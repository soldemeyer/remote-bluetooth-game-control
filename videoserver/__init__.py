"""Standalone video server: capture, encode, and stream to controller clients.

Runs on the PC holding the capture card, or as a subprocess on the Bluetooth
server itself. Either way it is the same program: the Bluetooth server is only
a control plane, and media travels straight from here to each client.
"""
