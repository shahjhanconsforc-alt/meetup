"""HTTP routers, one per panel in the UI.

    accounts_routes     accounts + their sessions
    automation_routes   the browser, scraping, run control
    campaign_routes     drafts, targeting, outreach, RSVPs
    data_routes         the stored members/categories, and the activity feed
    minio_routes        object storage health, buckets, media, backups
    settings_routes     the tunable knobs

Routes stay thin on purpose: they validate input, call one function in the layer
below, and translate its exceptions into status codes. Anything that reads like
logic belongs in backend/ or backend/automation/, not here.
"""
