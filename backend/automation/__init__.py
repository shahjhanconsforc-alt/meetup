"""Browser automation.

Layered deliberately, bottom to top:

    playwright_runner   owns the browser process and its persistent profiles
    typing / cursor     make an action look like a person did it
    media               attachments for a message
    visit               generic page moves: goto, scroll, collect links
    meetup              everything that knows a Meetup selector
    campaign            a whole job — scrape a group, message its members

Only `meetup` knows what Meetup's DOM looks like. When their markup changes —
and it does — the selectors in that one module are what needs updating, and
nothing above or below it should have to change.
"""
