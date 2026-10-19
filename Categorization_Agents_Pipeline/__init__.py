"""Turn scraped Meetup bios into structured categories, tags and entities.

Eight agents, a queue, and no model call anywhere: the whole thing is lexicon +
regex, which is why it classifies tens of thousands of bios in seconds and gives
the same answer twice. See README.md for the shape and pipeline.py for the flow.
"""
