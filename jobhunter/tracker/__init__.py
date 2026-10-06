"""The application tracker: applications, their stages and events, and the local page that shows them."""
STAGES = ("applied", "screen", "interview", "offer", "accepted", "rejected", "withdrawn", "ghosted")
OPEN_STAGES = ("applied", "screen", "interview", "offer")
CLOSED_STAGES = ("accepted", "rejected", "withdrawn", "ghosted")
REPLY_STAGES = ("screen", "interview", "offer", "accepted")       # the company answered
EVENT_KINDS = ("stage", "follow_up_set", "follow_up_done")
