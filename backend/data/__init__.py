"""Runtime data — store.json, screenshots, and the Chrome profiles.

Everything in here is generated and gitignored. It is also the ONE directory
that must be a volume in Docker: it holds the logged-in browser profiles, and
losing it means every account logging in again from an unfamiliar device.
"""
